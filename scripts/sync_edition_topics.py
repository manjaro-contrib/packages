#!/usr/bin/env python3
"""Tag each package repo with the editions whose settings package needs it.

An edition is defined by its settings package - the one providing
manjaro-desktop-settings - and a package belongs to that edition when the
settings package pulls it in, directly or through another package here.
Deriving the topics from the dependency graph keeps them honest: adding a
dependency is what makes a package part of an edition, so nothing has to
be remembered separately.

Topics look like `edition-sway`. Any edition topic no longer implied by the
graph is removed, so a dropped dependency withdraws the package.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

API = "https://api.github.com"
# github topics allow only lowercase alphanumerics and hyphens,
# so the namespace separator is a hyphen rather than a colon
PREFIX = "edition-"
MARKER = "manjaro-desktop-settings"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def api(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def edition_of(pkgbase: str) -> str:
    """`manjaro-sway-settings-git` and `manjaro-sway-settings` are both sway."""
    name = re.sub(r"-git$", "", pkgbase)
    name = re.sub(r"^manjaro-", "", name)
    return re.sub(r"-settings$", "", name)


def parse(repo: str, branch: str, token: str) -> dict | None:
    """Read a repo's PKGBUILD identity via the shared shell parser."""
    status, data = api(
        "GET", f"/repos/{repo}/contents/PKGBUILD?ref={branch}", token
    )
    if status != 200:
        return None
    content = base64.b64decode(data["content"]).decode()
    with tempfile.TemporaryDirectory() as workdir:
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
        log(f"  {repo}: parse failed")
        return None
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    return {
        "pkgbase": fields["pkgbase"],
        "names": fields["pkgname"].split(),
        "provides": [strip(p) for p in fields.get("provides", "").split()],
        "depends": [strip(d) for d in fields.get("depends", "").split()],
    }


def strip(dep: str) -> str:
    for sep in (">=", "<=", ">", "<", "="):
        if sep in dep:
            return dep.split(sep, 1)[0]
    return dep


def resolve(packages: dict[str, dict]) -> dict[str, set[str]]:
    """Map each pkgbase to the editions reaching it through the graph."""
    provider = {}
    for base, p in packages.items():
        for name in p["names"] + p["provides"]:
            provider[name] = base

    editions: dict[str, set[str]] = {base: set() for base in packages}
    for base, p in packages.items():
        if MARKER not in p["provides"]:
            continue
        edition = edition_of(base)
        # walk the graph so a package pulled in indirectly still counts
        seen, queue = set(), [base]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            editions.setdefault(current, set()).add(edition)
            for dep in packages[current]["depends"]:
                target = provider.get(dep)
                if target and target not in seen:
                    queue.append(target)
    return editions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--topic", default="pkg")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    status, found = api(
        "GET",
        f"/search/repositories?q=org:{args.org}+topic:{args.topic}&per_page=100",
        token,
    )
    if status != 200:
        log(f"discovery failed ({status}): {found}")
        return 1

    repos = {r["name"]: r for r in found["items"]}
    packages, owner = {}, {}
    for name, repo in repos.items():
        parsed = parse(repo["full_name"], repo["default_branch"], token)
        if parsed is None:
            log(f"{name}: no readable PKGBUILD, skipping")
            continue
        packages[parsed["pkgbase"]] = parsed
        owner[parsed["pkgbase"]] = name

    editions = resolve(packages)
    total = {e for s in editions.values() for e in s}
    log(f"{len(total)} edition(s): {', '.join(sorted(total)) or 'none'}")

    changed = failed = 0
    for base, wanted in sorted(editions.items()):
        name = owner[base]
        current = set(repos[name]["topics"])
        desired = {t for t in current if not t.startswith(PREFIX)}
        desired |= {f"{PREFIX}{e}" for e in wanted}
        if desired == current:
            continue
        added = sorted(desired - current)
        removed = sorted(current - desired)
        log(f"{name}: {'+' + ','.join(added) if added else ''}"
            f"{' -' + ','.join(removed) if removed else ''}")
        if args.dry_run:
            changed += 1
            continue
        status, payload = api(
            "PUT", f"/repos/{args.org}/{name}/topics", token,
            {"names": sorted(desired)},
        )
        if status != 200:
            log(f"{name}: topic update failed ({status}): {payload}")
            failed += 1
            continue
        changed += 1

    log(f"{changed} repo(s) {'would change' if args.dry_run else 'updated'}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
