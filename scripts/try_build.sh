#!/usr/bin/env bash
# Build one package locally in the same container CI uses, for either
# architecture, without pushing anything.
#
# The point is the round trip: a CI build costs minutes and a queue slot,
# and an architecture-specific failure is otherwise only reproducible by
# pushing. This runs the same makepkg in the same image.
#
# aarch64 needs binfmt_misc handlers on the host. Docker Desktop and
# rootful Docker have them; rootless does not, and cannot register them,
# so the script says so rather than failing inside the container with
# "exec format error".
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: try_build.sh [--arch x86_64|aarch64] [--repo NAME | --dir PATH]

  --arch   architecture to build for (default: the host's)
  --repo   package repository under the org, cloned fresh
  --dir    a local directory holding a PKGBUILD
  --keep   leave the container running on failure, to poke at it
  --shell  drop into a shell in the build container instead of building

examples:
  scripts/try_build.sh --repo pacseek --arch aarch64
  scripts/try_build.sh --dir . --arch x86_64
  scripts/try_build.sh --repo pacseek --arch aarch64 --shell
EOF
  exit 2
}

ORG="${ORG:-manjaro-contrib}"
IMAGE="${IMAGE:-ghcr.io/$ORG/packages/builder:latest}"
ARCH="$(uname -m)"
REPO="" DIR="" KEEP=0 SHELL_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH="${2:?}"; shift 2 ;;
    --repo) REPO="${2:?}"; shift 2 ;;
    --dir) DIR="${2:?}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --shell) SHELL_ONLY=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$REPO" ] || [ -n "$DIR" ] || usage
[ -z "$REPO" ] || [ -z "$DIR" ] || { echo "--repo and --dir are exclusive" >&2; exit 2; }

case "$ARCH" in
  x86_64) platform=linux/amd64 ;;
  aarch64) platform=linux/arm64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 2 ;;
esac

# a foreign architecture needs an emulator the kernel knows about; the
# container cannot supply one for itself
if [ "$ARCH" != "$(uname -m)" ]; then
  handler=$([ "$ARCH" = aarch64 ] && echo qemu-aarch64 || echo qemu-x86_64)
  if [ ! -e "/proc/sys/fs/binfmt_misc/$handler" ]; then
    cat >&2 <<EOF
$ARCH needs the $handler binfmt handler, which is not registered.

Register it once (needs root, persists until reboot):
  sudo docker run --privileged --rm tonistiigi/binfmt --install $ARCH

Rootless Docker cannot register handlers: the install runs inside a
container and never reaches the host. Use a rootful daemon for this, or
push a branch and let CI build the architecture natively.
EOF
    exit 1
  fi
fi

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

if [ -n "$REPO" ]; then
  echo "## cloning $ORG/$REPO"
  git clone -q --depth 1 "https://github.com/$ORG/$REPO" "$workdir/pkg"
else
  cp -r "$DIR" "$workdir/pkg"
fi
[ -f "$workdir/pkg/PKGBUILD" ] || { echo "no PKGBUILD in the source" >&2; exit 1; }

# the image is private to the org, so a pull needs a login; say that
# rather than letting docker report a bare "unauthorized"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if ! docker pull -q --platform "$platform" "$IMAGE" >/dev/null 2>&1; then
    cat >&2 <<EOF
cannot pull $IMAGE

It is private to the organisation. Log in once with a token that has
read:packages:
  echo \$GITHUB_TOKEN | docker login ghcr.io -u \$USER --password-stdin

Or point IMAGE at something else:
  IMAGE=manjarolinux/base:latest scripts/try_build.sh ...
which works for packages whose makedepends the base image already has.
EOF
    exit 1
  fi
fi

echo "## building for $ARCH in $IMAGE"

# --nocheck matches the pipeline: check() needs a display or a network for
# several of these, and CI does not run it either
# the prebuilt image already has base-devel and the builder account; any
# other image has to be bootstrapped, or makepkg -s cannot install
# makedepends and refuses to run as root
build_cmd='
set -e
# a fresh container has no package databases, so makepkg -s cannot resolve
# a single makedepend until they are synced. The prebuilt image is already
# up to date, so this costs nothing there.
# a fresh container has no package databases, so makepkg -s cannot resolve
# a single makedepend until they are synced. base-devel is installed
# unconditionally: manjarolinux/base carries makepkg and a builder user
# but not gcc, so testing for either proves nothing. --needed makes this
# a no-op on the prebuilt image.
pacman -Syu --noconfirm --needed base-devel git sudo >/dev/null
id builder >/dev/null 2>&1 || useradd -m builder
echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
cp -r /src /home/builder/pkg
chown -R builder:builder /home/builder/pkg
cd /home/builder/pkg
sudo -u builder makepkg -s --noconfirm --nocheck
ls -la ./*.pkg.tar.zst
'
[ "$SHELL_ONLY" -eq 1 ] && build_cmd='cp -r /src /home/builder/pkg; chown -R builder:builder /home/builder/pkg; cd /home/builder/pkg; exec bash'

run_args=(--rm --platform "$platform" -v "$workdir/pkg":/src:ro
          --security-opt seccomp=unconfined -w /home/builder)
[ "$SHELL_ONLY" -eq 1 ] && run_args+=(-it)
[ "$KEEP" -eq 1 ] && run_args=("${run_args[@]/--rm/}")

if docker run "${run_args[@]}" "$IMAGE" bash -c "$build_cmd"; then
  echo "## $ARCH: built"
else
  status=$?
  echo "## $ARCH: FAILED (exit $status)" >&2
  echo "   re-run with --shell to inspect the build environment" >&2
  exit "$status"
fi
