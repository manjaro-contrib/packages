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


def load(config: str = CONFIG) -> dict[str, dict]:
    """Every listed package, mapped to its entry. Entries are never None."""
    with open(config) as f:
        packages = yaml.safe_load(f)["packages"] or {}
    return {name: (entry or {}) for name, entry in packages.items()}


def repo_of(entry: dict) -> str:
    """The pacman repository a package belongs in.

    Nothing in a built package records this - membership is simply which
    database the package appears in - so it can only come from the config.
    """
    repo = entry.get("repo", DEFAULT_REPO)
    if repo not in REPOS:
        raise ValueError(f"unknown repo {repo!r}, expected one of {REPOS}")
    return repo


