"""Read packages.yml, the authority for what this repository builds.

Six scripts load this file, so the shape of an entry is defined once here
rather than in each of them. In particular the repository a package is
published into has a default, and a default repeated in six places is a
default that will disagree with itself.
"""

import yaml

CONFIG = "packages.yml"

# core is the boot-critical set and multilib the 32-bit compatibility set;
# everything else is extra, which upstream is 96% of
REPOS = ("core", "extra", "multilib")
DEFAULT_REPO = "extra"

# how gitlab-sync names a mirror: the GitLab group path, slashes to hyphens
MIRROR_GROUPS = frozenset(
    {
        "packages-core",
        "packages-extra",
        "packages-multilib",
        "packages-community",
        "applications",
        "tools-development-tools",
        "profiles-and-settings",
        "artwork-branding",
    }
)


def load(config: str = CONFIG) -> dict[str, dict]:
    """Every listed package, mapped to its entry. Entries are never None."""
    with open(config) as f:
        packages = yaml.safe_load(f)["packages"] or {}
    return {name: (entry or {}) for name, entry in packages.items()}


def repo_for_package(name: str, config: str = CONFIG) -> str:
    """The repository a built package belongs in, found by package name.

    packages.yml is keyed by repository, and a repository need not be named
    after the package it builds: packages-core-manjaro-release produces
    manjaro-release. publish.py and apply_manifest.py see only a package
    name, so without this the entry is missed and the package silently
    lands in the default repository - which is what shipped in #71 and #73.

    A gitlab-sync mirror is the GitLab path with slashes as hyphens, so the
    package name is what follows the group. Matching on any trailing hyphen
    instead would let a package called `release` claim
    packages-core-manjaro-release's entry.
    """
    entries = load(config)
    if name in entries:
        return repo_of(entries[name])
    matches = [
        k
        for k in entries
        if k.endswith(f"-{name}") and k[: -len(name) - 1] in MIRROR_GROUPS
    ]
    if len(matches) == 1:
        return repo_of(entries[matches[0]])
    # absent or ambiguous: an unlisted package gets what any unlisted
    # package gets, rather than aborting a publish mid-batch
    return DEFAULT_REPO


def repo_of(entry: dict) -> str:
    """The pacman repository a package belongs in.

    Nothing in a built package records this - membership is simply which
    database the package appears in - so it can only come from the config.
    """
    repo = entry.get("repo", DEFAULT_REPO)
    if repo not in REPOS:
        raise ValueError(f"unknown repo {repo!r}, expected one of {REPOS}")
    return repo


