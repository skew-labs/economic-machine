#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo 'Use a Linux build host' >&2; exit 1; }
cd "$(dirname "$0")"
layout=${1:-wide256}
case "$layout" in
  compact16) features=() ;;
  wide256) features=(-- --features wide256) ;;
  production) features=(-- --features production) ;;
  *) echo 'Expected compact16, wide256 or production' >&2; exit 1 ;;
esac
if [[ "$layout" == production && ! -f program/src/oracle.rs ]]; then exit 1; fi
cd program
cargo-build-sbf --tools-version v1.57 --arch v0 --jobs 2 --lto --sbf-out-dir ../build/sbf "${features[@]}"
