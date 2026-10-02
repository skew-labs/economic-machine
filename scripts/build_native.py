"""Build, sanitize and measure C++ only on the authorized remote host."""

import hashlib
import argparse
import json
import os
import subprocess
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True, timeout=90, **kwargs)


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Native builds run on the owner-selected remote host")
    parser = argparse.ArgumentParser()
    parser.add_argument("--program-only", action="store_true")
    args = parser.parse_args()
    build = ROOT / "build/native"
    evidence = ROOT / "artifacts/atlas-release"
    build.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    base = ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(ROOT / "native/include")]
    kernel = str(ROOT / "native/src/kernel.cpp")
    program = str(ROOT / "native/src/program.cpp")
    previous = None
    if args.program_only:
        previous = json.loads((evidence / "native-build.json").read_text())
        for relative in ["native/src/kernel.cpp", "native/include/machine/kernel.hpp",
                         "native/tests/conformance.cpp", "native/src/benchmark.cpp"]:
            if previous["source_sha256"].get(relative) != hashlib.sha256((ROOT / relative).read_bytes()).hexdigest():
                raise RuntimeError("Kernel inputs changed; run complete native verification")
    staged = build / "libmachine_kernel.next.so"
    run(base + ["-O3", "-fPIC", "-shared", kernel, program, "-o", str(staged)])
    checksum = hashlib.sha256(staged.read_bytes()).hexdigest()
    immutable = build / ("libmachine_kernel-" + checksum + ".so")
    if immutable.exists():
        if hashlib.sha256(immutable.read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Native content-address collision")
    else:
        shutil.copyfile(staged, immutable)
    staged.replace(build / "libmachine_kernel.so")
    if previous:
        functional, sanitized = previous["functional"], previous["asan_ubsan"]
    else:
        run(base + ["-O2", kernel, "native/tests/conformance.cpp", "-o", str(build / "conformance")])
        functional = json.loads(run([str(build / "conformance")]).stdout)
        run(base + ["-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", kernel,
                    "native/tests/conformance.cpp", "-o", str(build / "conformance-sanitized")])
        sanitized = json.loads(run([str(build / "conformance-sanitized")],
                                  env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1"}).stdout)
    run(base + ["-O2", program, "native/tests/program.cpp", "-o", str(build / "program-conformance")])
    program_checks = json.loads(run([str(build / "program-conformance")]).stdout)
    run(base + ["-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", program,
                "native/tests/program.cpp", "-o", str(build / "program-sanitized")])
    program_sanitized = json.loads(run([str(build / "program-sanitized")],
                                    env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1"}).stdout)
    allowed = sorted(os.sched_getaffinity(0))
    # Avoid changing system-wide scheduling; pin only our short benchmark threads.
    chosen = allowed[-2:] if len(allowed) >= 2 else [-1, -1]
    if previous:
        measurements = json.loads((evidence / "native-benchmark.json").read_text())
    else:
        run(base + ["-O3", kernel, "native/src/benchmark.cpp", "-o", str(build / "benchmark")])
        measurements = json.loads(run([str(build / "benchmark"), *map(str, chosen)]).stdout)
    topology = {}
    for cpu in allowed:
        folder = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        topology[str(cpu)] = {name: (folder / name).read_text().strip()
                             for name in ("physical_package_id", "core_id", "thread_siblings_list")}
    machine = run(["lscpu", "-J"]).stdout
    metadata = {"schema": "machine-native-build-1", "compiler": run(["g++", "--version"]).stdout.splitlines()[0],
                "machine": json.loads(machine), "cpu_topology": topology,
                "functional": functional, "asan_ubsan": sanitized,
                "program_checks": program_checks, "program_asan_ubsan": program_sanitized,
                "unchanged_kernel_tests_and_benchmark_reused": bool(previous),
                "library_sha256": hashlib.sha256((build / "libmachine_kernel.so").read_bytes()).hexdigest(),
                "immutable_library": str(immutable),
                "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in (ROOT / "native").rglob("*") if p.is_file()},
                "nic_acceleration": "NOT_IMPLEMENTED", "huge_pages": "NOT_ENABLED",
                "numa_policy": "DISCOVERED_NOT_MODIFIED", "system_irq_changes": False}
    (evidence / "native-build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (evidence / "native-benchmark.json").write_text(json.dumps(measurements, indent=2) + "\n")
    print(json.dumps({"functional": functional, "sanitized": sanitized, "kernel": measurements["kernel"],
                      "spsc_transit": measurements["spsc_transit"]}))


if __name__ == "__main__":
    main()
