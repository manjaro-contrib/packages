#!/usr/bin/env python3
"""Reconcile the unstable branch with packages.yml.

testing and stable are declarative: their manifests name what the branch
carries, and apply_manifest deletes whatever is published but unwanted.
unstable has no manifest, because it is built into rather than promoted
into, so nothing ever reconciled it - a package that stopped being built
stayed published forever, and packages.yml and the bucket diverged in
silence.

packages.yml already authorises builds; this makes it authoritative for
what stays published too. Being listed is the whole contract: unlist a
package and its artifacts go.

Withdrawal only. Publishing is what adds packages, and a package listed
but absent is a build that has not happened yet, not something to repair.

Runs under the repo-publish concurrency lock - it rewrites the database.
"""

import argparse
import os
import subprocess
import sys
import tempfile

import yaml
from botocore.exceptions import ClientError
from repo_common import DB_SUFFIXES, list_packages, s3_client
from repo_remove import pkgname_of
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def orphans(s3, bucket: str, prefix: str, allowed: set[str]) -> dict[str, list[str]]:
    """Published artifacts whose package is no longer listed, by name."""
    found: dict[str, list[str]] = {}
    for filename in list_packages(s3, bucket, prefix):
        name = pkgname_of(filename)
        if name and name not in allowed:
            found.setdefault(name, []).append(filename)
    return found


def withdraw(
    s3,
    bucket: str,
    branch: str,
    arch: str,
    db_name: str,
    allowed: set[str],
    dry_run: bool,
) -> list[str]:
    """Delete unlisted packages from one branch. Returns the names removed."""
    prefix = f"{branch}/{arch}/"
    found = orphans(s3, bucket, prefix, allowed)
    if not found:
        log(f"{branch}/{arch}: nothing to withdraw")
        return []

    for name, files in sorted(found.items()):
        log(f"{branch}/{arch}: {name} is published but not listed"
            f" ({len(files)} artifact(s))")
    if dry_run:
        log(f"{branch}/{arch}: dry run, nothing deleted")
        return sorted(found)

    with tempfile.TemporaryDirectory() as workdir:
        db_file = os.path.join(workdir, f"{db_name}.db.tar.gz")
        # .files must come along or repo-remove rebuilds it from nothing,
        # dropping every other package's file list
        for suffix in (".db.tar.gz", ".files.tar.gz"):
            local = os.path.join(workdir, f"{db_name}{suffix}")
            try:
                s3.download_file(bucket, prefix + f"{db_name}{suffix}", local)
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise
        if not os.path.exists(db_file):
            log(f"{branch}/{arch}: no database, skipping")
            return []

        repo_remove = ["repo-remove", db_file, *sorted(found)]
        key = os.environ.get("GPG_KEYID")
        if key:
            # the rewritten database needs a fresh signature
            repo_remove[1:1] = ["--sign", "--key", key]
        subprocess.run(repo_remove, check=True, stdout=subprocess.DEVNULL)

        for name, files in sorted(found.items()):
            for filename in files:
                for suffix in ("", ".sig"):
                    try:
                        s3.delete_object(Bucket=bucket, Key=prefix + filename + suffix)
                    except ClientError as e:
                        if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                            raise
            log(f"{branch}/{arch}: withdrew {name}")

        for suffix in DB_SUFFIXES:
            for fname in (f"{db_name}{suffix}", f"{db_name}{suffix}.sig"):
                # repo-remove writes .db/.files as symlinks to the archives
                real = os.path.realpath(os.path.join(workdir, fname))
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, prefix + fname)

    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument("--branch", default="unstable")
    parser.add_argument("--arches", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be withdrawn without deleting",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        allowed = set(yaml.safe_load(f)["packages"] or {})
    if not allowed:
        # an empty list would withdraw everything; a truncated config is
        # far likelier than a deliberate wipe
        log(f"{args.config} lists no packages, refusing to withdraw everything")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()

    removed = []
    for arch in [a.strip() for a in args.arches.split(",") if a.strip()]:
        removed += withdraw(
            s3, bucket, args.branch, arch, args.db_name, allowed, args.dry_run
        )

    if removed and not args.dry_run:
        write_state(s3, bucket, log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
