#!/usr/bin/env python3
"""Re-sign every package and database in a branch with the current key.

Rotating the signing key leaves the published artifacts signed by the old
one, so `SigLevel = Required` rejects them until each signature is
replaced. The packages themselves are untouched - only the detached
signatures and the database are rewritten, so nothing needs rebuilding.

Runs under the repo-publish lock: it rewrites the database.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from botocore.exceptions import ClientError
from repo_common import DB_SUFFIXES, list_packages, s3_client
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sign(path: str, key: str) -> str:
    """Replace the detached signature beside a file."""
    sig = path + ".sig"
    if os.path.exists(sig):
        os.unlink(sig)
    subprocess.run(
        ["gpg", "--batch", "--yes", "--detach-sign", "--local-user", key, path],
        check=True,
    )
    return sig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    key = os.environ.get("GPG_KEYID")
    if not key:
        log("GPG_KEYID is required")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    prefix = f"{args.branch}/{args.arch}/"

    packages = list_packages(s3, bucket, prefix)
    if not packages:
        log(f"{args.branch} holds no packages")
        return 0
    log(f"{args.branch}: {len(packages)} package(s) to re-sign")
    if args.dry_run:
        return 0

    with tempfile.TemporaryDirectory() as workdir:
        paths = []
        for name in packages:
            local = os.path.join(workdir, name)
            s3.download_file(bucket, prefix + name, local)
            sig = sign(local, key)
            s3.upload_file(sig, bucket, prefix + os.path.basename(sig))
            paths.append(local)
            log(f"re-signed {name}")

        # the database is rebuilt so its own signature matches the new key
        db_file = os.path.join(workdir, f"{args.db_name}.db.tar.gz")
        subprocess.run(
            ["repo-add", "--sign", "--key", key, db_file, *paths], check=True
        )
        for suffix in DB_SUFFIXES:
            for fname in (f"{args.db_name}{suffix}", f"{args.db_name}{suffix}.sig"):
                real = os.path.realpath(os.path.join(workdir, fname))
                if not os.path.exists(real):
                    continue
                s3.upload_file(real, bucket, prefix + fname)
                log(f"uploaded {fname}")

        # a stale .sig from the old key would otherwise linger for any
        # database suffix repo-add no longer produces
        for suffix in DB_SUFFIXES:
            local = os.path.realpath(os.path.join(workdir, f"{args.db_name}{suffix}"))
            if os.path.exists(local):
                continue
            try:
                s3.delete_object(
                    Bucket=bucket, Key=prefix + f"{args.db_name}{suffix}.sig"
                )
            except ClientError:
                pass

    write_state(s3, bucket, log)
    log(f"{args.branch}: re-signed with {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
