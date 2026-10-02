"""Create or independently verify the release artifact manifest on Canada.

Run with --verify after checkout to catch changed source or evidence. Reproduce
the underlying tests/source audit to assess claims; a manifest is not a proof
that the compiler/test runner or upstream provider was honest.
"""

import argparse
import json
import time
from pathlib import Path

from economic_machine.values import digest
from machine_commerce.release_proof import file_evidence, verify_manifest

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Release verification runs on the remote host")
    parser = argparse.ArgumentParser(); parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    out = ROOT / "artifacts/atlas-release/manifest.json"
    if args.verify:
        print(json.dumps(verify_manifest(json.loads(out.read_text())))); return
    paths = set()
    for folder, suffixes in [("src", {".py"}), ("native", {".hpp", ".cpp", ".txt"}),
                             ("contracts", {".sol"}), ("web", {".js", ".css", ".html", ".svg"}),
                             ("scripts", {".py", ".mjs"}), ("tests", {".py", ".cjs"}),
                             ("site", {".html", ".css", ".js", ".json", ".svg"})]:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                paths.add(str(path.relative_to(ROOT)))
    paths.update({"README.md", "LICENSE", "pyproject.toml", "requirements-verified.txt", "docs/ATLAS_DATAPASS_NATIVE.md", "docs/AGENT_CONTROL.md"})
    for name in ["atlas.json", "brief.json", "source-audit.json", "native-build.json",
                 "native-benchmark.json", "datapass-build.json", "datapass-tests.log", "integration-tests.log",
                 "agent-control-tests.log", "agent-focused-tests.log", "agent-portal-tests.log", "agent-control-validation.json", "native-integration-tests.log", "dataset-scope-tests.log", "final-source-tests.log", "line-count.json", "live-web-check.json"]:
        paths.add("artifacts/atlas-release/" + name)
    for path in (ROOT / "artifacts/atlas-release/screenshots").glob("*.png"):
        paths.add(str(path.relative_to(ROOT)))
    files = [file_evidence(ROOT, path) for path in sorted(paths)]
    report = json.loads((ROOT / "artifacts/atlas-release/atlas.json").read_text())
    audit = json.loads((ROOT / "artifacts/atlas-release/source-audit.json").read_text())
    if not audit["accepted"] or audit["report_sha256"] != report["report_sha256"]:
        raise RuntimeError("Source audit does not match report")
    body = {"schema": "machine-release-proof-1", "created_at": int(time.time()), "files": files,
            "claims": {"report_sha256": report["report_sha256"], "observations": audit["observations_checked"],
                       "arbitrum_datapass_public_deployment": "NOT_DEPLOYED", "live_new_datapass_purchases": 0,
                       "native_authority": "CANDIDATE_ONLY", "agent_control": "SAME_ENGINE_DATABASE_SHARED_USDT_TURNOVER", "raw_archives_publicly_resold": False},
            "assurance": "SOURCE_AUDIT_PLUS_REPRODUCIBLE_LOCAL_EVM_AND_NATIVE_TESTS_NOT_PRODUCTION_CERTIFICATION"}
    manifest = body | {"manifest_sha256": digest(body)}
    verify_manifest(manifest)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": len(files), "manifest_sha256": manifest["manifest_sha256"]}))


if __name__ == "__main__":
    main()
