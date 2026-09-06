#!/usr/bin/env python3
"""Sync a CODEOWNERS file into each package repo from packages.yml.

Maintainers are declared centrally, but review assignment happens in the
package repos, so the two drift unless the file is generated. CODEOWNERS
also requests review on *every* pull request, including ones the upstream
tracker cannot request reviewers on itself.

Idempotent: a repo whose file already matches is left untouched, so this
can run on a schedule without churning commits.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

import yaml

API = "https://api.github.com"
PATH = ".github/CODEOWNERS"
HEADER = """\
# Generated from packages.yml in manjaro-contrib/packages; edit it there.
# Owners are requested for review on every pull request in this repository.
"""


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


def render(maintainers: list[str]) -> str:
    owners = " ".join(f"@{m.lstrip('@')}" for m in maintainers)
    return f"{HEADER}* {owners}\n"


def sync_repo(org: str, name: str, maintainers: list[str], token: str) -> bool:
    """Write CODEOWNERS if it is missing or stale. True when a change landed."""
    wanted = render(maintainers)
    status, current = api("GET", f"/repos/{org}/{name}/contents/{PATH}", token)

    sha = None
    if status == 200:
        if base64.b64decode(current["content"]).decode() == wanted:
            log(f"{name}: up to date")
            return False
        sha = current["sha"]
    elif status != 404:
        raise RuntimeError(f"{name}: read failed ({status}): {current}")

    body = {
        "message": "chore: sync CODEOWNERS from packages.yml",
        "content": base64.b64encode(wanted.encode()).decode(),
        "committer": {
            "name": "manjaro-contrib-bot",
            "email": "bot@manjaro-contrib",
        },
    }
    if sha:
        body["sha"] = sha
    status, payload = api("PUT", f"/repos/{org}/{name}/contents/{PATH}", token, body)
    if status not in (200, 201):
        raise RuntimeError(f"{name}: write failed ({status}): {payload}")
    log(f"{name}: {'updated' if sha else 'created'} CODEOWNERS")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--config", default="packages.yml")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    with open(args.config) as f:
        packages = yaml.safe_load(f)["packages"]

    changed = failed = 0
    for name, cfg in packages.items():
        maintainers = (cfg or {}).get("maintainers") or []
        if not maintainers:
            # no owner means no review requirement to express
            log(f"{name}: no maintainers, skipping")
            continue
        try:
            changed += sync_repo(args.org, name, maintainers, token)
        except (RuntimeError, urllib.error.HTTPError) as e:
            log(f"{name}: {e}")
            failed += 1

    log(f"{changed} repo(s) updated, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
