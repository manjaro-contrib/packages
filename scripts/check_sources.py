#!/usr/bin/env python3
"""Fail a PKGBUILD that fetches its source from gitlab.manjaro.org.

Clones of that host fail often enough to break builds outright - four ISO
runs and the whole mhwd family were blocked by it in a single day, and
`git ls-remote` against it still times out. A package sourced from there
inherits that flakiness on *every* build, not once.

We mirror those repositories into this organization, so the fix is always
available: point the source at the mirror carrying the same revision. The
mirror is a faithful copy, so nothing about what gets built changes.

Only `source=` is checked. A `url=` naming gitlab.manjaro.org is metadata
describing where a package comes from - it is never fetched, and rewriting
it would misstate the package's origin.
"""

import argparse
import os
import re
import subprocess
import sys
import urllib.error

from catalog import load as load_catalog
from check_pkgver import fetch_pkgbuild

import patches

HOST = "gitlab.manjaro.org"
# the source array, whose entries makepkg actually fetches
SOURCE = re.compile(r"^\s*source(_\w+)?=\((.*?)\)", re.MULTILINE | re.DOTALL)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def expand(text: str, pkgbuild: str) -> str:
    """Substitute the plain scalar assignments a source entry can reference.

    udev-usb-sync writes `source=("git+${url}.git#tag=$pkgver")` with the
    host in `url`, so matching the source line alone missed it. Only simple
    top-level assignments are resolved - enough to see through the common
    `${url}` and `${_pkgbase}` indirection without evaluating the PKGBUILD.
    """
    for name, value in re.findall(
        r"^(\w+)=[\"']?([^\"'\n()]*)[\"']?$", pkgbuild, re.MULTILINE
    ):
        for form in (f"${{{name}}}", f"${name}"):
            text = text.replace(form, value)
    return text


def blocked_sources(pkgbuild: str) -> list[str]:
    """Every source entry fetched from the unreliable host."""
    found = []
    for m in SOURCE.finditer(pkgbuild):
        # a commented entry is never fetched: calamares keeps its previous
        # gitlab url commented out above the live one
        live = "\n".join(
            line.split("#", 1)[0] if line.lstrip().startswith("#") else line
            for line in m.group(2).splitlines()
        )
        for entry in live.split():
            resolved = expand(entry, pkgbuild)
            if HOST in resolved:
                found.append(entry.strip("\"'"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="GitHub organization")
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument(
        "--repo", action="append", help="check one repository; repeatable"
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    repos = args.repo or sorted(load_catalog(args.config))
    problems = []
    for repo in repos:
        try:
            pkgbuild = fetch_pkgbuild(args.org, repo, token)
        except (urllib.error.HTTPError, RuntimeError) as e:
            log(f"{repo}: {e}")
            return 1
        if pkgbuild is None:
            log(f"{repo}: no PKGBUILD on the default branch, skipping")
            continue
        # a local patch is how a force-pushed mirror gets this fix, so judge
        # what will actually be built
        try:
            pkgbuild = patches.apply_to_text(repo, "PKGBUILD", pkgbuild)
        except subprocess.CalledProcessError:
            log(f"{repo}: local patches do not apply")
            problems.append((repo, ["patches do not apply"]))
            continue
        blocked = blocked_sources(pkgbuild)
        if blocked:
            problems.append((repo, blocked))
            log(f"{repo}: sourced from {HOST}")
            for entry in blocked:
                log(f"    {entry}")
        else:
            log(f"{repo}: sources ok")

    if not problems:
        log(f"checked {len(repos)} package(s), none sourced from {HOST}")
        return 0

    log("")
    log(f"{len(problems)} package(s) fetch their source from {HOST}:")
    for repo, _ in problems:
        log(f"  {repo}")
    log("")
    log("Clones of that host fail often enough to break builds outright, and a")
    log("package sourced from it inherits that on every build. We mirror those")
    log("repositories into this organization, so point the source at the mirror")
    log("carrying the same revision:")
    log("")
    log("    source=(\"$pkgname::git+https://github.com/<org>/<mirror>.git#tag=v1\")")
    log("")
    log("Name the clone target after the package. The mirror is named for its")
    log("GitLab path, so a bare clone lands in a directory of that name while")
    log("prepare() and build() expect the package's own.")
    log("")
    log("If the repository is a gitlab-sync mirror it is force-pushed, so commit")
    log("the fix to patches/<repo>/ here rather than to the package repository.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
