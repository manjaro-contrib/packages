#!/usr/bin/env python3
"""Move objects from <branch>/<arch>/ to <branch>/<repo>/<arch>/.

The old layout had no repository component, so every package lived in one
flat prefix per branch. This copies each object to the prefix its
repository now implies, server-side, and only deletes the old one once the
copy is verified present.

The old prefix is a public URL: every existing pacman.conf points at it, so
this deliberately leaves it in place. Deleting it is a separate decision,
taken once clients have moved.
"""

import argparse
import os
import sys

from botocore.exceptions import ClientError
from catalog import load as load_catalog
from catalog import repo_of
from repo_common import prefix_for, s3_client
from repo_remove import pkgname_of

BRANCHES = ("unstable", "testing", "stable")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def old_prefix(branch: str, arch: str) -> str:
    return f"{branch}/{arch}/"


def plan_moves(s3, bucket: str, branch: str, arch: str, catalog: dict) -> list[tuple]:
    """Every (source key, destination key) the migration would copy.

    A database is not moved: it names packages by filename and is rebuilt
    per repository afterwards, so copying one would leave each repository
    claiming every package.
    """
    src = old_prefix(branch, arch)
    moves = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=src):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key.removeprefix(src)
            if "/" in name or not name:
                # already migrated, or the state file at the root
                continue
            pkgname = pkgname_of(name)
            if pkgname is None:
                # a database, its signature, or the state file: none of
                # them belong to a repository, and the databases are
                # rebuilt per repository afterwards
                continue
            repo = repo_of(catalog.get(pkgname, {}))
            moves.append((key, prefix_for(branch, arch, repo) + name))
    return sorted(moves)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument(
        "--branches", default=",".join(BRANCHES), help="branches to migrate"
    )
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="remove the flat prefix after copying; only once clients have moved",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the plan without writing"
    )
    args = parser.parse_args()

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    catalog = load_catalog(args.config)

    total = 0
    for branch in [b.strip() for b in args.branches.split(",") if b.strip()]:
        moves = plan_moves(s3, bucket, branch, args.arch, catalog)
        if not moves:
            log(f"{branch}: nothing to migrate")
            continue
        log(f"{branch}: {len(moves)} object(s) to move")
        if args.dry_run:
            for src, dst in moves[:6]:
                log(f"  {src} -> {dst}")
            if len(moves) > 6:
                log(f"  ... and {len(moves) - 6} more")
            continue

        for src, dst in moves:
            s3.copy_object(
                Bucket=bucket, CopySource={"Bucket": bucket, "Key": src}, Key=dst
            )
            # the delete is gated on the copy being readable, not on
            # copy_object returning: a half-migrated bucket that still has
            # the original is recoverable, one that does not is not
            if args.delete_old:
                try:
                    s3.head_object(Bucket=bucket, Key=dst)
                except ClientError:
                    log(f"ERROR: {dst} missing after copy, keeping {src}")
                    return 1
                s3.delete_object(Bucket=bucket, Key=src)
            total += 1
        log(f"{branch}: moved {len(moves)} object(s)")

    if args.dry_run:
        log("dry run, nothing written")
        return 0

    log(f"migrated {total} object(s); run rebuild-db to write the new databases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
