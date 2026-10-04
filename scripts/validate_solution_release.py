"""Focused release administration and signer regression evidence; never deploys."""

import collections
import hashlib
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    "src/machine_engine/solution_setup.py",
    "src/machine_engine/solution_signer.py",
    "scripts/solution_setup.py",
    "scripts/validate_solution_release.py",
    "tests/test_solution_setup.py",
    "tests/test_solution_signer.py",
    "tests/test_solution_mainnet.py",
    "contracts/SkewSolutionMining.sol",
]


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote verification required")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")]
    folder = ROOT / "artifacts/solution-release"
    folder.mkdir(parents=True, exist_ok=True)
    before = hashes()
    linter = "/srv/skew/economic-machine-commerce-20261002/tools/browser-qa-venv/bin/ruff"
    lint = subprocess.run(
        [linter, "check", *[name for name in SOURCES if name.endswith(".py")]],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    (folder / "lint.log").write_text(lint.stdout + lint.stderr)
    start = time.monotonic()
    suite = unittest.TestLoader().loadTestsFromNames(["test_solution_setup", "test_solution_signer"])
    with (folder / "focused-tests.log").open("w") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    if before != hashes():
        raise RuntimeError("Source changed during release verification")
    scan = json.loads((folder / "security/slither.json").read_text())
    findings = scan.get("results", {}).get("detectors", [])
    if scan.get("success") is not True:
        raise RuntimeError("Static analysis did not complete")
    report = {
        "schema": "solution-release-validation-1",
        "accepted": result.wasSuccessful(),
        "distinct_cases": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - start, 3),
        "source_sha256": before,
        "lint_passed": True,
        "test_log_sha256": hashlib.sha256((folder / "focused-tests.log").read_bytes()).hexdigest(),
        "static_analysis": {
            "tool": "Slither 0.11.3",
            "severity_counts": dict(collections.Counter(f["impact"] for f in findings)),
            "json_sha256": hashlib.sha256((folder / "security/slither.json").read_bytes()).hexdigest(),
            "findings_are_not_automatically_dismissed": True,
        },
        "scope": "UNSIGNED_VRF_ADMIN_AND_CHANGED_SIGNER_CONNECTIONS",
        "public_transactions": 0,
        "public_deployments": 0,
        "link_transferred_juels": "0",
        "independent_security_review_completed": False,
        "operating_wallet_configured": False,
        "public_launch_ready": False,
    }
    (folder / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"source_sha256"}}))
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
