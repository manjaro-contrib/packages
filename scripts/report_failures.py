#!/usr/bin/env python3
"""Report which workflows are currently failing, in one place.

boxit has an `errors` command that answers "what is broken right now".
Our equivalent was reading Actions logs per workflow, which means nobody
notices until they go looking - an upstream outage failed fifteen of
fifteen ISO builds across three runs and surfaced nowhere.

This reads the latest completed run of each workflow on the default
branch and keeps the failing ones in a single labelled issue. A workflow
is judged by its most recent run only: an old failure that has since
succeeded is fixed, not broken, and belongs in the history rather than
the report.

Read-only against Actions; the only thing it writes is the issue.
"""

import argparse
import os
import sys
import urllib.parse

from gh_api import api, keep_issue

LABEL = "ci-failing"
TITLE = "Workflows currently failing"

# runs of these are expected to fail in normal use, so reporting them
# would train everyone to ignore the issue
IGNORED = {"pages-build-deployment"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def workflows(repo: str, token: str) -> list[dict]:
    status, data = api("GET", f"/repos/{repo}/actions/workflows?per_page=100", token)
    if status != 200:
        raise RuntimeError(f"cannot list workflows ({status}): {data}")
    return [
        w
        for w in data["workflows"]
        if w["state"] == "active" and w["name"] not in IGNORED
    ]


def latest_run(repo: str, workflow_id: int, branch: str, token: str) -> dict | None:
    """The most recent completed run of one workflow on a branch."""
    query = urllib.parse.urlencode(
        {"branch": branch, "status": "completed", "per_page": 1}
    )
    status, data = api(
        "GET", f"/repos/{repo}/actions/workflows/{workflow_id}/runs?{query}", token
    )
    if status != 200:
        raise RuntimeError(f"cannot list runs ({status}): {data}")
    runs = data.get("workflow_runs") or []
    return runs[0] if runs else None


def failing(repo: str, branch: str, token: str) -> list[dict]:
    """Every workflow whose most recent completed run did not succeed."""
    broken = []
    for workflow in workflows(repo, token):
        run = latest_run(repo, workflow["id"], branch, token)
        if run is None:
            continue
        # cancelled is a human stopping a run, not something broken
        if run["conclusion"] in ("success", "cancelled", "skipped"):
            continue
        broken.append(
            {
                "workflow": workflow["name"],
                "conclusion": run["conclusion"],
                "url": run["html_url"],
                "when": run["updated_at"][:16].replace("T", " "),
            }
        )
    return broken


def issue_body(broken: list[dict], repo: str, branch: str) -> str:
    """The issue text, or empty when nothing is failing."""
    if not broken:
        return ""
    lines = [
        f"The most recent run of these workflows on `{branch}` did not succeed.",
        "",
        "| workflow | result | when | run |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| `{b['workflow']}` | {b['conclusion']} | {b['when']} UTC |"
        f" [log]({b['url']}) |"
        for b in broken
    ]
    lines += [
        "",
        (
            "<sub>Kept by `report-failures`; each row clears when that"
            " workflow next succeeds, and the issue closes when all do.</sub>"
        ),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO")
    parser.add_argument(
        "--branch",
        default="main",
        help="only runs on this branch count; a pull request failing is the"
        " author's problem, not the repository's",
    )
    parser.add_argument(
        "--issue",
        metavar="OWNER/REPO",
        help="keep the findings in a labelled issue on this repository",
    )
    args = parser.parse_args()

    token = os.environ["GITHUB_TOKEN"]
    broken = failing(args.repo, args.branch, token)

    for entry in broken:
        log(f"{entry['workflow']}: {entry['conclusion']} ({entry['when']} UTC)")
    if not broken:
        log("every workflow's latest run succeeded")

    if args.issue:
        result = keep_issue(
            args.issue, LABEL, TITLE, issue_body(broken, args.repo, args.branch), token
        )
        if result is None:
            log("no issue needed")
        else:
            action, number = result
            log(f"issue #{number} {action}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
