"""Shared pieces of the repository layout: the branch flow and bucket access."""

import os

import boto3

DB_SUFFIXES = [".db", ".db.tar.gz", ".files", ".files.tar.gz"]

# Manjaro's lifecycle flows one way: a package must age through each branch.
# Promoting backwards, or skipping a stage, would put binaries in stable
# that no one ran in testing.
FLOW = {"testing": "unstable", "stable": "testing"}


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_packages(s3, bucket: str, prefix: str) -> list[str]:
    """Every .pkg.tar.zst filename under a prefix, without the prefix."""
    names = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].removeprefix(prefix)
            if name.endswith(".pkg.tar.zst"):
                names.append(name)
    return names
