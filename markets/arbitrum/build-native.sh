#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo 'Use a Linux build host' >&2; exit 1; }
cd "$(dirname "$0")"
mkdir -p build
runtime_include=../runtime/include
g++ -std=c++20 -O3 -DNDEBUG -shared -fPIC -I "$runtime_include" agent/engine.cpp -o build/libmachine_arbitrum.so
g++ -std=c++20 -O3 -I "$runtime_include" agent/engine.cpp agent/verify.cpp -o build/verify-native
build/verify-native
