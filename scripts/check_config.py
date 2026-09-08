#!/usr/bin/env python3
"""Validate packages.yml and the branch manifests before anything reads them.

These two files are the authority for what gets built and what each branch
carries, and every script trusts their shape. A typo is not caught by
review reliably - `repo: extras` looks right - and the failure surfaces
later as a package published into a database nobody configured, or a
promotion that silently skips an entry.

Checked here rather than in each consumer so a bad file is rejected once,
on the pull request that introduces it.
"""

import argparse
import pathlib
import re
import sys

import yaml
from catalog import DEFAULT_REPO, REPOS

# name-version-release, the form propose_promotion writes into a manifest
VERSION = re.compile(r"^[\w.+]+-[\w.+]+$")
# pacman package names: no uppercase, no spaces, no leading dash
PKGNAME = re.compile(r"^[a-z\d][a-z\d@._+-]*$")

ENTRY_FIELDS = {"upstream", "maintainers", "repo", "override", "floating"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def check_packages(path: pathlib.Path) -> list[str]:
    """Every problem in packages.yml."""
    problems = []
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict) or "packages" not in doc:
        return [f"{path}: no top-level 'packages' key"]
    extra_keys = set(doc) - {"packages"}
    if extra_keys:
        problems.append(f"{path}: unexpected top-level keys {sorted(extra_keys)}")

    packages = doc["packages"] or {}
    if not isinstance(packages, dict):
        return [f"{path}: 'packages' is not a mapping"]

    for name, entry in packages.items():
        where = f"{path}: {name}"
        if not PKGNAME.match(str(name)):
            problems.append(f"{where}: not a valid package name")
        if entry is None:
            # a bare entry is legitimate; catalog.load normalises it
            continue
        if not isinstance(entry, dict):
            problems.append(f"{where}: entry is not a mapping")
            continue

        unknown = set(entry) - ENTRY_FIELDS
        if unknown:
            # the failure this is really for: a misspelled field is
            # silently ignored by every reader
            problems.append(f"{where}: unknown field(s) {sorted(unknown)}")

        repo = entry.get("repo", DEFAULT_REPO)
        if repo not in REPOS:
            problems.append(f"{where}: repo {repo!r} is not one of {list(REPOS)}")

        maintainers = entry.get("maintainers")
        if maintainers is None:
            problems.append(f"{where}: no maintainers")
        elif not isinstance(maintainers, list) or not all(
            isinstance(m, str) and m for m in maintainers
        ):
            problems.append(f"{where}: maintainers is not a list of names")
        elif not maintainers:
            # sync_codeowners would write a CODEOWNERS naming nobody
            problems.append(f"{where}: maintainers is empty")

        upstream = entry.get("upstream")
        if upstream is not None and not str(upstream).startswith(
            ("https://", "http://")
        ):
            problems.append(f"{where}: upstream is not a url")

        for field in ("override", "floating"):
            reason = entry.get(field)
            if reason is not None and not str(reason).strip():
                problems.append(f"{where}: {field} is empty, so it explains nothing")

    return problems


def check_manifest(path: pathlib.Path, listed: set[str]) -> list[str]:
    """Every problem in one branch manifest."""
    problems = []
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict) or "packages" not in doc:
        return [f"{path}: no top-level 'packages' key"]

    packages = doc["packages"] or {}
    if not isinstance(packages, dict):
        return [f"{path}: 'packages' is not a mapping"]

    for name, version in packages.items():
        where = f"{path}: {name}"
        if not PKGNAME.match(str(name)):
            problems.append(f"{where}: not a valid package name")
        if not isinstance(version, str) or not VERSION.match(version):
            problems.append(f"{where}: {version!r} is not a version-release")
        # a manifest naming a package no longer built would fail to apply
        if name not in listed:
            problems.append(f"{where}: not listed in packages.yml")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.yml", type=pathlib.Path)
    parser.add_argument("--branches", default="branches", type=pathlib.Path)
    args = parser.parse_args()

    problems = check_packages(args.config)
    log(f"{args.config}: {len(problems) or 'no'} problem(s)")

    listed = set(yaml.safe_load(args.config.read_text())["packages"] or {})
    for manifest in sorted(args.branches.glob("*.yml")):
        found = check_manifest(manifest, listed)
        log(f"{manifest}: {len(found) or 'no'} problem(s)")
        problems += found

    for problem in problems:
        log(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
