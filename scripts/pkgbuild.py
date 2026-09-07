"""Read a PKGBUILD's identity via the shared shell parser."""

import os
import subprocess

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parse_pkgbuild.sh")


def strip_constraint(dep: str) -> str:
    """`foo>=1.2` and `foo=1.2` both name the package `foo`."""
    for sep in (">=", "<=", ">", "<", "="):
        if sep in dep:
            return dep.split(sep, 1)[0]
    return dep


def fields(content: str, workdir: str) -> tuple[dict[str, str] | None, str]:
    """Parse a PKGBUILD's text. Returns (fields, error) with one of them set.

    Sourcing runs arbitrary shell from the package repository, so this stays
    a subprocess with a timeout; callers report the error in their own terms.
    """
    path = os.path.join(workdir, "PKGBUILD")
    with open(path, "w") as f:
        f.write(content)
    result = subprocess.run(
        [SCRIPT, path], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    parsed = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    return parsed, ""
