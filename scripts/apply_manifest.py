#!/usr/bin/env python3
"""Reconcile a branch on R2 to its manifest.

The manifest is the desired state; this computes the difference against the
bucket and applies it. Packages named at a version the branch lacks are
copied server-side from the source branch, packages no longer named are
withdrawn, and the pacman database is rebuilt to match.

Idempotent: applying an unchanged manifest performs no writes, so it is
safe to run on every merge.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from botocore.exceptions import ClientError
from catalog import REPOS, repo_of
from catalog import load as load_catalog
from manifest import load
from release_store import download as fetch_release
from release_store import get_release
from repo_common import DB_SUFFIXES, FLOW, list_packages, prefix_for, s3_client
from repo_remove import pkgname_of
from repo_state import write_state


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def artifact_for(s3, bucket: str, prefix: str, name: str, version: str) -> str | None:
    """The filename carrying `name` at `version` under a prefix, if present."""
    for key in list_packages(s3, bucket, prefix):
        if pkgname_of(key) != name:
            continue
        # name-version-arch.pkg.tar.zst: strip the name and the arch field
        rest = key[len(name) + 1 :].removesuffix(".pkg.tar.zst")
        if rest.rsplit("-", 1)[0] == version:
            return key
    return None


def plan(s3, bucket: str, src_prefix: str, dst_prefix: str, wanted: dict) -> dict:
    """Split the manifest into what must be copied, removed or left alone."""
    present = {}
    for key in list_packages(s3, bucket, dst_prefix):
        name = pkgname_of(key)
        if name:
            present.setdefault(name, []).append(key)

    add, remove, keep, missing = [], [], [], []
    for name, version in wanted.items():
        current = present.get(name, [])
        match = artifact_for(s3, bucket, dst_prefix, name, version)
        if match:
            keep.append(match)
            remove += [k for k in current if k != match]
            continue
        source = artifact_for(s3, bucket, src_prefix, name, version)
        if source is None:
            missing.append((name, version))
            continue
        add.append(source)
        remove += current

    for name, keys in present.items():
        if name not in wanted:
            remove += keys
    return {"add": add, "remove": remove, "keep": keep, "missing": missing}


def restore(
    s3, bucket: str, org: str, src_prefix: str, missing: list, token: str
) -> list[str]:
    """Put superseded versions back into the source branch from their releases.

    publish prunes a superseded build from the bucket, but every build is
    also kept as a release asset on its own package repository. Without
    this, reverting a manifest to an older version - the documented way to
    roll back - fails the moment that version has been superseded, which
    is exactly when a rollback is wanted.

    Returns the source keys now available to copy.
    """
    recovered = []
    for name, version in list(missing):
        repo = f"{org}/{name}"
        release = get_release(repo, version, token)
        if release is None:
            log(f"{name} {version}: no release on {repo}")
            continue
        # download() wants the pacman filenames and fetches each .sig
        # alongside, so ask only for the packages the release carries
        packages = [
            a["name"]
            for a in release.get("assets", [])
            if a["name"].endswith(".pkg.tar.zst")
        ]
        if not packages:
            log(f"{name} {version}: release carries no package")
            continue
        with tempfile.TemporaryDirectory() as workdir:
            try:
                fetch_release(repo, version, packages, workdir, token)
            except RuntimeError as e:
                log(f"{name} {version}: cannot restore ({e})")
                continue
            for filename in sorted(os.listdir(workdir)):
                s3.upload_file(
                    os.path.join(workdir, filename), bucket, src_prefix + filename
                )
                log(f"{name} {version}: restored {filename} to {src_prefix}")
        recovered.append((name, version))
    return recovered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--org",
        default="manjaro-contrib",
        help="organisation holding the package repositories a superseded"
        " version can be restored from",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the plan without writing"
    )
    args = parser.parse_args()

    source = FLOW.get(args.branch)
    if source is None:
        log(f"{args.branch} is not a promotion target")
        return 1

    wanted = load(args.branch, args.root)
    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    catalog = load_catalog()

    # each repository is promoted on its own: a database covers one
    # repository, so a package moving between branches only ever affects
    # the database of the repository it belongs to
    failed = False
    changed = False
    for repo in REPOS:
        members = {
            name: version
            for name, version in wanted.items()
            if repo_of(catalog.get(name, {})) == repo
        }
        src_prefix = prefix_for(source, args.arch, repo)
        dst_prefix = prefix_for(args.branch, args.arch, repo)

        p = plan(s3, bucket, src_prefix, dst_prefix, members)

        # a version the source branch no longer carries may still exist as a
        # release asset; restoring it is what makes reverting a manifest work
        if p["missing"] and not args.dry_run:
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                if restore(s3, bucket, args.org, src_prefix, p["missing"], token):
                    p = plan(s3, bucket, src_prefix, dst_prefix, members)
            else:
                log("GITHUB_TOKEN unset, cannot restore from the release store")

        for kind in ("add", "remove"):
            for key in p[kind]:
                log(f"{repo}: {kind}: {key}")
        if p["missing"]:
            for name, version in p["missing"]:
                log(
                    f"ERROR: {name} {version} is not in {source}/{repo}"
                    " or its release store"
                )
            failed = True
            continue
        if not p["add"] and not p["remove"]:
            log(f"{args.branch}/{repo} already matches its manifest")
            continue
        if args.dry_run:
            log(
                f"{repo}: would add {len(p['add'])}"
                f" and remove {len(p['remove'])} object(s)"
            )
            continue

        for name in p["add"]:
            for suffix in ("", ".sig"):
                try:
                    s3.copy_object(
                        Bucket=bucket,
                        CopySource={
                            "Bucket": bucket,
                            "Key": src_prefix + name + suffix,
                        },
                        Key=dst_prefix + name + suffix,
                    )
                except ClientError as e:
                    if suffix == "" or e.response["Error"]["Code"] not in (
                        "NoSuchKey",
                        "404",
                    ):
                        raise
            log(f"{repo}: copied {name}")

        for key in p["remove"]:
            for suffix in ("", ".sig"):
                try:
                    s3.delete_object(Bucket=bucket, Key=dst_prefix + key + suffix)
                except ClientError as e:
                    if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                        raise
            log(f"{repo}: withdrew {key}")

        with tempfile.TemporaryDirectory() as workdir:
            # the database is rebuilt from scratch: repo-add cannot express a
            # removal and an addition in one consistent step
            paths = []
            for name in p["add"] + p["keep"]:
                dest = os.path.join(workdir, name)
                s3.download_file(bucket, dst_prefix + name, dest)
                paths.append(dest)
                # --include-sigs reads the signature from beside the package,
                # so without fetching it the database would carry no %PGPSIG%
                try:
                    s3.download_file(
                        bucket, dst_prefix + name + ".sig", dest + ".sig"
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                        raise

            db_file = os.path.join(workdir, f"{args.db_name}.db.tar.gz")
            if paths:
                # --include-sigs records each package's signature in the
                # database, matching every Arch and Manjaro repository;
                # --sign signs the database itself
                cmd = ["repo-add", "--include-sigs", db_file, *paths]
                key = os.environ.get("GPG_KEYID")
                if key:
                    cmd[1:1] = ["--sign", "--key", key]
                subprocess.run(cmd, check=True)

            for suffix in DB_SUFFIXES:
                for fname in (
                    f"{args.db_name}{suffix}",
                    f"{args.db_name}{suffix}.sig",
                ):
                    real = os.path.realpath(os.path.join(workdir, fname))
                    if not os.path.exists(real):
                        continue
                    s3.upload_file(real, bucket, dst_prefix + fname)
                    log(f"{repo}: uploaded {fname}")
        changed = True

    if failed:
        return 1
    if not changed:
        return 0

    write_state(s3, bucket, log)
    log(f"{args.branch} now carries {len(wanted)} package(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
