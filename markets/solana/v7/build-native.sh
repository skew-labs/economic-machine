#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo 'Use a Linux build host' >&2; exit 1; }
cd "$(dirname "$0")"
mkdir -p build
runtime_include=../../runtime/include
g++ -std=c++20 -O3 -Wall -Wextra -Werror -fPIC -shared -I "$runtime_include" agents/native/mm.cpp -o build/libmachine_mm.so
g++ -std=c++20 -O3 -Wall -Wextra -Werror -fPIC -shared -I "$runtime_include" agents/native/program.cpp -o build/libmachine_program.so
