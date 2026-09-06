#!/usr/bin/env python3
"""Regenerate a branch's databases from the packages present in the bucket.

check_repo reports drift; this is how it gets repaired. The packages on R2
are the truth - they are what clients download and what the signatures
cover - so the databases are rebuilt from them rather than patched.

Every operation that writes a database is incremental, which is how a
database can end up describing something other than the bucket. This is
the one that does not: it lists the branch, downloads what it finds, and
runs repo-add over the whole set.

Must run under the repo-publish concurrency lock; it rewrites the same
objects publish does. --dry-run reports what would change and writes
nothing.
"""

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile

from botocore.exceptions import ClientError
from repo_common import DB_SUFFIXES, list_packages, s3_client
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def entry_count(path: str) -> int:
    """How many packages a database archive describes."""
    with tarfile.open(path, mode="r:gz") as tar:
        return sum(1 for name in tar.getnames() if name.endswith("/desc"))


def rebuild(
    s3, bucket: str, branch: str, arch: str, db_name: str, dry_run: bool
) -> int:
    """Rebuild one branch's databases. Returns the package count."""
    prefix = f"{branch}/{arch}/"
    names = list_packages(s3, bucket, prefix)
    if not names:
        log(f"{branch}/{arch}: no packages, nothing to rebuild")
        return 0

    with tempfile.TemporaryDirectory() as workdir:
        paths = []
        for name in names:
            dest = os.path.join(workdir, name)
            s3.download_file(bucket, prefix + name, dest)
            paths.append(dest)
            # repo-add reads the signature from beside the package; without
            # it the rebuilt database would silently lose every %PGPSIG%
            try:
                s3.download_file(bucket, prefix + name + ".sig", dest + ".sig")
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                    raise
                log(f"  {name}: no signature on R2")

        db_file = os.path.join(workdir, f"{db_name}.db.tar.gz")
        cmd = ["repo-add", "--include-sigs", db_file, *paths]
        # a dry run uploads nothing, so it must not need a keyring either -
        # signing here would fail on a runner that never imported the key
        key = None if dry_run else os.environ.get("GPG_KEYID")
        if key:
            cmd[1:1] = ["--sign", "--key", key]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

        files_file = os.path.join(workdir, f"{db_name}.files.tar.gz")
        log(
            f"{branch}/{arch}: rebuilt from {len(names)} package(s)"
            f" -> db={entry_count(db_file)} files={entry_count(files_file)}"
        )

        if dry_run:
            log(f"{branch}/{arch}: dry run, nothing uploaded")
            return len(names)

        for suffix in DB_SUFFIXES:
            for fname in (f"{db_name}{suffix}", f"{db_name}{suffix}.sig"):
                # repo-add writes .db/.files as symlinks to the archives
                real = os.path.realpath(os.path.join(workdir, fname))
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, prefix + fname)
                log(f"  uploaded {fname}")

    return len(names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branches",
        required=True,
        help="comma-separated branches to rebuild",
    )
    parser.add_argument("--arches", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be rebuilt without uploading",
    )
    args = parser.parse_args()

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()

    wrote = False
    for branch in [b.strip() for b in args.branches.split(",") if b.strip()]:
        for arch in [a.strip() for a in args.arches.split(",") if a.strip()]:
            if rebuild(s3, bucket, branch, arch, args.db_name, args.dry_run):
                wrote = wrote or not args.dry_run

    if wrote:
        # the databases changed, so every poller needs to know
        write_state(s3, bucket, log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
