"""Shared pieces of the repository layout: the branch flow and bucket access."""

import os

import boto3

DB_SUFFIXES = [".db", ".db.tar.gz", ".files", ".files.tar.gz"]

# every architecture published. "any" packages are not listed: they
# live in each arch prefix rather than one of their own.
#
# aarch64 is gone entirely: building stopped in #64, and the frozen tree it
# left behind has since been withdrawn from every branch. Keeping it here
# would make check_repo walk nine prefixes that do not exist and rebuild_db
# publish empty databases for an architecture nobody fetches. The layout is
# arch-aware throughout, so re-adding it is this line plus build-publish.
ARCHES = ("x86_64",)

# What a contrib section is called in pacman.conf. A database is fetched by
# its section name, so sharing [extra] with the distribution is what made
# pacman discard one of them - see db_name_for.
DB_PREFIX = "contrib-"


def db_name_for(repo: str) -> str:
    """The database filename stem for a repository.

    Named after the repository *and* prefixed: pacman keeps one database
    per section name and takes the first server that answers, so a contrib
    [extra] beside the distribution's is discarded with "database already
    registered" and whichever survives hides the other. That was harmless
    while nothing depended on our own output, and became a hard blocker the
    moment multilib carried lib32-* packages that other lib32-* packages
    need - the build could not see what we had just published (#62).

    A distinct name lets both be configured at once:

        [contrib-multilib]
        Server = .../unstable/multilib/$arch

    publish writes the old name too, so a client configured the documented
    way keeps working; see also_publishes_as.
    """
    return f"{DB_PREFIX}{repo}"


def legacy_db_name_for(repo: str) -> str:
    """The unprefixed name, still published so existing clients keep working."""
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
