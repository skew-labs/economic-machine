#!/usr/bin/env bash
set -euo pipefail
test "$(uname -s)" = Linux || { echo 'Use the remote build host.' >&2; exit 2; }
task_root=$(cd "$(dirname "$0")/../.." && pwd)
task_build=$(cd "$task_root/../build" && pwd)
test "$(df --output=avail "$task_build" | tail -1)" -gt 2097152
test "$(awk '/MemAvailable/{print $2}' /proc/meminfo)" -gt 2097152
flags=(-std=c++20 -O2 -pthread -I"$task_root/native/include" -I"$task_root/venue-fixture"
       -I"$task_build/deps/usr/include" -I"$task_build/curl/include")
g++ "${flags[@]}" "$task_root/products/pacifica_muse/native/agent.cpp" -o "$task_build/muse-maker" \
    -L"$task_build/curl/lib" '-Wl,-rpath,$ORIGIN/curl/lib' -lcurl -lcrypto -lssl
for task_test in session policy; do
    g++ "${flags[@]}" "$task_root/products/pacifica_muse/native/${task_test}_checks.cpp" \
        -o "$task_build/muse-${task_test}-checks" -lcrypto
    "$task_build/muse-${task_test}-checks"
done
