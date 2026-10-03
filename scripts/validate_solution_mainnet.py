"""Focused changed-code and existing integration checks, with source-bound evidence."""

import hashlib
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    "test_solution_mainnet",
    "test_solution_signer",
    "test_solution_preflight",
    "test_solution_operations.ChainBoundary",
    "test_solution_operations.LocalEVMLifecycle",
]
SOURCES = [
    "contracts/SkewSolutionMining.sol",
    "contracts/SkewVRFMock.sol",
    "src/machine_engine/solution_client.py",
    "src/machine_engine/solution_chain.py",
    "src/machine_engine/solution_operations.py",
    "src/machine_engine/solution_signer.py",
    "src/machine_engine/solution_networks.py",
    "scripts/solution_signer.py",
    "scripts/solution_mainnet_preflight.py",
    "scripts/solution_operator.py",
    "scripts/solution_cli.py",
    "scripts/compile_solution_mainnet.py",
    "scripts/validate_solution_mainnet.py",
    "tests/test_solution_mainnet.py",
    "tests/test_solution_signer.py",
    "tests/test_solution_preflight.py",
    "tests/test_solution_operations.py",
]


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote tests/evidence required")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")]
    folder = ROOT / "artifacts/solution-mainnet"
    before = fingerprints()
    compiler = json.loads((folder / "contract-build.json").read_text())
    if compiler["warnings"] or any(
        before["contracts/" + name] != sha for name, sha in compiler["sources"].items()
    ):
        raise RuntimeError("Contract source/build differs")
    if hashlib.sha256((folder / "contracts.json").read_bytes()).hexdigest() != compiler["artifact_sha256"]:
        raise RuntimeError("Compiled artifact differs")
    suite = unittest.TestLoader().loadTestsFromNames(TARGETS)
    expected = suite.countTestCases()
    start = time.monotonic()
    with (folder / "focused-tests.log").open("w") as out:
        result = unittest.TextTestRunner(stream=out, verbosity=2).run(suite)
    if before != fingerprints():
        raise RuntimeError("Sources changed during verification")
    report = {
        "schema": "solution-mainnet-validation-1",
        "accepted": result.wasSuccessful(),
        "distinct_cases": result.testsRun,
        "expected_cases": expected,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "seconds": round(time.monotonic() - start, 3),
        "targets": TARGETS,
        "source_sha256": before,
        "compiler": compiler,
        "log_sha256": hashlib.sha256((folder / "focused-tests.log").read_bytes()).hexdigest(),
        "scope": "CHANGED_CODE_AND_CONNECTION_REGRESSION_NOT_FULL_SUITE_OR_EXTERNAL_AUDIT",
        "randomness": "MOCK_ONLY",
        "signature_keys": "DISPOSABLE_PYEVM_ONLY",
        "public_deployments": 0,
        "mainnet_minted_tokens": "0",
        "public_launch_ready": False,
    }
    (folder / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"source_sha256", "compiler"}}))
    if not result.wasSuccessful() or result.testsRun != expected or result.skipped:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
