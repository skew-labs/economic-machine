#!/usr/bin/env bash
set -euo pipefail
test "$(uname -s)" = Linux || { echo 'Use the remote build host.' >&2; exit 2; }
task_source=$(cd "$(dirname "$0")/../.." && pwd)
task_build=$(cd "$task_source/../build" && pwd)
test "$(df --output=avail "$task_build" | tail -1)" -gt 2097152
test "$(awk '/MemAvailable/{print $2}' /proc/meminfo)" -gt 2097152
cd "$task_source"
g++ -std=c++20 -O3 -fPIC -shared -I native/include products/pacifica_muse/native/learning.cpp -o "$task_build/libmuse_learning.so"
PYTHONPATH=src MUSE_RESEARCH_LIB="$task_build/libmuse_learning.so" python3 -m unittest discover -s tests -p 'test_muse_*.py' -v
