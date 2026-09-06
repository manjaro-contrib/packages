# manjaro-contrib packages

A signed pacman repository for Manjaro, built centrally from package
repositories across the `manjaro-contrib` organization.

Package repos hold nothing but a `PKGBUILD` and carry the topic
`manjaro-contrib-pkg`. This repository does the rest: it discovers them,
builds what is missing, signs it, publishes it to R2, and tracks what is
waiting to move between branches. No workflow files live in the package
repos themselves.

Served from <https://packages.manjaro.download>.

## Using the repository

Import and locally sign the repository key. It is not published to a
keyserver, so fetch it from this repository:

```sh
curl -O https://raw.githubusercontent.com/manjaro-contrib/packages/main/gpg-public-key.asc
sudo pacman-key --add gpg-public-key.asc
sudo pacman-key --lsign-key E8AAE31963A022B8480CC007A13A52A61B5D8836
```

Until the key is trusted, `pacman -Sy` fails with `invalid or corrupted
database (PGP signature)`. That is the signature check working, not a
broken mirror.

Then add the repository to `/etc/pacman.conf`, above `[core]`:

```ini
[manjaro-contrib]
SigLevel = Required DatabaseRequired
Server = https://packages.manjaro.download/unstable/$arch
```

Swap `unstable` for `testing` or `stable` to follow a slower branch.
`SigLevel = Required DatabaseRequired` verifies both the packages and the
database index — without `DatabaseRequired` a signed package set can still
be rolled back or have entries removed. Both are signed, so require both.

Finally:

```sh
sudo pacman -Sy
```

## Branches

Packages are built once into `unstable` and then promoted as binaries; they
are never rebuilt for a branch. Promotion is strictly one-directional:

```
unstable -> testing -> stable
```

## How it works

```mermaid
flowchart LR
  R[package repos<br/>topic: manjaro-contrib-pkg] --> C[check]
  C -->|missing artifact| B[build]
  B --> P[publish]
  P --> U[(unstable)]
  U -->|promote| T[(testing)]
  T -->|promote| S[(stable)]
  B -.->|store binary| GH[GitHub releases]
  GH -.->|reuse instead of rebuild| B
```

State lives in the bucket, not in a database. A package needs building when
its `PKGBUILD` version has no matching artifact under the branch prefix, so
the check is a stateless comparison that survives a lost run or a manual
upload.

## Workflows

| Workflow | Trigger | Does |
| --- | --- | --- |
| `build-publish` | cron, dispatch, `repository_dispatch` | discovers, builds, signs, publishes to `unstable` |
| `promote` | manual | copies binaries between branches server-side |
| `repo-remove` | manual | deletes a package from branches and the database |
| `update-upstreams` | cron | opens PRs when a tracked AUR package changes |
| `promotion-backlog` | cron, after the above | keeps one issue per branch listing what is pending |

Every workflow that writes the database shares the `repo-publish`
concurrency group, so publishes, promotions and removals queue rather than
clobber each other.

### Promoting

Run the `promote` workflow with `dry_run` ticked first. It classifies each
package against the target branch as new, replace, overwrite, identical or
missing, and writes nothing, which is the only safe way to review an `ALL`
sync before it happens.

### Adding a package

Create a repository containing a `PKGBUILD`, add the topic
`manjaro-contrib-pkg`, and it is picked up on the next run. To track an AUR
Add it to [`packages.yml`](packages.yml), which inventories every package
this repository builds. Give it an `upstream` to have update PRs opened
automatically when that source moves; omit it for packages maintained
here.

## Repository layout

```
scripts/          the engine; each script is runnable on its own
worker/           cloudflare worker serving the bucket with directory listings
packages.yml      every package built here, and what it tracks
gpg-public-key.asc  the repository signing key
```

Objects are laid out as `<branch>/<arch>/`, alongside BoxIt-style `state`
files at the root and per branch, mirroring what Manjaro's own mirrors
serve so mirror tooling can poll a hash instead of walking the tree.

## Operating

Configuration lives in GitHub. Variables: `REPO_URL`, `GPG_KEYID`.
Secrets: `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET`, `GPG_SECRET_BASE64`, `DISPATCH_TOKEN`.

`DISPATCH_TOKEN` is a fine-grained token over the organization needing
Contents read/write, Pull requests read/write, and Issues read/write.

The scripts run locally against the same environment variables, which is
the fastest way to check a change before pushing it:

```sh
set -a && . ./.env && set +a
GITHUB_TOKEN="$DISPATCH_TOKEN" python3 scripts/check_updates.py \
  --org manjaro-contrib --repo-url "$REPO_URL"
```

The signing key is passphrase-less so unattended builds can use it. Anyone
with access to the repository secrets can therefore sign packages; treat
`GPG_SECRET_BASE64` as production credentials and keep the revocation
certificate somewhere durable.
