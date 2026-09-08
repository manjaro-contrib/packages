"""Local patches applied to a package repository we cannot commit into.

A gitlab-sync mirror is force-pushed from gitlab.manjaro.org, so a fix
committed there is overwritten within two hours. Carrying the fix here
instead keeps the mirror a faithful copy and still lets us build the
package.

A patch has to reach two places or they disagree about what will be built:
check_updates reads only the PKGBUILD text to decide which version is
missing, while the build job checks out the whole repository. Both call
into here.
"""

import os
import subprocess
import tempfile

PATCH_DIR = "patches"


def patch_paths(name: str, root: str = ".") -> list[str]:
    """Every patch file declared for a package, in application order."""
    if not name:
        return []
    d = os.path.join(root, PATCH_DIR, name)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".patch")]


def apply(name: str, workdir: str, root: str = ".") -> list[str]:
    """Apply a package's patches to a checkout. Returns what was applied.

    Raises CalledProcessError if a patch does not apply: a patch that has
    been made redundant by an upstream change is a change of behaviour, not
    something to skip quietly.
    """
    applied = []
    for path in patch_paths(name, root):
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", os.path.abspath(path)],
            cwd=workdir,
            check=True,
        )
        applied.append(os.path.basename(path))
    return applied


def apply_to_text(name: str, filename: str, content: str, root: str = ".") -> str:
    """Apply a package's patches to one file's text, outside a checkout.

    check_updates has the PKGBUILD but not the repository, so the patch is
    applied in a scratch git tree holding just that file. A patch touching
    other files still applies here as long as it also touches this one;
    anything it cannot find is reported by git apply as usual.
    """
    paths = patch_paths(name, root)
    if not paths:
        return content

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, filename)
        with open(target, "w") as f:
            f.write(content)
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        for path in paths:
            subprocess.run(
                [
                    "git",
                    "apply",
                    "--whitespace=nowarn",
                    "--include",
                    filename,
                    os.path.abspath(path),
                ],
                cwd=tmp,
                check=True,
            )
        with open(target) as f:
            return f.read()
