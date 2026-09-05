#!/usr/bin/env python3
"""Track the promotion backlog as a GitHub issue per target branch.

A branch is behind whenever its source holds packages it lacks. That state
is otherwise invisible until someone runs a dry run, so it is mirrored into
one issue per target branch: opened when work appears, edited as the set
changes, and closed once the branch has caught up.

The backlog is derived from the same plan promote.py executes, so the issue
can never describe something a real promotion would not do.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import boto3
from promote import FLOW, list_packages, plan_promotion

API = "https://api.github.com"
LABEL = "promotion-backlog"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def api(
    method: str, path: str, token: str, body: dict | None = None
) -> tuple[int, dict | list | None]:
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


def write(method: str, path: str, token: str, body: dict, what: str) -> bool:
    """Perform an issue mutation, reporting a permission failure loudly.

    Every write here needs the token's Issues scope. A 403 returns a normal
    JSON body, so an unchecked call looks identical to success.
    """
    status, payload = api(method, path, token, body)
    if status in (200, 201):
        return True
    if status == 403:
        raise RuntimeError(
            f"{what} failed: token lacks Issues write permission. "
            "Grant the fine-grained token 'Issues: read and write'."
        )
    raise RuntimeError(f"{what} failed ({status}): {payload}")


def find_issue(repo: str, target: str, token: str) -> dict | None:
    # matched on the body marker, not the label: labelling needs an Issues
    # write scope the token may not carry, and a missed match would open a
    # duplicate issue on every run
    status, issues = api(
        "GET", f"/repos/{repo}/issues?state=open&per_page=100", token
    )
    if status != 200 or not issues:
        return None
    marker = marker_for(target)
    for issue in issues:
        if marker in (issue.get("body") or ""):
            return issue
    return None


def marker_for(target: str) -> str:
    # identifies the issue for a branch independently of its title
    return f"<!-- promotion-backlog:{target} -->"


def render_body(source: str, target: str, plan: list[dict]) -> str:
    lines = [
        marker_for(target),
        f"`{source}` holds packages that `{target}` does not.",
        "",
    ]
    for action, heading in (("new", "New"), ("replace", "Updated")):
        rows = [p for p in plan if p["action"] == action]
        if not rows:
            continue
        lines.append(f"### {heading} ({len(rows)})")
        for row in rows:
            detail = f" — supersedes `{row['detail']}`" if row["detail"] else ""
            lines.append(f"- `{row['name']}`{detail}")
        lines.append("")
    lines += [
        "### Promote",
        "Run the `promote` workflow with:",
        "```",
        f"source_branch: {source}",
        f"target_branch: {target}",
        "packages:      ALL",
        "```",
        (
            "Tick `dry_run` first to review. This issue closes automatically "
            f"once `{target}` has caught up."
        ),
    ]
    return "\n".join(lines)


def sync_branch(repo: str, s3, bucket: str, target: str, arch: str, token: str) -> None:
    source = FLOW[target]
    src_prefix = f"{source}/{arch}/"
    dst_prefix = f"{target}/{arch}/"
    names = list_packages(s3, bucket, src_prefix)
    plan = plan_promotion(s3, bucket, src_prefix, dst_prefix, names)
    pending = [p for p in plan if p["action"] in ("new", "replace", "overwrite")]

    issue = find_issue(repo, target, token)
    if not pending:
        if issue:
            write(
                "POST",
                f"/repos/{repo}/issues/{issue['number']}/comments",
                token,
                {"body": f"`{target}` is in sync with `{source}`; closing."},
                "issue comment",
            )
            write(
                "PATCH",
                f"/repos/{repo}/issues/{issue['number']}",
                token,
                {"state": "closed", "state_reason": "completed"},
                "issue close",
            )
            log(f"{target}: caught up, closed #{issue['number']}")
        else:
            log(f"{target}: caught up")
        return

    title = f"promote {len(pending)} package(s) to {target}"
    body = render_body(source, target, pending)
    if issue is None:
        status, created = api(
            "POST",
            f"/repos/{repo}/issues",
            token,
            {"title": title, "body": body, "labels": [LABEL]},
        )
        if status not in (200, 201):
            raise RuntimeError(f"issue create failed ({status}): {created}")
        log(f"{target}: opened #{created['number']} for {len(pending)} package(s)")
        return

    if issue["title"] == title and (issue.get("body") or "") == body:
        log(f"{target}: backlog unchanged (#{issue['number']})")
        return
    write(
        "PATCH",
        f"/repos/{repo}/issues/{issue['number']}",
        token,
        {"title": title, "body": body},
        "issue update",
    )
    log(f"{target}: updated #{issue['number']} to {len(pending)} package(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name holding the issues")
    parser.add_argument(
        "--branches",
        default="testing,stable",
        help="comma-separated target branches to track",
    )
    parser.add_argument("--arch", default="x86_64")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    bucket = os.environ["R2_BUCKET"]
    s3 = s3_client()
    for target in [b.strip() for b in args.branches.split(",") if b.strip()]:
        if target not in FLOW:
            log(f"{target}: no source branch defined, skipping")
            continue
        sync_branch(args.repo, s3, bucket, target, args.arch, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
