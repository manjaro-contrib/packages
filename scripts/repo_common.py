"""Shared pieces of the repository layout: the branch flow and bucket access."""

import os

import boto3

DB_SUFFIXES = [".db", ".db.tar.gz", ".files", ".files.tar.gz"]

# every architecture published. "any" packages are not listed: they
# live in each arch prefix rather than one of their own.
#
# aarch64 is still here although build-publish.yml no longer builds for it:
# the packages built before it was switched off are still in the bucket and
# still served, so check_repo, rebuild_db and repo_remove have to keep
# seeing them. Dropping it here would leave that tree unverifiable and
# unremovable rather than merely frozen.
ARCHES = ("x86_64", "aarch64")

def db_name_for(repo: str) -> str:
    """The database filename stem for a repository.

    Upstream names each database after its repository - core.db, extra.db -
    and pacman derives the filename from the section name in pacman.conf,
    so [extra] fetches extra.db and nothing else. A single name shared
    across repositories would be unreadable by a stock client.
    """
    return repo


def prefix_for(branch: str, arch: str, repo: str) -> str:
    """Where a repository's objects live for a branch and architecture.

    Membership is not recorded in package metadata - a `desc` entry has no
    %REPO% field - so which database a package appears in *is* its
    repository. That makes the prefix the only place membership exists in
    the bucket, and every script has to agree on it.
    """
    return f"{branch}/{repo}/{arch}/"


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
