#!/usr/bin/env python3
"""Print a short-lived installation token for the GitHub App.

Workflows get this from actions/create-github-app-token; this is the local
equivalent, so the scripts can be run by hand against the same identity
rather than needing a personal access token kept around for the purpose.
"""

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

API = "https://api.github.com"


def b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def app_jwt(app_id: str, pem: str) -> str:
    now = int(time.time())
    signing_input = (
        b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        + b"."
        # backdated a minute to tolerate clock skew against github
        + b64url(json.dumps({"iat": now - 60, "exp": now + 300, "iss": app_id}).encode())
    )
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem)
        keyfile = f.name
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", keyfile, "-binary"],
            input=signing_input,
            capture_output=True,
            check=True,
        )
    finally:
        os.unlink(keyfile)
    return (signing_input + b"." + b64url(proc.stdout)).decode()


def get(path: str, token: str, method: str = "GET"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="manjaro-contrib")
    parser.add_argument(
        "--key",
        default="manjaro-contrib-bot.2026-09-06.private-key.pem",
        help="path to the app private key",
    )
    args = parser.parse_args()

    app_id = os.environ.get("APP_ID")
    if not app_id:
        print("APP_ID is required", file=sys.stderr)
        return 1
    key = pathlib.Path(args.key)
    if not key.exists():
        print(f"{key} not found", file=sys.stderr)
        return 1

    jwt = app_jwt(app_id, key.read_text())
    installations = get("/app/installations", jwt)
    for inst in installations:
        if inst["account"]["login"] == args.org:
            token = get(
                f"/app/installations/{inst['id']}/access_tokens", jwt, "POST"
            )["token"]
            print(token)
            return 0
    print(f"app is not installed on {args.org}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
