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
from repo_common import DB_SUFFIXES, s3_client
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
    parser.add_argument("--db-name", default="manjaro-contrib")
    args = parser.parse_args()

    packages = sorted(glob.glob(os.path.join(args.pkg_dir, "*.pkg.tar.zst")))
    if not packages:
        log("no packages to publish")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    prefix = f"{args.branch}/{args.arch}/"
    db_file = os.path.join(args.pkg_dir, f"{args.db_name}.db.tar.gz")

    try:
        s3.download_file(bucket, prefix + f"{args.db_name}.db.tar.gz", db_file)
        log("downloaded existing database")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise
        log("no database yet, repo-add will create one")

    repo_add = ["repo-add", db_file, *packages]
    key = os.environ.get("GPG_KEYID")
    if key:
        # --sign writes manjaro-contrib.db.sig; without it a signed package
        # set still leaves the package list itself forgeable
        repo_add[1:1] = ["--sign", "--key", key]
    subprocess.run(repo_add, check=True)

    for pkg in packages:
        s3.upload_file(pkg, bucket, prefix + os.path.basename(pkg))
        log(f"uploaded {os.path.basename(pkg)}")
        sig = pkg + ".sig"
        if os.path.exists(sig):
            s3.upload_file(sig, bucket, prefix + os.path.basename(sig))

    prune_superseded(s3, bucket, prefix, [os.path.basename(p) for p in packages])

    for suffix in DB_SUFFIXES:
        for name in (f"{args.db_name}{suffix}", f"{args.db_name}{suffix}.sig"):
            local = os.path.join(args.pkg_dir, name)
            # repo-add writes .db/.files as symlinks; upload the real bytes
            real = os.path.realpath(local)
            if not os.path.exists(real):
                continue
            s3.upload_file(real, bucket, prefix + name)
            log(f"uploaded {name}")

    write_state(s3, bucket, log)

    log(f"published {len(packages)} package(s) to {args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
