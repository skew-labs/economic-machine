#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Linux ]] || { echo 'Use a Linux build host' >&2; exit 1; }
cd "$(dirname "$0")"
mkdir -p build evidence
runtime_include=../../runtime/include
g++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Werror -Wno-misleading-indentation -fPIC -shared -I "$runtime_include" evolution/native.cpp -o build/libevolution.so
g++ -std=c++20 -O3 -DNDEBUG -Wall -Wextra -Werror -Wno-misleading-indentation -fPIC -shared -I "$runtime_include" ${MP_RESEARCH_CXXFLAGS:-} research/native.cpp ${MP_RESEARCH_LDFLAGS:--lsqlite3 -lcrypto} -Wl,-z,defs -o build/libmachine_research.so
