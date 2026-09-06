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
import os
import sys

from gh_api import api, commit_file, reset_branch
from manifest import dump, load, path_for
from repo_common import FLOW, list_packages, s3_client
from repo_remove import pkgname_of


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




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
    # the branch is rebuilt from base each run, so the proposal always
    # reflects the source branch as it is now
    reset_branch(args.repo, head, args.base, token)
    commit_file(
        args.repo,
        str(path_for(args.branch)),
        dump(args.branch, proposed),
        f"chore: promote {len(proposed)} package(s) to {args.branch}",
        head,
        token,
    )

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
