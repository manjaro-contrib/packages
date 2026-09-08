#!/usr/bin/env python3
"""Fail on a pkgver() that changes with the clock rather than the source.

check_updates decides what to build by comparing the version a PKGBUILD
declares against the artifacts on R2. A pkgver() returning `date +%Y%m%d`
makes that comparison meaningless: the version differs every day whether
or not anything changed, so every package with one is rebuilt daily,
forever, and republished under a name nothing asked for.

manjaro-keyring had exactly this and we fixed it downstream. manjaro-system
has it too and cannot be fixed the same way - gitlab-sync force-pushes that
mirror, so a downstream commit is overwritten within two hours. That makes
this a property to detect before adding a package, not after.

A pkgver() counting commits on an *unpinned* VCS source fails the same way
with a different input. gtk3-nocsd declared `r74.c153438` while makepkg
resolved `r82.512c2bd` from live HEAD, so check_updates looked for an
artifact no build ever produces and rebuilt it every run. Worse than waste:
each rebuild of the same version overwrites the published object with a
byte-different one while the database still describes the old, leaving
sizes that disagree.

Deriving a version from a *pinned* source is fine and common: the commit
date of a `#commit=` revision moves only when someone bumps the revision,
which is the intended behaviour. Only versions that do not follow from the
repository contents are rejected.
"""

import argparse
import base64
import os
import re
import subprocess
import sys
import urllib.error

from catalog import load as load_catalog
from gh_api import api

import patches

# a shell call whose value comes from the wall clock. `date -r file` and
# `git show --date=` read a file and a commit, so they are not included.
CLOCK = re.compile(
    r"""
    (?<!-r\s)                 # date -r FILE reads the file's mtime
    \bdate\b                  # the date builtin
    (?!\s+-r\b)               # not `date -r`
    [^\n|)]*                  # its arguments
    (\+%|--date=|--rfc)       # producing a formatted stamp
    """,
    re.VERBOSE,
)
# printf '%(%Y%m%d)T' is the same reading without calling date
PRINTF_CLOCK = re.compile(r"%\(\s*%[YmdHMS][^)]*\)T")
# EPOCHSECONDS and SECONDS are bash's own clocks
SHELL_CLOCK = re.compile(r"\$\{?(EPOCHSECONDS|EPOCHREALTIME)\b")

# a pkgver() interrogating the checkout it was handed
VCS_READ = re.compile(r"\b(git|hg|svn|bzr)\s+\S")
# makepkg's vcs source syntax, and the fragments that fix it to a revision.
# `#branch=` and `#branch` alone track a moving tip, so they do not pin.
VCS_SOURCE = re.compile(r"\b(git|hg|svn|bzr)\+[^\s\"\')]+")
PINNED_FRAGMENT = re.compile(r"#(commit|tag|revision)=")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def pkgver_body(pkgbuild: str) -> str | None:
    """The body of pkgver(), or None if the PKGBUILD declares no function."""
    m = re.search(r"^\s*pkgver\s*\(\s*\)\s*\{", pkgbuild, re.MULTILINE)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    for j in range(i, len(pkgbuild)):
        if pkgbuild[j] == "{":
            depth += 1
        elif pkgbuild[j] == "}":
            depth -= 1
            if depth == 0:
                return pkgbuild[i + 1 : j]
    return pkgbuild[i + 1 :]


def clock_reads(body: str) -> list[str]:
    """Every line of a pkgver() body that reads the current time."""
    found = []
    for line in body.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if CLOCK.search(stripped) or PRINTF_CLOCK.search(stripped) or SHELL_CLOCK.search(
            stripped
        ):
            found.append(stripped)
    return found


def unpinned_sources(pkgbuild: str) -> list[str]:
    """Every VCS source in the PKGBUILD not fixed to a revision."""
    found = []
    for m in VCS_SOURCE.finditer(pkgbuild):
        url = m.group(0)
        # the fragment follows the url inside the same source entry
        tail = pkgbuild[m.end() : pkgbuild.find("\n", m.end())]
        if not PINNED_FRAGMENT.search(url + tail):
            found.append(url)
    return found


def floats_with_upstream(body: str, pkgbuild: str) -> list[str]:
    """pkgver() lines reading a VCS checkout that no revision pins.

    Empty when every VCS source is pinned: the version then moves only
    when someone bumps the revision, which is a source change like any
    other.
    """
    unpinned = unpinned_sources(pkgbuild)
    if not unpinned:
        return []
    found = []
    for line in body.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped and VCS_READ.search(stripped):
            found.append(stripped)
    if not found:
        return []
    return found + [f"unpinned source: {u}" for u in unpinned]


def fetch_pkgbuild(org: str, repo: str, token: str) -> str | None:
    status, data = api("GET", f"/repos/{org}/{repo}/contents/PKGBUILD", token)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"{repo}: fetching PKGBUILD failed ({status}): {data}")
    return base64.b64decode(data["content"]).decode(errors="replace")


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
        # a local patch is what fixes this for a mirror we cannot commit
        # into, so judge what will actually be built
        try:
            pkgbuild = patches.apply_to_text(repo, "PKGBUILD", pkgbuild)
        except subprocess.CalledProcessError:
            log(f"{repo}: local patches do not apply")
            problems.append((repo, ["patches do not apply"]))
            continue
        body = pkgver_body(pkgbuild)
        if body is None:
            continue
        reads = clock_reads(body)
        floats = floats_with_upstream(body, pkgbuild)
        if reads or floats:
            problems.append((repo, reads, floats))
            log(f"{repo}: pkgver() {'reads the clock' if reads else 'floats with an unpinned source'}")
            for line in reads + floats:
                log(f"    {line}")
        else:
            log(f"{repo}: pkgver() ok")

    if not problems:
        log(f"checked {len(repos)} package(s), every pkgver() follows its source")
        return 0

    log("")
    log(f"{len(problems)} package(s) version themselves by something other than")
    log("their source:")
    for repo, reads, _ in problems:
        log(f"  {repo} ({'clock' if reads else 'unpinned source'})")
    log("")
    log("A version that does not follow from the repository contents makes")
    log("check_updates look for an artifact the build never produces, so the")
    log("package is rebuilt every run and republished forever. Each rebuild")
    log("overwrites the published object with a byte-different one while the")
    log("database still describes the old, so the repository stops matching")
    log("itself.")
    log("")
    log("Fix it in the package repository by pinning pkgver to the upstream")
    log("release, or derive it from a pinned source revision - a VCS source")
    log("needs a #commit= or #tag= fragment for its version to mean anything.")
    log("If the repository is a gitlab-sync mirror it is force-pushed, so a")
    log("downstream commit will not survive - that package cannot be built")
    log("here until upstream changes it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
