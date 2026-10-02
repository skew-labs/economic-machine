"""Inspect publishable source and Git history without printing credential values."""

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(rb"(?:sk-bk-|sk-proj-|xai-|ghp_|github_pat_)[A-Za-z0-9_-]{24,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"em_live_[A-Za-z0-9_-]{32,}"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = [Path(p.decode()) for p in args.manifest.read_bytes().split(b"\0") if p]
    findings, counts = [], {"implementation": {"physical_lines": 0, "nonblank_lines": 0, "files": 0},
                            "tests": {"physical_lines": 0, "nonblank_lines": 0, "files": 0}}
    for path in paths:
        if not (ROOT / path).is_file():
            raise ValueError("manifest file missing: " + str(path))
        if path.name.startswith(".env") and path.name != ".env.example" or path.suffix in {".pem", ".key"} or ".sqlite" in path.name:
            findings.append({"path": str(path), "reason": "PRIVATE_FILE_TYPE"})
        content = (ROOT / path).read_bytes()
        if any(pattern.search(content) for pattern in PATTERNS):
            findings.append({"path": str(path), "reason": "CREDENTIAL_PATTERN"})
        if path.suffix in {".py", ".js", ".cjs", ".sol", ".html", ".css"} and path.parts[0] in {"src", "scripts", "contracts", "web", "tests"}:
            group = "tests" if path.parts[0] == "tests" else "implementation"
            lines = content.splitlines()
            counts[group]["physical_lines"] += len(lines)
            counts[group]["nonblank_lines"] += sum(bool(line.strip()) for line in lines)
            counts[group]["files"] += 1
    objects = subprocess.check_output(["git", "rev-list", "--objects", "--all"], cwd=ROOT).decode().splitlines()
    blobs = 0
    for entry in objects:
        identity, _, path = entry.partition(" ")
        kind = subprocess.check_output(["git", "cat-file", "-t", identity], cwd=ROOT).strip()
        if kind != b"blob":
            continue
        blobs += 1
        content = subprocess.check_output(["git", "cat-file", "blob", identity], cwd=ROOT)
        if any(pattern.search(content) for pattern in PATTERNS):
            findings.append({"path": path, "blob": identity, "reason": "HISTORICAL_CREDENTIAL_PATTERN"})
    report = {"inspected_files": len(paths), "historical_blobs": blobs, "findings": findings, "line_counts": counts,
              "count_scope": "Physical and nonblank source lines; excludes JSON, fixtures, logs, images, dependencies and Git objects.",
              "assurance": "Pattern and private-file inspection; not a universal secret detector or security audit."}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
