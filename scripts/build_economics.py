"""Build and verify only the new native economic primitive library remotely."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Native economic verification requires the authorized remote host")
    build = ROOT / "build/native"
    build.mkdir(parents=True, exist_ok=True)
    base = ["g++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", "native/include"]
    source = "native/src/economics.cpp"
    test = "native/tests/economics.cpp"
    run(base + ["-O2", source, test, "-o", str(build / "economics-conformance")])
    functional = json.loads(run([str(build / "economics-conformance")]))
    run(base + ["-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                source, test, "-o", str(build / "economics-sanitized")])
    sanitized = json.loads(run([str(build / "economics-sanitized")],
        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1", "UBSAN_OPTIONS": "halt_on_error=1"}))
    staged = build / "libmachine_economics.next.so"
    run(base + ["-O3", "-fPIC", "-shared", source, "-o", str(staged)])
    checksum = hashlib.sha256(staged.read_bytes()).hexdigest()
    immutable = build / ("libmachine_economics-" + checksum + ".so")
    if immutable.exists():
        if hashlib.sha256(immutable.read_bytes()).hexdigest() != checksum:
            raise RuntimeError("Native immutable path differs")
    else:
        shutil.copyfile(staged, immutable)
    staged.unlink()
    paths = [ROOT / source, ROOT / test, *sorted((ROOT / "native/include/machine/economics").glob("*.hpp"))]
    proof = {"schema": "native-economic-build-1", "functional": functional, "asan_ubsan": sanitized,
             "library_sha256": checksum, "immutable_library": str(immutable),
             "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
             "compiler": run(["g++", "--version"]).splitlines()[0],
             "execution_authority": "NONE", "scope": "DETERMINISTIC_ASSUMPTION_BASED_CANDIDATE_CALCULATIONS"}
    output = ROOT / "artifacts/atlas-release/economics-build.json"
    output.write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
