#!/usr/bin/env python3
"""Verify that a published branch actually resolves.

The state hash says whether a branch changed, not whether it works: a
partial upload produces a new hash just as a good one does. So this checks
the invariants a pacman client depends on, for each branch:

- every database entry names a file that exists, at the size recorded
- every published package has a database entry, or clients cannot see it
- the .files database lists the same packages as .db
- every package carries the detached signature SigLevel = Required needs
- the databases themselves are signed

Read-only, so it is safe to run against a live branch at any time.
Findings are kept in a labelled issue rather than only failing a run, so
drift that appears between publishes still gets noticed.
"""

import argparse
import io
import os
import re
import sys
import tarfile

from botocore.exceptions import ClientError
from gh_api import keep_issue
from repo_common import s3_client
from repo_state import BRANCHES

# a database entry records its own filename and size; both must match
FIELD = re.compile(r"%([A-Z0-9]+)%\n([^\n]*)")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def db_entries(payload: bytes) -> dict[str, dict[str, str]]:
    """Parse a pacman database into {pkgname-pkgver-pkgrel: fields}."""
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith("/desc"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            desc = handle.read().decode()
            entries[member.name.removesuffix("/desc")] = dict(
                FIELD.findall(desc)
            )
    return entries


def objects(s3, bucket: str, prefix: str) -> dict[str, int]:
    """Every object under a prefix, mapped to its size."""
    found = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found[obj["Key"].removeprefix(prefix)] = obj["Size"]
    return found


def check_branch(
    s3, bucket: str, branch: str, arch: str, db_name: str
) -> list[str]:
    """Every inconsistency found in one branch."""
    prefix = f"{branch}/{arch}/"
    problems = []

    present = objects(s3, bucket, prefix)
    if not present:
        # an empty branch is legitimate: nothing has been promoted into it
        return []

    payload = fetch(s3, bucket, f"{prefix}{db_name}.db.tar.gz")
    if payload is None:
        return [f"{branch}: packages present but no database"]
    entries = db_entries(payload)

    for suffix in (".db.tar.gz", ".files.tar.gz"):
        if f"{db_name}{suffix}.sig" not in present:
            problems.append(f"{branch}: {db_name}{suffix} is unsigned")

    listed = set()
    for name, fields in sorted(entries.items()):
        filename = fields.get("FILENAME")
        if not filename:
            problems.append(f"{branch}: {name} has no %FILENAME%")
            continue
        listed.add(filename)
        if filename not in present:
            problems.append(f"{branch}: {name} names a missing file")
            continue
        recorded = fields.get("CSIZE")
        if recorded and int(recorded) != present[filename]:
            problems.append(
                f"{branch}: {filename} is {present[filename]} bytes,"
                f" database says {recorded}"
            )
        if f"{filename}.sig" not in present:
            problems.append(f"{branch}: {filename} has no signature")

    # a package nobody can install is as broken as a missing one
    for name in sorted(present):
        if name.endswith(".pkg.tar.zst") and name not in listed:
            problems.append(f"{branch}: {name} is published but not in the database")

    files_payload = fetch(s3, bucket, f"{prefix}{db_name}.files.tar.gz")
    if files_payload is None:
        problems.append(f"{branch}: no .files database")
    else:
        files_entries = set(db_entries(files_payload))
        for missing in sorted(set(entries) - files_entries):
            problems.append(f"{branch}: {missing} is absent from .files")
        for extra in sorted(files_entries - set(entries)):
            problems.append(f"{branch}: {extra} is in .files but not .db")

    return problems


LABEL = "repo-inconsistent"
TITLE = "Published repository is inconsistent"


def issue_body(problems: list[str]) -> str:
    """The issue text, or empty when the repository resolves cleanly."""
    if not problems:
        return ""
    lines = [
        "A published branch does not resolve. Every entry below breaks an",
        "invariant a pacman client depends on, so it is worth fixing before",
        "the next promotion carries it onward.",
        "",
    ]
    lines += [f"- {p}" for p in problems]
    lines += [
        "",
        (
            "<sub>Kept by `check-repo`; closed automatically once the"
            " repository resolves again.</sub>"
        ),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch",
        action="append",
        help="branch to check; repeatable, defaults to all of them",
    )
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--db-name", default="manjaro-contrib")
    parser.add_argument(
        "--issue",
        metavar="OWNER/REPO",
        help="keep the findings in a labelled issue on this repository",
    )
    args = parser.parse_args()

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()

    problems = []
    for branch in args.branch or BRANCHES:
        found = check_branch(s3, bucket, branch, args.arch, args.db_name)
        log(f"{branch}: {len(found) or 'no'} problem(s)")
        problems += found

    for problem in problems:
        log(f"  {problem}")

    if args.issue:
        result = keep_issue(
            args.issue,
            LABEL,
            TITLE,
            issue_body(problems),
            os.environ["GITHUB_TOKEN"],
        )
        if result is None:
            log("no issue needed")
        else:
            action, number = result
            log(f"issue #{number} {action}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
