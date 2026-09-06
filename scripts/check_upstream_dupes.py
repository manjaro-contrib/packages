#!/usr/bin/env python3
"""Report packages Arch now ships, so we need not.

An overlay exists to provide what the distribution does not. Once a
package reaches Arch it flows into Manjaro on its own, and keeping ours
only decides which copy pacman picks - if ours is older, that is a
downgrade nobody asked for.

Compared against Arch rather than Manjaro deliberately: Arch is weeks
ahead, so a package landing there is the earliest honest signal that ours
has become redundant, giving time to drop it before the duplicate ships.

Packages we deliberately override carry an `override` reason in
packages.yml and are reported separately, so the decision is recorded
rather than rediscovered.

Findings are kept in a single labelled issue rather than failing the run:
a package appearing upstream is worth acting on, but not urgently, and a
red check for it would train everyone to ignore red checks. The issue is
opened when a package becomes redundant, edited as the list changes, and
closed once nothing is left.
"""

import argparse
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request

import yaml
from gh_api import keep_issue

# arch's repositories, in the order pacman would search them
REPOS = ["core", "extra", "multilib"]
MIRROR = "https://geo.mirror.pkgbuild.com"
USER_AGENT = "manjaro-contrib-builder"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def arch_packages(arch: str) -> dict[str, str]:
    """Every package Arch ships, mapped to its version."""
    found: dict[str, str] = {}
    for repo in REPOS:
        url = f"{MIRROR}/{repo}/os/{arch}/{repo}.db.tar.gz"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            log(f"  {repo}: unavailable ({e.code}), skipping")
            continue
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for entry in tar.getnames():
                # entries are directories named <pkgname>-<pkgver>-<pkgrel>
                if "/" in entry:
                    continue
                name, _, rest = entry.rpartition("-")
                name, _, ver = name.rpartition("-")
                if name:
                    found[name] = f"{ver}-{rest}"
    return found


LABEL = "upstream-redundant"
TITLE = "Potentially redundant packages"


def issue_body(redundant: list[dict], overridden: list[dict]) -> str:
    """The issue text, or empty when there is nothing to report."""
    if not redundant:
        return ""
    lines = [
        "Arch now ships these packages, so this repository may not need to.",
        "An overlay package that duplicates the distribution only decides",
        "which copy pacman picks - and if ours is older, that is a downgrade.",
        "",
        "| package | arch version |",
        "| --- | --- |",
    ]
    lines += [f"| `{e['package']}` | `{e['upstream']}` |" for e in redundant]
    lines += [
        "",
        "Either drop the package from this repository, or record why we",
        "override upstream in [`packages.yml`](packages.yml):",
        "",
        "```yaml",
        f"{redundant[0]['package']}:",
        "  override: why upstream's build is not enough",
        "```",
    ]
    if overridden:
        lines += [
            "",
            "<details><summary>Deliberate overrides</summary>",
            "",
            "| package | arch version | reason |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| `{e['package']}` | `{e['upstream']}` | {e['reason']} |"
            for e in overridden
        ]
        lines += ["", "</details>"]
    lines += [
        "",
        (
            "<sub>Kept by `check-upstream-dupes`; closed automatically"
            " once nothing is redundant.</sub>"
        ),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="packages.yml")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument(
        "--json", action="store_true", help="emit the findings as json"
    )
    parser.add_argument(
        "--issue",
        metavar="OWNER/REPO",
        help="keep the findings in a labelled issue on this repository",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        packages = yaml.safe_load(f)["packages"] or {}

    log(f"fetching arch {args.arch} databases")
    upstream = arch_packages(args.arch)
    if not upstream:
        log("no upstream databases could be read")
        return 1
    log(f"{len(upstream)} upstream package(s) known")

    redundant, overridden = [], []
    for name, cfg in sorted(packages.items()):
        if name not in upstream:
            continue
        entry = {"package": name, "upstream": upstream[name]}
        reason = (cfg or {}).get("override")
        if reason:
            entry["reason"] = reason
            overridden.append(entry)
        else:
            redundant.append(entry)

    if args.json:
        json.dump({"redundant": redundant, "overridden": overridden}, sys.stdout)
        return 0

    for entry in overridden:
        log(f"{entry['package']}: also in arch {entry['upstream']}"
            f" - overridden on purpose: {entry['reason']}")
    for entry in redundant:
        log(f"{entry['package']}: arch ships {entry['upstream']}")

    if redundant:
        log("")
        log(f"{len(redundant)} package(s) arch now ships. Either drop them,")
        log("or record why we override upstream by adding to packages.yml:")
        log("  <package>:")
        log("    override: why upstream's build is not enough")
    else:
        log(f"nothing redundant ({len(overridden)} deliberate override(s))")

    if args.issue:
        token = os.environ["GITHUB_TOKEN"]
        result = keep_issue(
            args.issue, LABEL, TITLE, issue_body(redundant, overridden), token
        )
        if result is None:
            log("no issue needed")
        else:
            action, number = result
            log(f"issue #{number} {action}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
