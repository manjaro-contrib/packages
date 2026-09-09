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
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile

from botocore.exceptions import ClientError
from catalog import REPOS, listed_package_names
from repo_common import (
    ARCHES,
    DB_SUFFIXES,
    db_name_for,
    legacy_db_name_for,
    list_packages,
    prefix_for,
    s3_client,
)
from repo_remove import pkgname_of
from repo_state import write_state

# %FIELD%\nvalue in a pacman desc entry
FIELD = re.compile(r"%([A-Z0-9]+)%\n([^\n]*)")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def bases(s3, bucket: str, prefix: str, db_name: str) -> dict[str, str]:
    """Each published package mapped to the pkgbase that produced it.

    A split package publishes members the config never names -
    packages-extra-pamac builds pamac-gtk - and the database records the
    relationship in %BASE%, so it needs no second list to drift from.
    """
    try:
        payload = s3.get_object(
            Bucket=bucket, Key=f"{prefix}{db_name}.db.tar.gz"
        )["Body"].read()
    except ClientError:
        return {}
    mapping = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith("/desc"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            desc = handle.read().decode()
            fields = dict(FIELD.findall(desc))
            if "NAME" in fields:
                mapping[fields["NAME"]] = fields.get("BASE", fields["NAME"])
    return mapping


def orphans(
    s3, bucket: str, prefix: str, allowed: set[str], base_of: dict[str, str]
) -> dict[str, list[str]]:
    """Published artifacts whose package is no longer listed, by name."""
    found: dict[str, list[str]] = {}
    for filename in list_packages(s3, bucket, prefix):
        name = pkgname_of(filename)
        if not name:
            continue
        # a split member is authorised by its pkgbase being listed
        if name in allowed or base_of.get(name, name) in allowed:
            continue
        found.setdefault(name, []).append(filename)
    return found


def withdraw(
    s3,
    bucket: str,
    branch: str,
    arch: str,
    repo: str,
    allowed: set[str],
    dry_run: bool,
) -> list[str]:
    """Delete unlisted packages from one branch. Returns the names removed."""
    prefix = prefix_for(branch, arch, repo)
    db_name = db_name_for(repo)
    found = orphans(s3, bucket, prefix, allowed, bases(s3, bucket, prefix, db_name))
    if not found:
        log(f"{branch}/{repo}/{arch}: nothing to withdraw")
        return []

    for name, files in sorted(found.items()):
        log(f"{branch}/{repo}/{arch}: {name} is published but not listed"
            f" ({len(files)} artifact(s))")
    if dry_run:
        log(f"{branch}/{repo}/{arch}: dry run, nothing deleted")
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
            log(f"{branch}/{repo}/{arch}: no database, skipping")
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
            log(f"{branch}/{repo}/{arch}: withdrew {name}")

        legacy = legacy_db_name_for(repo)
        for suffix in DB_SUFFIXES:
            for fname in (f"{db_name}{suffix}", f"{db_name}{suffix}.sig"):
                # repo-remove writes .db/.files as symlinks to the archives
                real = os.path.realpath(os.path.join(workdir, fname))
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, prefix + fname)
                # the same bytes under the pre-#62 name, so a client
                # configured before the split keeps resolving
                s3.upload_file(
                    real, bucket, prefix + fname.replace(db_name, legacy, 1)
                )

    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument("--branch", default="unstable")
    parser.add_argument("--arches", default=",".join(ARCHES))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be withdrawn without deleting",
    )
    args = parser.parse_args()

    # package names, not repository keys: packages-core-pacman-mirrors
    # builds pacman-mirrors, and comparing published filenames against the
    # keys withdrew every package whose repository is not named after it
    allowed = listed_package_names(args.config)
    if not allowed:
        # an empty list would withdraw everything; a truncated config is
        # far likelier than a deliberate wipe
        log(f"{args.config} lists no packages, refusing to withdraw everything")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()

    removed = []
    for arch in [a.strip() for a in args.arches.split(",") if a.strip()]:
        for repo in REPOS:
            removed += withdraw(
                s3, bucket, args.branch, arch, repo, allowed, args.dry_run
            )

    if removed and not args.dry_run:
        write_state(s3, bucket, log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
