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

import boto3
from botocore.exceptions import ClientError

DB_SUFFIXES = [".db", ".db.tar.gz", ".files", ".files.tar.gz"]


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

    subprocess.run(["repo-add", "--remove", db_file, *packages], check=True)

    for pkg in packages:
        s3.upload_file(pkg, bucket, prefix + os.path.basename(pkg))
        log(f"uploaded {os.path.basename(pkg)}")
        sig = pkg + ".sig"
        if os.path.exists(sig):
            s3.upload_file(sig, bucket, prefix + os.path.basename(sig))

    for suffix in DB_SUFFIXES:
        local = os.path.join(args.pkg_dir, f"{args.db_name}{suffix}")
        # repo-add writes .db/.files as symlinks; upload the real bytes
        real = os.path.realpath(local)
        if not os.path.exists(real):
            continue
        s3.upload_file(real, bucket, prefix + f"{args.db_name}{suffix}")
        log(f"uploaded {args.db_name}{suffix}")

    log(f"published {len(packages)} package(s) to {args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
