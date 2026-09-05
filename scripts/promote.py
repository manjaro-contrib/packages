#!/usr/bin/env python3
"""Promote built packages between repo branches via server-side R2 copies.

Copies .pkg.tar.zst objects (and their .sig if present) from the source
branch prefix to the target prefix without egress, then rebuilds the target
branch's pacman database locally and re-uploads it.
"""

import argparse
import os
import subprocess
import sys
import tempfile

import boto3
from botocore.exceptions import ClientError
from repo_state import write_state

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
    args = parser.parse_args()

    if args.source_branch == args.target_branch:
        log("source and target branch must differ")
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

        subprocess.run(["repo-add", db_file, *pkg_paths], check=True)

        for suffix in DB_SUFFIXES:
            local = os.path.join(workdir, f"{args.db_name}{suffix}")
            # repo-add writes .db/.files as symlinks; upload the real bytes
            real = os.path.realpath(local)
            if not os.path.exists(real):
                continue
            s3.upload_file(real, bucket, dst_prefix + f"{args.db_name}{suffix}")
            log(f"uploaded {args.db_name}{suffix}")

    write_state(s3, bucket, log)

    log(f"promoted {len(copied)} package(s) to {args.target_branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
