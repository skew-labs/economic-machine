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
    paths.update({"README.md", "LICENSE", "pyproject.toml", "requirements-verified.txt", "docs/ATLAS_DATAPASS_NATIVE.md", "docs/AGENT_CONTROL.md", "docs/ECONOMIC_PRIMITIVES.md"})
    for name in ["atlas.json", "brief.json", "source-audit.json", "native-build.json",
                 "native-benchmark.json", "economics-build.json", "economics-integration-tests.log", "economics-portal-final.log", "economics-live-check.json",
                 "datapass-build.json", "datapass-tests.log", "integration-tests.log",
                 "agent-control-tests.log", "agent-focused-tests.log", "agent-portal-tests.log", "agent-control-validation.json", "native-integration-tests.log", "dataset-scope-tests.log", "final-source-tests.log", "line-count.json", "live-web-check.json"]:
        paths.add("artifacts/atlas-release/" + name)
    paths.update({"artifacts/arbitrum-sepolia/datapass-deployment.json", "artifacts/arbitrum-sepolia/datapass-deployment-review.json"})
    paths.update({"artifacts/atlas-release/economics-independent-build.log",
                  "artifacts/atlas-release/economics-independent-tests.log",
                  "artifacts/atlas-release/economics-independent-verification.json",
                  "docs/ECONOMIC_RELEASE_20261003.md"})
    for path in (ROOT / "artifacts/atlas-release/screenshots").iterdir():
        if path.suffix in {".png", ".jpg"}:
            paths.add(str(path.relative_to(ROOT)))
    paths.add("deploy/native-economics.conf")
    paths.update({"docs/CONSOLE_UX_20261003.md", "artifacts/atlas-release/console-ux-browser.json",
                  "artifacts/atlas-release/console-ux-site-deployment.json"})
    paths.add("docs/SERVICE_COMMERCE.md")
    paths.add("docs/LANDING_20261003.md")
    paths.add("artifacts/atlas-release/landing-route-tests.log")
    for path in (ROOT / "artifacts/landing-20261003").iterdir():
        if path.is_file() and path.suffix in {".json", ".jpg", ".png"}:
            paths.add(str(path.relative_to(ROOT)))
    for name in ["commerce-checkout-tests.log", "commerce-portal-tests.log", "commerce-portal-tests.initial.log",
                 "commerce-recovery-tests.log", "commerce-wallet-tests.log", "commerce-native-tests.log",
                 "commerce-wallet-native-tests.log", "commerce-cache-tests.log", "commerce-live-check.json", "commerce-browser.json"]:
        paths.add("artifacts/atlas-release/" + name)
    files = [file_evidence(ROOT, path) for path in sorted(paths)]
    report = json.loads((ROOT / "artifacts/atlas-release/atlas.json").read_text())
    audit = json.loads((ROOT / "artifacts/atlas-release/source-audit.json").read_text())
    if not audit["accepted"] or audit["report_sha256"] != report["report_sha256"]:
        raise RuntimeError("Source audit does not match report")
    deployment = json.loads((ROOT / "artifacts/arbitrum-sepolia/datapass-deployment.json").read_text())
    unsigned_proof = {key: value for key, value in deployment.items() if key != "proof_sha256"}
    if (digest(unsigned_proof) != deployment["proof_sha256"] or deployment["status"] != "PUBLIC_TESTNET_DEPLOYED"
            or deployment["purchase_executed"] or deployment["network"] != "eip155:421614"
            or len(deployment["observations"]) != 2):
        raise RuntimeError("Public deployment proof is inconsistent")
    observations = deployment["observations"]
    if (observations[0]["rpc"] == observations[1]["rpc"] or any(
            row["receipt_status"] != 1 or row["finalized_block"] < row["block_number"]
            or row["contract_address"] != deployment["contract_address"] or row["tx_hash"] != deployment["tx_hash"]
            or row["block_hash"] != observations[0]["block_hash"] or row["runtime_sha256"] != observations[0]["runtime_sha256"]
            for row in observations)):
        raise RuntimeError("Deployment observations disagree")
    build = json.loads((ROOT / "artifacts/atlas-release/datapass-build.json").read_text())
    if (observations[0]["runtime_sha256"] != build["contracts"]["SkewDataPass"]["runtime_sha256"]
            or deployment["source_sha256"] != build["sources"]["SkewDataPass.sol"]):
        raise RuntimeError("Public deployment and compiled source differ")
    lines = json.loads((ROOT / "artifacts/atlas-release/line-count.json").read_text())
    body = {"schema": "machine-release-proof-1", "created_at": int(time.time()), "files": files,
            "claims": {"report_sha256": report["report_sha256"], "observations": audit["observations_checked"],
                       "arbitrum_datapass_public_deployment": deployment["status"],
                       "datapass_contract": deployment["contract_address"], "deployment_proof_sha256": deployment["proof_sha256"],
                       "live_new_datapass_purchases": 0, "economic_operations": 20,
                       "engine_implementation_nonblank": lines["engine_implementation"]["nonblank"],
                       "target_30000_engine_nonblank_reached": lines["target_30000_engine_nonblank_reached"],
                       "native_authority": "CANDIDATE_ONLY", "agent_control": "SAME_ENGINE_DATABASE_SHARED_USDT_TURNOVER", "raw_archives_publicly_resold": False},
            "assurance": "SOURCE_AUDIT_PLUS_REPRODUCIBLE_LOCAL_EVM_AND_NATIVE_TESTS_NOT_PRODUCTION_CERTIFICATION"}
    manifest = body | {"manifest_sha256": digest(body)}
    verify_manifest(manifest)
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"files": len(files), "manifest_sha256": manifest["manifest_sha256"]}))


if __name__ == "__main__":
    main()
