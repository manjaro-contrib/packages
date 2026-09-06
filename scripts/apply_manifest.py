#!/usr/bin/env python3
"""Reconcile a branch on R2 to its manifest.

The manifest is the desired state; this computes the difference against the
bucket and applies it. Packages named at a version the branch lacks are
copied server-side from the source branch, packages no longer named are
withdrawn, and the pacman database is rebuilt to match.

Idempotent: applying an unchanged manifest performs no writes, so it is
safe to run on every merge.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from botocore.exceptions import ClientError
from manifest import load
from repo_common import DB_SUFFIXES, FLOW, list_packages, s3_client
from repo_remove import pkgname_of
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def artifact_for(s3, bucket: str, prefix: str, name: str, version: str) -> str | None:
    """The filename carrying `name` at `version` under a prefix, if present."""
    for key in list_packages(s3, bucket, prefix):
        if pkgname_of(key) != name:
            continue
        # name-version-arch.pkg.tar.zst: strip the name and the arch field
        rest = key[len(name) + 1 :].removesuffix(".pkg.tar.zst")
        if rest.rsplit("-", 1)[0] == version:
            return key
    return None


def plan(s3, bucket: str, src_prefix: str, dst_prefix: str, wanted: dict) -> dict:
    """Split the manifest into what must be copied, removed or left alone."""
    present = {}
    for key in list_packages(s3, bucket, dst_prefix):
        name = pkgname_of(key)
        if name:
            present.setdefault(name, []).append(key)

    add, remove, keep, missing = [], [], [], []
    for name, version in wanted.items():
        current = present.get(name, [])
        match = artifact_for(s3, bucket, dst_prefix, name, version)
        if match:
            keep.append(match)
            remove += [k for k in current if k != match]
            continue
        source = artifact_for(s3, bucket, src_prefix, name, version)
        if source is None:
            missing.append(f"{name} {version}")
            continue
        add.append(source)
        remove += current

    for name, keys in present.items():
        if name not in wanted:
            remove += keys
    return {"add": add, "remove": remove, "keep": keep, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--dry-run", action="store_true", help="report the plan without writing"
    )
    args = parser.parse_args()

    source = FLOW.get(args.branch)
    if source is None:
        log(f"{args.branch} is not a promotion target")
        return 1

    wanted = load(args.branch, args.root)
    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    src_prefix = f"{source}/{args.arch}/"
    dst_prefix = f"{args.branch}/{args.arch}/"

    p = plan(s3, bucket, src_prefix, dst_prefix, wanted)
    for kind in ("add", "remove"):
        for key in p[kind]:
            log(f"{kind}: {key}")
    if p["missing"]:
        for entry in p["missing"]:
            log(f"ERROR: {entry} is not available in {source}")
        return 1
    if not p["add"] and not p["remove"]:
        log(f"{args.branch} already matches its manifest")
        return 0
    if args.dry_run:
        log(f"would add {len(p['add'])} and remove {len(p['remove'])} object(s)")
        return 0

    for name in p["add"]:
        for suffix in ("", ".sig"):
            try:
                s3.copy_object(
                    Bucket=bucket,
                    CopySource={"Bucket": bucket, "Key": src_prefix + name + suffix},
                    Key=dst_prefix + name + suffix,
                )
            except ClientError as e:
                if suffix == "" or e.response["Error"]["Code"] not in (
                    "NoSuchKey",
                    "404",
                ):
                    raise
        log(f"copied {name}")

    for key in p["remove"]:
        for suffix in ("", ".sig"):
            try:
                s3.delete_object(Bucket=bucket, Key=dst_prefix + key + suffix)
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise
        log(f"withdrew {key}")

    with tempfile.TemporaryDirectory() as workdir:
        # the database is rebuilt from scratch: repo-add cannot express a
        # removal and an addition in one consistent step
        paths = []
        for name in p["add"] + p["keep"]:
            dest = os.path.join(workdir, name)
            s3.download_file(bucket, dst_prefix + name, dest)
            paths.append(dest)
            # --include-sigs reads the signature from beside the package,
            # so without fetching it the database would carry no %PGPSIG%
            try:
                s3.download_file(bucket, dst_prefix + name + ".sig", dest + ".sig")
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise

        db_file = os.path.join(workdir, f"{args.db_name}.db.tar.gz")
        if paths:
            # --include-sigs records each package's signature in the
            # database, matching every Arch and Manjaro repository;
            # --sign signs the database itself
            cmd = ["repo-add", "--include-sigs", db_file, *paths]
            key = os.environ.get("GPG_KEYID")
            if key:
                cmd[1:1] = ["--sign", "--key", key]
            subprocess.run(cmd, check=True)

        for suffix in DB_SUFFIXES:
            for fname in (f"{args.db_name}{suffix}", f"{args.db_name}{suffix}.sig"):
                real = os.path.realpath(os.path.join(workdir, fname))
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, dst_prefix + fname)
                log(f"uploaded {fname}")

    write_state(s3, bucket, log)
    log(f"{args.branch} now carries {len(wanted)} package(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
