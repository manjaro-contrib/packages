#!/usr/bin/env bash
# Sources a PKGBUILD in this throwaway subprocess and prints its identity
# variables as key=value lines for the python tooling to consume.
# Sourcing executes arbitrary shell from the package repo — callers must
# treat this as untrusted-code execution and run it accordingly.
set -euo pipefail

pkgbuild=$1
cd "$(dirname "$pkgbuild")"

# shellcheck disable=SC1091
source "./$(basename "$pkgbuild")"

printf 'pkgbase=%s\n' "${pkgbase:-${pkgname[0]}}"
printf 'pkgname=%s\n' "${pkgname[*]}"
printf 'pkgver=%s\n' "$pkgver"
printf 'pkgrel=%s\n' "$pkgrel"
printf 'epoch=%s\n' "${epoch:-}"
printf 'arch=%s\n' "${arch[*]}"
printf 'provides=%s\n' "${provides[*]:-}"
# every dependency kind matters for build order: makedepends and
# checkdepends must exist before makepkg runs, depends before it resolves
printf 'depends=%s\n' "${depends[*]:-} ${makedepends[*]:-} ${checkdepends[*]:-}"
