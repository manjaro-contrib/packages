"""GitHub REST helpers, and file commits that GitHub signs for us.

Commits made through the contents API are created server-side and signed
with GitHub's web-flow key, so they satisfy a `required_signatures` rule
without the workflow holding any signing key. Shelling out to `git commit`
produces unsigned commits that such a rule rejects.
"""

import base64
import json
import urllib.error
import urllib.request

API = "https://api.github.com"


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


def reset_branch(repo: str, branch: str, base: str, token: str) -> None:
    """Point `branch` at the tip of `base`, creating it if absent.

    The proposal branches are regenerated from scratch each run, so an
    existing one is moved rather than merged onto.
    """
    status, ref = api("GET", f"/repos/{repo}/git/ref/heads/{base}", token)
    if status != 200:
        raise RuntimeError(f"cannot read {base} ({status}): {ref}")
    sha = ref["object"]["sha"]

    status, payload = api(
        "PATCH",
        f"/repos/{repo}/git/refs/heads/{branch}",
        token,
        {"sha": sha, "force": True},
    )
    if status == 200:
        return
    if status != 422:
        raise RuntimeError(f"cannot move {branch} ({status}): {payload}")

    status, payload = api(
        "POST",
        f"/repos/{repo}/git/refs",
        token,
        {"ref": f"refs/heads/{branch}", "sha": sha},
    )
    if status not in (200, 201):
        raise RuntimeError(f"cannot create {branch} ({status}): {payload}")


def commit_file(
    repo: str, path: str, content: str, message: str, branch: str, token: str
) -> None:
    """Write one file on a branch as a signed, server-side commit."""
    status, existing = api(
        "GET", f"/repos/{repo}/contents/{path}?ref={branch}", token
    )
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if status == 200:
        if base64.b64decode(existing["content"]).decode() == content:
            return
        body["sha"] = existing["sha"]
    elif status != 404:
        raise RuntimeError(f"cannot read {path} ({status}): {existing}")

    status, payload = api("PUT", f"/repos/{repo}/contents/{path}", token, body)
    if status not in (200, 201):
        raise RuntimeError(f"cannot write {path} ({status}): {payload}")
