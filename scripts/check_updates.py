#!/usr/bin/env python3
"""Discover package repos by GitHub topic and diff them against the R2 repo.

Stateless: the desired state is each repo's PKGBUILD on its default branch,
the actual state is whatever artifacts exist under the branch prefix on R2.
Packages already released on their own repo are marked reusable so the
pipeline can republish the stored binary instead of rebuilding it.
Emits a JSON build list on stdout; everything else goes to stderr.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from release_store import get_release, has_assets

TOPIC = "manjaro-contrib-pkg"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Cloudflare answers the default Python-urllib agent with 403, which would
# otherwise look like "artifact missing" and rebuild every package forever.
USER_AGENT = "manjaro-contrib-builder"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def github_request(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def discover_repos(org: str, token: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        data = github_request(
            f"https://api.github.com/search/repositories"
            f"?q=org:{org}+topic:{TOPIC}&per_page=100&page={page}",
            token,
        )
        repos.extend(data["items"])
        if len(repos) >= data["total_count"] or not data["items"]:
            return repos
        page += 1


def fetch_pkgbuild(repo: dict, token: str) -> str | None:
    # The contents API, unlike raw.githubusercontent.com, is not CDN-cached
    # and reflects a push immediately — otherwise a fresh bump looks built.
    url = (
        f"https://api.github.com/repos/{repo['full_name']}"
        f"/contents/PKGBUILD?ref={repo['default_branch']}"
    )
    try:
        data = github_request(url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return base64.b64decode(data["content"]).decode()


def parse_pkgbuild(content: str, workdir: str) -> dict | None:
    path = os.path.join(workdir, "PKGBUILD")
    with open(path, "w") as f:
        f.write(content)
    result = subprocess.run(
        [os.path.join(SCRIPT_DIR, "parse_pkgbuild.sh"), path],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        log(f"  parse failed: {result.stderr.strip()}")
        return None
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    fields["pkgname"] = fields["pkgname"].split()
    fields["arch"] = fields["arch"].split()
    fields["provides"] = [strip_constraint(d) for d in fields.get("provides", "").split()]
    fields["depends"] = [strip_constraint(d) for d in fields.get("depends", "").split()]
    return fields


def strip_constraint(dep: str) -> str:
    """`foo>=1.2` and `foo=1.2` both name the package `foo`."""
    for sep in (">=", "<=", ">", "<", "="):
        if sep in dep:
            return dep.split(sep, 1)[0]
    return dep


def assign_waves(pending: list[dict]) -> None:
    """Number each package by how deep it sits in the pending dependency graph.

    The build matrix runs everything at once, so a package depending on
    another package built in the same run fails: its dependency is only in
    the repository after that run publishes. Grouping into waves lets the
    workflow build wave 0, publish, then wave 1, and so on.

    Only dependencies *within this run* matter; anything already published
    or coming from an upstream repository resolves normally.
    """
    # a package can be pulled in under any name it provides
    provider = {}
    for item in pending:
        for name in map(strip_constraint, item["names"] + item["provides"]):
            provider[name] = item["pkgbase"]

    deps = {
        item["pkgbase"]: {
            provider[dep]
            for dep in map(strip_constraint, item["depends"])
            if dep in provider and provider[dep] != item["pkgbase"]
        }
        for item in pending
    }

    wave = {}
    remaining = dict(deps)
    current = 0
    while remaining:
        ready = [k for k, v in remaining.items() if not (v - wave.keys())]
        if not ready:
            # a cycle cannot be ordered; build the rest together and let
            # makepkg report which dependency is genuinely unsatisfiable
            log(f"  dependency cycle among {', '.join(sorted(remaining))}")
            ready = list(remaining)
        for name in ready:
            wave[name] = current
            del remaining[name]
        current += 1

    for item in pending:
        item["wave"] = wave[item["pkgbase"]]


def full_version(fields: dict) -> str:
    version = f"{fields['pkgver']}-{fields['pkgrel']}"
    return f"{fields['epoch']}:{version}" if fields.get("epoch") else version


def artifact_names(fields: dict, target_arch: str) -> list[str]:
    version = full_version(fields)
    # 'any' packages keep their arch suffix but live in the arch-specific dir
    arch = "any" if "any" in fields["arch"] else target_arch
    return [
        f"{name}-{version}-{arch}.pkg.tar.zst" for name in fields["pkgname"]
    ]


def exists_on_r2(repo_url: str, branch: str, arch: str, filename: str) -> bool:
    url = f"{repo_url}/{branch}/{arch}/{urllib.parse.quote(filename)}"
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        # Anything else (403 bot rules, 5xx) is an error, not an absent
        # artifact — treating it as absent would rebuild and republish.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="GitHub organization")
    parser.add_argument(
        "--repo-url",
        required=True,
        help="public base URL of the pacman repository (no trailing slash)",
    )
    parser.add_argument("--branch", default="unstable")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument(
        "--wave",
        type=int,
        help="emit only packages in this dependency wave",
    )
    parser.add_argument(
        "--only-repo",
        help="restrict the check to a single repository name (dispatch trigger)",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    repos = discover_repos(args.org, token)
    log(f"discovered {len(repos)} repos with topic {TOPIC}")
    if args.only_repo:
        repos = [r for r in repos if r["name"] == args.only_repo]
        if not repos:
            log(f"repo {args.only_repo} not found or not tagged {TOPIC}")
            return 1

    pending = []
    for repo in repos:
        log(f"checking {repo['full_name']}")
        content = fetch_pkgbuild(repo, token)
        if content is None:
            log("  no PKGBUILD on default branch, skipping")
            continue
        with tempfile.TemporaryDirectory() as workdir:
            fields = parse_pkgbuild(content, workdir)
        if fields is None:
            continue
        version = full_version(fields)
        artifacts = artifact_names(fields, args.arch)
        missing = [
            name
            for name in artifacts
            if not exists_on_r2(args.repo_url, args.branch, args.arch, name)
        ]
        if not missing:
            log("  up to date")
            continue
        # A prior build of this exact version is republishable as-is; only
        # the full artifact set counts, a partial release must rebuild.
        release = get_release(repo["full_name"], version, token)
        reusable = has_assets(release, artifacts)
        log(f"  needs {'release reuse' if reusable else 'build'}: {', '.join(missing)}")
        pending.append(
            {
                "repo": repo["full_name"],
                "default_branch": repo["default_branch"],
                "pkgbase": fields["pkgbase"],
                "version": version,
                "artifacts": artifacts,
                "reusable": reusable,
                "names": fields["pkgname"],
                "provides": fields["provides"],
                "depends": fields["depends"],
            }
        )

    assign_waves(pending)
    pending.sort(key=lambda p: (p["wave"], p["pkgbase"]))
    waves = max((p["wave"] for p in pending), default=-1) + 1
    if waves > 1:
        log(f"{len(pending)} package(s) in {waves} dependency waves")
        for w in range(waves):
            names = [p["pkgbase"] for p in pending if p["wave"] == w]
            log(f"  wave {w}: {', '.join(names)}")

    if args.wave is not None:
        pending = [p for p in pending if p["wave"] == args.wave]

    json.dump(pending, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
