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
# A split package may override arch in its own package_<name>() - nvidia-utils
# is x86_64 while its mhwd-nvidia member is any - and the artifact filename
# follows the override. Running each function in a subshell reports what
# makepkg will actually produce, rather than assuming the global value.
for _name in "${pkgname[@]}"; do
  _fn="package_${_name}"
  if declare -f "$_fn" >/dev/null 2>&1; then
    _arch=$(
      # a subshell so an override cannot leak into the next member
      eval "$(declare -f "$_fn" | sed -n '/^[[:space:]]*arch=/p')" 2>/dev/null
      printf '%s' "${arch[*]}"
    )
  else
    _arch="${arch[*]}"
  fi
  printf 'pkgarch=%s %s\n' "$_name" "${_arch:-${arch[*]}}"
done

printf 'provides=%s\n' "${provides[*]:-}"
# every dependency kind matters for build order: makedepends and
# checkdepends must exist before makepkg runs, depends before it resolves
printf 'depends=%s\n' "${depends[*]:-} ${makedepends[*]:-} ${checkdepends[*]:-}"
