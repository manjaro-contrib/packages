"""Publish BoxIt-style state files describing the repository contents.

Mirrors what Manjaro's own mirrors serve: a per-branch `<branch>/state` and
a global `/state`, each carrying a hash that changes whenever the branch
content changes. Mirror-sync tooling polls these instead of diffing whole
trees, so they must be rewritten by every operation that touches packages.
"""

import datetime
import hashlib

BRANCHES = ["unstable", "testing", "stable"]

GLOBAL_TEMPLATE = """\
###
### BoxIt global state file
###

# Unique hash code representing current repository state.
# This hash code changes in a frequent interval.
state={state}

# Date and time of the last state update.
date={date}"""

BRANCH_TEMPLATE = """\
###
### BoxIt branch state file
###

# Unique hash code representing current branch state.
# This hash code changes as soon as anything changes in this branch.
state={state}

# Date and time of the last branch change.
date={date}"""


def _timestamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def branch_digest(s3, bucket: str, branch: str) -> str:
    """Hash of every object in a branch, keyed by name, size and etag.

    Content-addressed rather than time-based: republishing identical bytes
    leaves the hash alone, so mirrors only resync on a real change.
    """
    entries = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{branch}/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/state"):
                continue
            etag = obj["ETag"].strip('"')
            entries.append(f"{obj['Key']} {obj['Size']} {etag}")
    digest = hashlib.sha1()
    for entry in sorted(entries):
        digest.update(entry.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def write_state(s3, bucket: str, log=lambda _msg: None) -> None:
    """Rewrite every branch state file plus the global one."""
    now = _timestamp()
    digests = {}
    for branch in BRANCHES:
        digest = branch_digest(s3, bucket, branch)
        digests[branch] = digest
        s3.put_object(
            Bucket=bucket,
            Key=f"{branch}/state",
            Body=BRANCH_TEMPLATE.format(state=digest, date=now).encode(),
            ContentType="text/plain",
        )
        log(f"state {branch}={digest}")

    combined = hashlib.sha1()
    for branch in BRANCHES:
        combined.update(f"{branch} {digests[branch]}\n".encode())
    s3.put_object(
        Bucket=bucket,
        Key="state",
        Body=GLOBAL_TEMPLATE.format(state=combined.hexdigest(), date=now).encode(),
        ContentType="text/plain",
    )
    log(f"state global={combined.hexdigest()}")
