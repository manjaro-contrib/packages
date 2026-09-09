#!/usr/bin/env python3
"""Sync package repos from their upstreams (e.g. AUR) via pull requests.

Reads packages.yml, and for every package whose upstream carries content
the package repo lacks, pushes an `update-from-upstream` branch with a
clean merge and opens a PR requesting review from the configured
maintainers. Merge conflicts abort that package — a human has to
reconcile manually.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import yaml
from gh_api import api

UPDATE_BRANCH = "update-from-upstream"
# the raw upstream commits, pushed so github can merge them itself
UPSTREAM_REF = "upstream-master"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def pr_exists(org: str, name: str, token: str) -> bool:
    status, prs = api(
        "GET", f"/repos/{org}/{name}/pulls?head={org}:{UPDATE_BRANCH}&state=open", token
    )
    if status != 200:
        raise RuntimeError(f"pull request lookup failed ({status}): {prs}")
    return bool(prs)


def sync_package(org: str, name: str, cfg: dict, token: str) -> bool:
    upstream = (cfg or {}).get("upstream")
    if not upstream:
        # packages maintained here have no external source to track; they
        # are still listed so packages.yml inventories the whole repository
        log(f"{name}: no upstream, nothing to track")
        return True
    maintainers = (cfg or {}).get("maintainers", [])
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

        # rehearse the merge locally so a conflict never reaches a pull
        # request, then throw the result away. --allow-unrelated-histories
        # because repos seeded by importing upstream's files share no
        # commit with it, and git otherwise refuses outright
        run(["git", "checkout", "-b", UPDATE_BRANCH], cwd=repo)
        merge = run(
            ["git", "merge", "--no-commit", "--no-ff",
             "--allow-unrelated-histories", "upstream/master"],
            cwd=repo,
            check=False,
        )
        if merge.returncode != 0:
            run(["git", "merge", "--abort"], cwd=repo, check=False)
            log(f"{name}: merge conflict with upstream, manual intervention needed")
            return False

        # a commit count is not a content delta: an unrelated history counts
        # every upstream commit even when the merge resolves to exactly our
        # tree, which would open a pull request that changes nothing
        unchanged = run(
            ["git", "diff", "--cached", "--quiet", "HEAD"], cwd=repo, check=False
        ).returncode == 0
        run(["git", "merge", "--abort"], cwd=repo, check=False)
        if unchanged:
            log(f"{name}: upstream matches our tree, nothing to open")
            return True

        default_branch = run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo
        ).stdout.strip().removeprefix("origin/")

        # push the upstream commits under their own ref and let github merge
        # them: a merge commit github creates is signed, ours is not
        run(
            ["git", "push", "--force", "origin",
             f"upstream/master:refs/heads/{UPSTREAM_REF}"],
            cwd=repo,
        )
        run(
            ["git", "push", "--force", "origin",
             f"origin/{default_branch}:refs/heads/{UPDATE_BRANCH}"],
            cwd=repo,
        )

    status, payload = api(
        "POST",
        f"/repos/{org}/{name}/merges",
        token,
        {
            "base": UPDATE_BRANCH,
            "head": UPSTREAM_REF,
            "commit_message": f"chore: merge {ahead} upstream commit(s)",
        },
    )
    if status not in (200, 201, 204):
        log(f"{name}: server-side merge failed ({status}): {payload}")
        return False

    status, pr = api(
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
    if status not in (200, 201):
        log(f"{name}: opening the pull request failed ({status}): {pr}")
        return False
    log(f"{name}: opened PR #{pr['number']}")

    if maintainers:
        status, detail = api(
            "POST",
            f"/repos/{org}/{name}/pulls/{pr['number']}/requested_reviewers",
            token,
            {"reviewers": maintainers},
        )
        # 422: reviewer is the PR author or lacks repo access - PR is still valid
        if status not in (200, 201):
            log(f"{name}: could not request reviewers ({status})")
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
