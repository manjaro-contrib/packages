#!/usr/bin/env python3
"""Publish freshly built packages to the branch prefix on R2.

Downloads the branch database (initializing a new one if absent), registers
every .pkg.tar.zst in the given directory with repo-add, then uploads the
packages, signatures, and database files. Must run under the workflow-level
concurrency lock — two concurrent publishes would clobber the database.
"""

import argparse
import glob
import os
import subprocess
import sys

from botocore.exceptions import ClientError
from catalog import repo_for_package
from repo_common import (
    DB_SUFFIXES,
    db_name_for,
    legacy_db_name_for,
    prefix_for,
    s3_client,
)
from repo_remove import artifacts_for, pkgname_of
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




def prune_superseded(s3, bucket: str, prefix: str, published: list[str]) -> None:
    """Drop older versions of just-published packages from the bucket.

    repo-add's --remove only unlinks local files, and the previous versions
    exist solely on R2, so they would accumulate forever without this.
    """
    keep = set(published)
    for filename in published:
        for key in artifacts_for(s3, bucket, prefix, pkgname_of(filename)):
            name = key.removeprefix(prefix)
            if name in keep or name.removesuffix(".sig") in keep:
                continue
            s3.delete_object(Bucket=bucket, Key=key)
            log(f"pruned superseded {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pkg-dir", required=True, help="directory of built packages")
    parser.add_argument("--branch", default="unstable")
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    packages = sorted(glob.glob(os.path.join(args.pkg_dir, "*.pkg.tar.zst")))
    if not packages:
        log("no packages to publish")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()

    # a build batch can span repositories, and repo-add writes one database
    # at a time, so group first and publish each repository separately
    grouped: dict[str, list[str]] = {}
    for pkg in packages:
        name = pkgname_of(os.path.basename(pkg))
        # by package name, not by config key: a repository is not always
        # named after the package it builds
        grouped.setdefault(repo_for_package(name), []).append(pkg)

    for repo in sorted(grouped):
        members = grouped[repo]
        prefix = prefix_for(args.branch, args.arch, repo)
        db_name = db_name_for(repo)
        db_file = os.path.join(args.pkg_dir, f"{db_name}.db.tar.gz")

        # a previous repository in this loop leaves its database behind, and
        # repo-add would extend it rather than start the next one clean
        for stale in glob.glob(os.path.join(args.pkg_dir, f"{db_name}.*")):
            os.remove(stale)

        # both databases must be fetched: repo-add updates whichever files it
        # finds and creates the rest from scratch, so publishing with only
        # .db present rebuilt .files from the current build alone, dropping
        # every other package's file list
        for suffix in (".db.tar.gz", ".files.tar.gz"):
            local = os.path.join(args.pkg_dir, f"{db_name}{suffix}")
            try:
                s3.download_file(bucket, prefix + f"{db_name}{suffix}", local)
                log(f"{repo}: downloaded existing {db_name}{suffix}")
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise
                log(f"{repo}: no {db_name}{suffix} yet, repo-add will create one")

        # --include-sigs records each package's signature in the database, as
        # every Arch and Manjaro repository does; tooling that reads a database
        # expects the field, and pacman -Si can then report a package's signer
        # without fetching it. --sign is unrelated: it signs the database
        # itself, without which the package list is forgeable.
        repo_add = ["repo-add", "--include-sigs", db_file, *members]
        key = os.environ.get("GPG_KEYID")
        if key:
            repo_add[1:1] = ["--sign", "--key", key]
        subprocess.run(repo_add, check=True)

        for pkg in members:
            s3.upload_file(pkg, bucket, prefix + os.path.basename(pkg))
            log(f"{repo}: uploaded {os.path.basename(pkg)}")
            sig = pkg + ".sig"
            if os.path.exists(sig):
                s3.upload_file(sig, bucket, prefix + os.path.basename(sig))

        prune_superseded(s3, bucket, prefix, [os.path.basename(p) for p in members])

        legacy = legacy_db_name_for(repo)
        for suffix in DB_SUFFIXES:
            for name in (f"{db_name}{suffix}", f"{db_name}{suffix}.sig"):
                local = os.path.join(args.pkg_dir, name)
                # repo-add writes .db/.files as symlinks; upload the real bytes
                real = os.path.realpath(local)
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, prefix + name)
                log(f"{repo}: uploaded {name}")
                # the same bytes under the pre-#62 name, so a client
                # configured as the README documents keeps resolving while
                # the prefixed section is adopted
                s3.upload_file(
                    real, bucket, prefix + name.replace(db_name, legacy, 1)
                )

    write_state(s3, bucket, log)

    log(f"published {len(packages)} package(s) to {args.branch}: "
        + ", ".join(f"{r}={len(g)}" for r, g in sorted(grouped.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
