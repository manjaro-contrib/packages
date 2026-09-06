#!/usr/bin/env python3
"""Promote built packages between repo branches via server-side R2 copies.

Copies .pkg.tar.zst objects (and their .sig if present) from the source
branch prefix to the target prefix without egress, then rebuilds the target
branch's pacman database locally and re-uploads it. Versions the copy
supersedes are pruned from the target, matching what publish.py does.

--dry-run resolves and classifies every package against the target without
writing anything, which is the only safe way to inspect a full ALL sync.
"""

import argparse
import os
import subprocess
import sys
import tempfile

import boto3
from botocore.exceptions import ClientError
from publish import prune_superseded
from repo_remove import pkgname_of
from repo_state import write_state

DB_SUFFIXES = [".db", ".db.tar.gz", ".files", ".files.tar.gz"]
# Manjaro's lifecycle flows one way: a package must age through each branch.
# Promoting backwards, or skipping a stage, would put binaries in stable
# that no one ran in testing.
FLOW = {"testing": "unstable", "stable": "testing"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_packages(s3, bucket: str, prefix: str) -> list[str]:
    names = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].removeprefix(prefix)
            if name.endswith(".pkg.tar.zst"):
                names.append(name)
    return names


def copy_object(s3, bucket: str, src_key: str, dst_key: str) -> bool:
    try:
        s3.copy_object(
            Bucket=bucket, CopySource={"Bucket": bucket, "Key": src_key}, Key=dst_key
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return False
        raise


def download(s3, bucket: str, key: str, dest: str) -> bool:
    try:
        s3.download_file(bucket, key, dest)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return False
        raise


def object_etag(s3, bucket: str, key: str) -> str | None:
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ETag"].strip('"')
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def plan_promotion(
    s3, bucket: str, src_prefix: str, dst_prefix: str, names: list[str]
) -> list[dict]:
    """Classify what promoting each name would do to the target branch.

    Resolves against live object state so the plan reflects exactly what a
    real run would perform: missing sources abort, identical objects are
    no-ops, and an existing other version is reported as a replacement.
    """
    existing = {}
    for key in list_packages(s3, bucket, dst_prefix):
        name = pkgname_of(key)
        if name:
            existing.setdefault(name, []).append(key)

    plan = []
    for name in names:
        src_etag = object_etag(s3, bucket, src_prefix + name)
        if src_etag is None:
            plan.append({"name": name, "action": "missing", "detail": ""})
            continue
        dst_etag = object_etag(s3, bucket, dst_prefix + name)
        if dst_etag == src_etag:
            plan.append({"name": name, "action": "identical", "detail": ""})
            continue
        pkg = pkgname_of(name)
        superseded = sorted(k for k in existing.get(pkg, []) if k != name)
        if dst_etag is not None:
            action, detail = "overwrite", "same filename, different bytes"
        elif superseded:
            action, detail = "replace", ", ".join(superseded)
        else:
            action, detail = "new", ""
        plan.append({"name": name, "action": action, "detail": detail})
    return plan


def report_plan(plan: list[dict], source: str, target: str) -> int:
    order = ["new", "replace", "overwrite", "identical", "missing"]
    for action in order:
        rows = [p for p in plan if p["action"] == action]
        if not rows:
            continue
        log(f"{action} ({len(rows)}):")
        for row in rows:
            suffix = f"  <- supersedes {row['detail']}" if row["detail"] else ""
            log(f"  {row['name']}{suffix}")

    missing = [p for p in plan if p["action"] == "missing"]
    changing = [p for p in plan if p["action"] in ("new", "replace", "overwrite")]
    log("")
    log(f"would promote {len(changing)} package(s) {source} -> {target}")
    if any(p["action"] == "replace" for p in plan):
        # promote only adds; repo-add drops the old entry from the database
        # but the superseded file itself stays in the bucket
        log("note: superseded files remain in the bucket, only the db entry changes")
    if missing:
        log(f"ERROR: {len(missing)} package(s) missing in {source}; a real run aborts")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--target-branch", required=True)
    parser.add_argument(
        "--packages",
        required=True,
        help="comma-separated .pkg.tar.zst filenames, or ALL for a full sync",
    )
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing to the bucket",
    )
    args = parser.parse_args()

    expected = FLOW.get(args.target_branch)
    if expected is None:
        log(
            f"{args.target_branch} is not a promotion target; "
            f"valid targets: {', '.join(sorted(FLOW))}"
        )
        return 1
    if args.source_branch != expected:
        log(
            f"{args.source_branch} -> {args.target_branch} is not a valid "
            f"promotion; {args.target_branch} is promoted from {expected}"
        )
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    src_prefix = f"{args.source_branch}/{args.arch}/"
    dst_prefix = f"{args.target_branch}/{args.arch}/"

    if args.packages == "ALL":
        names = list_packages(s3, bucket, src_prefix)
    else:
        names = [n.strip() for n in args.packages.split(",") if n.strip()]
    if not names:
        log("nothing to promote")
        return 1

    if args.dry_run:
        plan = plan_promotion(s3, bucket, src_prefix, dst_prefix, names)
        return report_plan(plan, args.source_branch, args.target_branch)

    copied = []
    for name in names:
        if not copy_object(s3, bucket, src_prefix + name, dst_prefix + name):
            log(f"{name}: missing in {args.source_branch}, aborting")
            return 1
        copy_object(s3, bucket, src_prefix + name + ".sig", dst_prefix + name + ".sig")
        copied.append(name)
        log(f"copied {name}")

    with tempfile.TemporaryDirectory() as workdir:
        db_file = os.path.join(workdir, f"{args.db_name}.db.tar.gz")
        if not download(s3, bucket, dst_prefix + f"{args.db_name}.db.tar.gz", db_file):
            log(f"no database in {args.target_branch} yet, repo-add will create one")

        # repo-add needs the package files present to hash and read metadata
        pkg_paths = []
        for name in copied:
            path = os.path.join(workdir, name)
            download(s3, bucket, dst_prefix + name, path)
            pkg_paths.append(path)

        repo_add = ["repo-add", db_file, *pkg_paths]
        key = os.environ.get("GPG_KEYID")
        if key:
            repo_add[1:1] = ["--sign", "--key", key]
        subprocess.run(repo_add, check=True)

        # promote only ever adds; without this the versions it supersedes
        # linger in the target bucket, invisible to pacman but still billed
        prune_superseded(s3, bucket, dst_prefix, copied)

        for suffix in DB_SUFFIXES:
            for name in (f"{args.db_name}{suffix}", f"{args.db_name}{suffix}.sig"):
                local = os.path.join(workdir, name)
                # repo-add writes .db/.files as symlinks; upload the real bytes
                real = os.path.realpath(local)
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, dst_prefix + name)
                log(f"uploaded {name}")

    write_state(s3, bucket, log)

    log(f"promoted {len(copied)} package(s) to {args.target_branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
