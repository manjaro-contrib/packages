#!/usr/bin/env python3
"""Remove a package from one or more branch repositories on R2.

Deletes every version of the named packages (plus signatures) from each
branch/arch prefix and drops their entries from the pacman database.
Runs under the repo-publish concurrency lock — it rewrites the database.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from botocore.exceptions import ClientError
from catalog import REPOS
from repo_common import ARCHES, DB_SUFFIXES, db_name_for, prefix_for, s3_client
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




def pkgname_of(filename: str) -> str | None:
    """The package name in `name-ver-rel-arch.pkg.tar.zst[.sig]`, else None.

    Neither ver, rel nor arch may contain a dash while package names may, so
    stripping exactly three trailing dash-separated fields is what keeps
    `foo` from also matching `foo-git`.
    """
    body = filename.removesuffix(".sig").removesuffix(".pkg.tar.zst")
    head, sep, _arch = body.rpartition("-")
    if not sep:
        return None
    head, sep, _rel = head.rpartition("-")
    if not sep:
        return None
    name, sep, _ver = head.rpartition("-")
    return name if sep else None


def belongs_to(filename: str, pkgname: str) -> bool:
    return pkgname_of(filename) == pkgname


def artifacts_for(s3, bucket: str, prefix: str, pkgname: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + pkgname + "-"):
        for obj in page.get("Contents", []):
            name = obj["Key"].removeprefix(prefix)
            if not name.endswith((".pkg.tar.zst", ".pkg.tar.zst.sig")):
                continue
            if belongs_to(name, pkgname):
                keys.append(obj["Key"])
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packages", required=True, help="comma-separated package names"
    )
    parser.add_argument(
        "--branches",
        default="unstable,testing,stable",
        help="comma-separated branches to remove from",
    )
    parser.add_argument("--arches", default=",".join(ARCHES))
    args = parser.parse_args()

    names = [n.strip() for n in args.packages.split(",") if n.strip()]
    if not names:
        log("no package names given")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    removed_any = False

    for branch in [b.strip() for b in args.branches.split(",") if b.strip()]:
        for arch in [a.strip() for a in args.arches.split(",") if a.strip()]:
            # a removal names a package, not a repository, so every
            # repository is searched: the caller pulling a broken package
            # should not have to know which database carries it
            for repo in REPOS:
                prefix = prefix_for(branch, arch, repo)
                db_name = db_name_for(repo)
                with tempfile.TemporaryDirectory() as workdir:
                    db_file = os.path.join(workdir, f"{db_name}.db.tar.gz")
                    # .files must come along: repo-remove rewrites whichever
                    # databases are present and leaves the absent one stale
                    for suffix in (".db.tar.gz", ".files.tar.gz"):
                        local = os.path.join(workdir, f"{db_name}{suffix}")
                        try:
                            s3.download_file(
                                bucket, prefix + f"{db_name}{suffix}", local
                            )
                        except ClientError as e:
                            if e.response["Error"]["Code"] not in (
                                "NoSuchKey",
                                "404",
                            ):
                                raise
                    if not os.path.exists(db_file):
                        log(f"{branch}/{arch}: no database, skipping")
                        continue

                    repo_remove = ["repo-remove", db_file, *names]
                    key = os.environ.get("GPG_KEYID")
                    if key:
                        # the rewritten database needs a fresh signature
                        repo_remove[1:1] = ["--sign", "--key", key]
                    subprocess.run(repo_remove, check=False)

                    for name in names:
                        for key in artifacts_for(s3, bucket, prefix, name):
                            s3.delete_object(Bucket=bucket, Key=key)
                            log(f"{branch}/{arch}: deleted {key.removeprefix(prefix)}")
                            removed_any = True

                    for suffix in DB_SUFFIXES:
                        for name in (
                            f"{db_name}{suffix}",
                            f"{db_name}{suffix}.sig",
                        ):
                            local = os.path.join(workdir, name)
                            real = os.path.realpath(local)
                            if not os.path.exists(real):
                                continue
                            s3.upload_file(real, bucket, prefix + name)
                    log(f"{branch}/{repo}/{arch}: database updated")

    if not removed_any:
        log("no matching packages found in any branch")
        return 1

    write_state(s3, bucket, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
