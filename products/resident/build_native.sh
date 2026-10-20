#!/usr/bin/env bash
# Invoke only on the authorized Linux compute host, from the repo root.
set -euo pipefail
if [[ "$(uname -s)" != Linux ]]; then
  echo 'Build on the authorized Linux host; the Mac is a control terminal.' >&2
  exit 1
fi
resident_build_dir=${1:?Pass an isolated build directory}
cmake -S native -B "$resident_build_dir/native" -DCMAKE_BUILD_TYPE=Release
cmake --build "$resident_build_dir/native" -j2 --target machine_resident_gate
cp "$resident_build_dir/native/libmachine_resident_gate.so" "$resident_build_dir/libresident_gate.so"
sha256sum "$resident_build_dir/libresident_gate.so"
