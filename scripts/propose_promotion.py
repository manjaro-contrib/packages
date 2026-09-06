#!/usr/bin/env python3
"""Open a pull request moving a branch manifest up to its source branch.

Promotion is declarative: the manifest states which version each branch
carries, and merging a change to it is what promotes. This proposes that
change, so reviewing a diff of package versions is the approval step and
git history records who promoted what.

The branch is force-updated in place, so a pending proposal keeps pace with
its source rather than accumulating stale pull requests.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from manifest import dump, load, path_for
from repo_common import FLOW, list_packages, s3_client
from repo_remove import pkgname_of

API = "https://api.github.com"


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


def version_of(filename: str, name: str) -> str:
    # name-version-arch.pkg.tar.zst, and only arch follows the version
    rest = filename[len(name) + 1 :].removesuffix(".pkg.tar.zst")
    return rest.rsplit("-", 1)[0]


def source_versions(s3, bucket: str, branch: str, arch: str) -> dict[str, str]:
    versions = {}
    for key in list_packages(s3, bucket, f"{branch}/{arch}/"):
        name = pkgname_of(key)
        if name:
            versions[name] = version_of(key, name)
    return versions


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def describe(current: dict, proposed: dict) -> str:
    added = sorted(set(proposed) - set(current))
    removed = sorted(set(current) - set(proposed))
    changed = sorted(k for k in current.keys() & proposed.keys() if current[k] != proposed[k])
    lines = []
    if added:
        lines.append(f"### Added ({len(added)})")
        lines += [f"- `{n}` {proposed[n]}" for n in added]
        lines.append("")
    if changed:
        lines.append(f"### Updated ({len(changed)})")
        lines += [f"- `{n}` {current[n]} → {proposed[n]}" for n in changed]
        lines.append("")
    if removed:
        lines.append(f"### Withdrawn ({len(removed)})")
        lines += [f"- `{n}` {current[n]}" for n in removed]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name of this repository")
    parser.add_argument("--branch", required=True, help="branch to promote into")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--base", default="main")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    source = FLOW.get(args.branch)
    if source is None:
        log(f"{args.branch} is not a promotion target")
        return 1

    s3 = s3_client()
    proposed = source_versions(s3, os.environ["R2_BUCKET"], source, args.arch)
    current = load(args.branch)

    if proposed == current:
        log(f"{args.branch} manifest already matches {source}")
        return 0

    head = f"promote/{args.branch}"
    path = path_for(args.branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(args.branch, proposed))

    run(["git", "config", "user.name", "manjaro-contrib-bot"])
    run(["git", "config", "user.email", "bot@manjaro-contrib"])
    run(["git", "checkout", "-B", head])
    run(["git", "add", str(path)])
    run(["git", "commit", "-m", f"chore: promote {len(proposed)} package(s) to {args.branch}"])
    # force: the proposal always reflects the source branch as it is now
    run(["git", "push", "--force", "origin", head])

    status, prs = api("GET", f"/repos/{args.repo}/pulls?head={args.repo.split('/')[0]}:{head}&state=open", token)
    if status == 200 and prs:
        log(f"{args.branch}: refreshed #{prs[0]['number']}")
        api(
            "PATCH",
            f"/repos/{args.repo}/pulls/{prs[0]['number']}",
            token,
            {"body": describe(current, proposed)},
        )
        return 0

    status, pr = api(
        "POST",
        f"/repos/{args.repo}/pulls",
        token,
        {
            "title": f"promote {len(proposed)} package(s) to {args.branch}",
            "head": head,
            "base": args.base,
            "body": describe(current, proposed),
        },
    )
    if status not in (200, 201):
        log(f"pull request failed ({status}): {pr}")
        return 1
    log(f"{args.branch}: opened #{pr['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
