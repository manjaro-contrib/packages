#!/usr/bin/env bash
# Imports the CI signing key from GPG_SECRET_BASE64 into the caller's
# keyring and marks it ultimately trusted, so makepkg and repo-add can
# sign without prompting. The key is passphrase-less by necessity: CI has
# nowhere to type one.
set -euo pipefail

: "${GPG_SECRET_BASE64:?GPG_SECRET_BASE64 is required}"
: "${GPG_KEYID:?GPG_KEYID is required}"

base64 -d <<<"$GPG_SECRET_BASE64" | gpg --batch --quiet --import

# without ultimate ownertrust gpg refuses to sign non-interactively
gpg --batch --import-ownertrust <<<"$(gpg --list-keys --with-colons "$GPG_KEYID" |
  awk -F: '/^fpr:/ {print $10 ":6:"; exit}')"

gpg --batch --yes --detach-sign --local-user "$GPG_KEYID" -o /dev/null /dev/null
echo "signing key $GPG_KEYID ready"
