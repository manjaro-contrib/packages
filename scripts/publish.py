#!/usr/bin/env python3
"""Publish freshly built packages to the branch prefix on R2.

Downloads the branch database (initializing a new one if absent), registers
every .pkg.tar.zst in the given directory with repo-add, then uploads the
packages, signatures, and database files. Must run under the workflow-level
concurrency lock — two concurrent publishes would clobber the database.
"""

import argparse
import functools
import glob
import os
import subprocess
import sys

from botocore.exceptions import ClientError
from repo_common import DB_SUFFIXES, s3_client
from repo_remove import artifacts_for, pkgname_of, version_of
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




def _versions_newest_last(names: list[str], pkgname: str) -> list[str]:
    """Order artifact filenames oldest first, by pacman's own comparison.

    Sorting the strings would put 1.9.0 after 1.10.0, so the wrong file
    would be treated as newest and kept. vercmp ships with pacman and is
    the same comparison the client uses.
    """
    def compare(a: str, b: str) -> int:
        va, vb = version_of(a, pkgname), version_of(b, pkgname)
        out = subprocess.run(
            ["vercmp", va, vb], capture_output=True, text=True, check=True
        )
        return int(out.stdout.strip())

    return sorted(names, key=functools.cmp_to_key(compare))


def prune_superseded(
    s3, bucket: str, prefix: str, published: list[str], keep_previous: int = 1
) -> None:
    """Drop older versions of just-published packages from the bucket.

    repo-add's --remove only unlinks local files, and the previous versions
    exist solely on R2, so they would accumulate forever without this.

    The most recent `keep_previous` superseded versions stay, because a
    client that read the database a moment ago is still fetching the
    version it named. Deleting it immediately turns that download into a
    404 partway through - pacman reports a corrupt or missing package for
    something that existed when it looked. The database only ever points at
    the new version, so the old files are unreferenced, just not yet gone.
    """
    keep = set(published)
    for filename in published:
        superseded = []
        for key in artifacts_for(s3, bucket, prefix, pkgname_of(filename)):
            name = key.removeprefix(prefix)
            if name in keep or name.removesuffix(".sig") in keep:
                continue
            superseded.append(key)

        # group by version so a package and its signature are kept or
        # dropped together; a package without its .sig fails SigLevel
        by_version = {}
        for key in superseded:
            name = key.removeprefix(prefix).removesuffix(".sig")
            by_version.setdefault(name, []).append(key)

        # newest last, so the tail is what a client may still be fetching
        order = _versions_newest_last(list(by_version), pkgname_of(filename))
        for name in order[: max(0, len(order) - keep_previous)]:
            for key in by_version[name]:
                s3.delete_object(Bucket=bucket, Key=key)
                log(f"pruned superseded {key.removeprefix(prefix)}")
        for name in order[max(0, len(order) - keep_previous) :]:
            log(f"kept superseded {name} for in-flight downloads")


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

    # both databases must be fetched: repo-add updates whichever files it
    # finds and creates the rest from scratch, so publishing with only
    # .db present rebuilt .files from the current build alone, dropping
    # every other package's file list
    for suffix in (".db.tar.gz", ".files.tar.gz"):
        local = os.path.join(args.pkg_dir, f"{args.db_name}{suffix}")
        try:
            s3.download_file(bucket, prefix + f"{args.db_name}{suffix}", local)
            log(f"downloaded existing {args.db_name}{suffix}")
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                raise
            log(f"no {args.db_name}{suffix} yet, repo-add will create one")

    # --include-sigs records each package's signature in the database, as
    # every Arch and Manjaro repository does; tooling that reads a database
    # expects the field, and pacman -Si can then report a package's signer
    # without fetching it. --sign is unrelated: it signs the database
    # itself, without which the package list is forgeable.
    repo_add = ["repo-add", "--include-sigs", db_file, *packages]
    key = os.environ.get("GPG_KEYID")
    if key:
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
