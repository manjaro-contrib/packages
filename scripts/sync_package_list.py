#!/usr/bin/env python3
"""Open a pull request adding newly tagged repositories to packages.yml.

A repository carrying the discovery topic is built whether or not it is
listed, so an unlisted one is invisible in the file that claims to
inventory everything. This finds that drift and proposes the entry, with an
AUR upstream prefilled when a package of that name exists there.

Entries are inserted into the existing text rather than rewritten from
parsed YAML, so the file's grouping and comments survive.
"""

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml
from gh_api import api, commit_file, reset_branch

AUR_RPC = "https://aur.archlinux.org/rpc/?v=5&type=info"
AUR_GROUP = "  # tracked against the AUR\n"
LOCAL_GROUP = "  # maintained here; no external upstream\n"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)




def tagged_repos(org: str, topic: str, token: str) -> list[str]:
    status, data = api(
        "GET",
        f"/search/repositories?q=org:{org}+topic:{topic}&per_page=100",
        token,
    )
    if status != 200:
        raise RuntimeError(f"discovery failed ({status}): {data}")
    return sorted(r["name"] for r in data["items"])


def in_aur(names: list[str]) -> set[str]:
    """Which of these package names exist in the AUR."""
    if not names:
        return set()
    query = "".join(f"&arg[]={urllib.parse.quote(n)}" for n in names)
    with urllib.request.urlopen(AUR_RPC + query) as resp:
        return {r["Name"] for r in json.load(resp)["results"]}


def entry(name: str, maintainers: list[str], upstream: str | None) -> str:
    owners = ", ".join(f'"{m}"' for m in maintainers)
    lines = [f"  {name}:"]
    if upstream:
        lines.append(f"    upstream: {upstream}")
    lines.append(f"    maintainers: [{owners}]")
    return "\n".join(lines) + "\n"


def insert(text: str, name: str, block: str, group: str) -> str:
    """Place a block alphabetically within its group, keeping the layout."""
    start = text.index(group) + len(group)
    end = len(text)
    for marker in (AUR_GROUP, LOCAL_GROUP):
        if marker != group and (pos := text.find(marker, start)) != -1:
            end = min(end, pos)

    section = text[start:end]
    # entries begin at two-space indent; anything else ends the group
    for m in re.finditer(r"^  ([A-Za-z0-9._+-]+):$", section, re.MULTILINE):
        if m.group(1) > name:
            at = start + m.start()
            return text[:at] + block + text[at:]
    trimmed = section.rstrip("\n")
    at = start + len(trimmed) + 1
    return text[:at] + block + text[at:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name of this repository")
    parser.add_argument("--topic", default="pkg")
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument("--base", default="main")
    parser.add_argument(
        "--maintainer",
        action="append",
        default=[],
        help="owner for new entries; repeatable",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    org = args.repo.split("/")[0]
    text = pathlib.Path(args.config).read_text()
    listed = set(yaml.safe_load(text)["packages"])
    tagged = tagged_repos(org, args.topic, token)

    missing = [n for n in tagged if n not in listed]
    stale = sorted(listed - set(tagged))
    for name in stale:
        # listed but untagged: the repo will not be built, so the entry lies
        log(f"{name}: listed but no longer carries the {args.topic} topic")
    if not missing:
        log(f"packages.yml lists every repo tagged {args.topic}")
        return 0

    aur = in_aur(missing)
    maintainers = args.maintainer or ["boredland"]
    for name in missing:
        upstream = f"https://aur.archlinux.org/{name}.git" if name in aur else None
        text = insert(
            text,
            name,
            entry(name, maintainers, upstream),
            AUR_GROUP if upstream else LOCAL_GROUP,
        )
        log(f"{name}: adding ({'aur' if upstream else 'local'})")

    # a malformed insert would break every workflow reading this file
    added = set(yaml.safe_load(text)["packages"])
    if added != listed | set(missing):
        log("refusing to propose: the result does not parse as expected")
        return 1

    if args.dry_run:
        log(f"would add {len(missing)} entr(ies)")
        return 0

    head = "chore/package-list"
    # rebuilt from base each run so the proposal never carries stale entries
    reset_branch(args.repo, head, args.base, token)
    commit_file(
        args.repo,
        args.config,
        text,
        f"chore: add {len(missing)} package(s) to packages.yml",
        head,
        token,
    )

    body = f"These repositories carry the `{args.topic}` topic but were not listed:\n\n" + "\n".join(
        f"- [`{n}`](https://github.com/{org}/{n})"
        + (" — tracked against the AUR" if n in aur else "")
        for n in missing
    )

    status, prs = api(
        "GET", f"/repos/{args.repo}/pulls?head={org}:{head}&state=open", token
    )
    if status == 200 and prs:
        api("PATCH", f"/repos/{args.repo}/pulls/{prs[0]['number']}", token, {"body": body})
        log(f"refreshed #{prs[0]['number']}")
        return 0

    status, pr = api(
        "POST",
        f"/repos/{args.repo}/pulls",
        token,
        {
            "title": f"chore: add {len(missing)} package(s) to packages.yml",
            "head": head,
            "base": args.base,
            "body": body,
        },
    )
    if status not in (200, 201):
        log(f"pull request failed ({status}): {pr}")
        return 1
    log(f"opened #{pr['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
