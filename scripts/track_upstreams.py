#!/usr/bin/env python3
"""Sync package repos from their upstreams (e.g. AUR) via pull requests.

Reads packages.yml, and for every package whose upstream has commits the
package repo lacks, pushes an `update-from-upstream` branch with a clean
merge and opens a PR requesting review from the configured maintainers.
Merge conflicts abort that package — a human has to reconcile manually.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import yaml

UPDATE_BRANCH = "update-from-upstream"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def github_api(method: str, path: str, token: str, body: dict | None = None) -> dict | None:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp) if resp.status != 204 else None


def pr_exists(org: str, name: str, token: str) -> bool:
    prs = github_api(
        "GET", f"/repos/{org}/{name}/pulls?head={org}:{UPDATE_BRANCH}&state=open", token
    )
    return bool(prs)


def sync_package(org: str, name: str, cfg: dict, token: str) -> bool:
    upstream = cfg["upstream"]
    maintainers = cfg.get("maintainers", [])
    clone_url = f"https://x-access-token:{token}@github.com/{org}/{name}.git"

    if pr_exists(org, name, token):
        log(f"{name}: update PR already open, skipping")
        return True

    with tempfile.TemporaryDirectory() as workdir:
        repo = os.path.join(workdir, name)
        run(["git", "clone", "--quiet", clone_url, repo], cwd=workdir)
        run(["git", "config", "user.name", "manjaro-contrib-bot"], cwd=repo)
        run(["git", "config", "user.email", "bot@manjaro-contrib"], cwd=repo)
        run(["git", "remote", "add", "upstream", upstream], cwd=repo)
        run(["git", "fetch", "--quiet", "upstream"], cwd=repo)

        ahead = run(
            ["git", "rev-list", "--count", "HEAD..upstream/master"], cwd=repo
        ).stdout.strip()
        if ahead == "0":
            log(f"{name}: up to date with upstream")
            return True
        log(f"{name}: upstream is {ahead} commits ahead")

        run(["git", "checkout", "-b", UPDATE_BRANCH], cwd=repo)
        merge = run(
            ["git", "merge", "--no-edit", "upstream/master"], cwd=repo, check=False
        )
        if merge.returncode != 0:
            run(["git", "merge", "--abort"], cwd=repo, check=False)
            log(f"{name}: merge conflict with upstream, manual intervention needed")
            return False

        run(["git", "push", "--force", "origin", UPDATE_BRANCH], cwd=repo)
        default_branch = run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo
        ).stdout.strip().removeprefix("origin/")

    pr = github_api(
        "POST",
        f"/repos/{org}/{name}/pulls",
        token,
        {
            "title": f"chore: update {name} from upstream",
            "head": UPDATE_BRANCH,
            "base": default_branch,
            "body": f"Automated merge of {ahead} new upstream commit(s) from {upstream}.",
        },
    )
    log(f"{name}: opened PR #{pr['number']}")

    if maintainers:
        try:
            github_api(
                "POST",
                f"/repos/{org}/{name}/pulls/{pr['number']}/requested_reviewers",
                token,
                {"reviewers": maintainers},
            )
        except urllib.error.HTTPError as e:
            # 422: reviewer is the PR author or lacks repo access — PR is still valid
            log(f"{name}: could not request reviewers ({e.code})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="GitHub organization")
    parser.add_argument("--config", default="packages.yml")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    with open(args.config) as f:
        config = yaml.safe_load(f)

    failures = []
    for name, cfg in (config.get("packages") or {}).items():
        try:
            if not sync_package(args.org, name, cfg, token):
                failures.append(name)
        except (subprocess.CalledProcessError, urllib.error.HTTPError) as e:
            detail = e.stderr if isinstance(e, subprocess.CalledProcessError) else e
            log(f"{name}: failed: {detail}")
            failures.append(name)

    if failures:
        log(f"failed packages: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
