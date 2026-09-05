#!/usr/bin/env python3
"""Keep built packages as GitHub releases on their own package repository.

The R2 bucket only ever holds the current version of a package, so without
a second home a superseded build is gone for good. Publishing every build
as a release asset gives an auditable history, lets a branch be rebuilt
from scratch, and lets the pipeline reuse an existing binary instead of
recompiling it.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def release_tag(version: str) -> str:
    # git refs forbid ':', which pacman uses to separate the epoch
    return version.replace(":", "_")


def asset_name(filename: str) -> str:
    # GitHub rewrites unusual characters in asset names, so normalise the
    # epoch separator up front and map back on download.
    return filename.replace(":", "_")


def request(
    method: str,
    url: str,
    token: str,
    body: bytes | dict | None = None,
    accept: str = "application/vnd.github+json",
    content_type: str | None = None,
) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Accept", accept)
    req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_release(repo: str, version: str, token: str) -> dict | None:
    tag = urllib.parse.quote(release_tag(version), safe="")
    status, payload = request("GET", f"{API}/repos/{repo}/releases/tags/{tag}", token)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"release lookup failed ({status}): {payload[:200]!r}")
    return json.loads(payload)


def has_assets(release: dict | None, artifacts: list[str]) -> bool:
    if release is None:
        return False
    names = {a["name"] for a in release.get("assets", [])}
    return all(asset_name(a) in names for a in artifacts)


def ensure_release(repo: str, version: str, token: str) -> dict:
    release = get_release(repo, version, token)
    if release is not None:
        return release
    tag = release_tag(version)
    status, payload = request(
        "POST",
        f"{API}/repos/{repo}/releases",
        token,
        {
            "tag_name": tag,
            "name": tag,
            "body": f"Automated build of {version} by manjaro-contrib.",
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"release create failed ({status}): {payload[:200]!r}")
    return json.loads(payload)


def upload(repo: str, version: str, paths: list[str], token: str) -> None:
    release = ensure_release(repo, version, token)
    existing = {a["name"]: a["id"] for a in release.get("assets", [])}
    for path in paths:
        name = asset_name(os.path.basename(path))
        if name in existing:
            # replace, so a re-run after a partial upload converges
            request(
                "DELETE",
                f"{API}/repos/{repo}/releases/assets/{existing[name]}",
                token,
            )
        with open(path, "rb") as f:
            blob = f.read()
        status, payload = request(
            "POST",
            f"{UPLOADS}/repos/{repo}/releases/{release['id']}/assets"
            f"?name={urllib.parse.quote(name, safe='')}",
            token,
            blob,
            content_type="application/octet-stream",
        )
        if status not in (200, 201):
            raise RuntimeError(f"asset upload failed ({status}): {payload[:200]!r}")
        log(f"released {name}")


def download(
    repo: str, version: str, artifacts: list[str], dest: str, token: str
) -> None:
    release = get_release(repo, version, token)
    if release is None:
        raise RuntimeError(f"no release for {version} on {repo}")
    by_name = {a["name"]: a for a in release.get("assets", [])}
    os.makedirs(dest, exist_ok=True)
    for artifact in artifacts:
        asset = by_name.get(asset_name(artifact))
        if asset is None:
            raise RuntimeError(f"{artifact} missing from release {version}")
        status, blob = request(
            "GET",
            f"{API}/repos/{repo}/releases/assets/{asset['id']}",
            token,
            accept="application/octet-stream",
        )
        if status != 200:
            raise RuntimeError(f"asset download failed ({status})")
        # written under the true pacman filename, not the sanitised one
        with open(os.path.join(dest, artifact), "wb") as f:
            f.write(blob)
        log(f"fetched {artifact} from release {version}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["upload", "download"])
    parser.add_argument("--repo", required=True, help="owner/name of the package repo")
    parser.add_argument("--version", required=True, help="full pacman version")
    parser.add_argument("--pkg-dir", required=True)
    parser.add_argument(
        "--artifacts", help="comma-separated filenames (download only)"
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log("GITHUB_TOKEN is required")
        return 1

    if args.action == "upload":
        paths = [
            os.path.join(args.pkg_dir, n)
            for n in sorted(os.listdir(args.pkg_dir))
            if n.endswith((".pkg.tar.zst", ".pkg.tar.zst.sig"))
        ]
        if not paths:
            log("no packages to release")
            return 1
        upload(args.repo, args.version, paths, token)
    else:
        artifacts = [a.strip() for a in (args.artifacts or "").split(",") if a.strip()]
        if not artifacts:
            log("--artifacts is required for download")
            return 1
        download(args.repo, args.version, artifacts, args.pkg_dir, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
