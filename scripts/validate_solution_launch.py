"""Cross-check focused evidence; a prerelease packet is not production certification."""

import hashlib
import json
import re
import time
from pathlib import Path

from economic_machine.values import digest

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    target = ROOT / path
    if target.is_symlink() or target.stat().st_size > 5000000:
        raise RuntimeError("Bounded evidence required")
    return json.loads(target.read_bytes())


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote evidence verification only")
    folder = ROOT / "artifacts/solution-launch"
    current = (folder / "tests.log").read_text()
    if not re.search(r"Ran 17 tests in [0-9.]+s\s+OK\s*$", current):
        raise RuntimeError("Current focused tests did not pass")
    first = (folder / "tests-first-pass.log").read_text()
    names = re.findall(r"^([^\n]+) \.\.\. ok$", current, re.MULTILINE)
    names += re.findall(
        r"^(test_[^\n]+ \(test_solution_launch\.Launch\.[^\n]+\)) \.\.\. ok$", first, re.MULTILINE
    )
    publishing = (folder / "publishing-tests.log").read_text()
    if not re.search(r"Ran 3 tests in [0-9.]+s\s+OK\s*$", publishing):
        raise RuntimeError("Public metadata connection tests did not pass")
    names += re.findall(r"^([^\n]+) \.\.\. ok$", publishing, re.MULTILINE)
    if len(set(names)) != 24:
        raise RuntimeError("Focused current evidence case count differs")
    soak = read("artifacts/solution-launch/soak-complete.json")
    crash = read("artifacts/solution-launch/soak-crash-checkpoint.json")
    build = read("artifacts/solution-operations/native-build.json")
    qualifier_hash = hashlib.sha256((ROOT / "scripts/qualify_solution_runtime.py").read_bytes()).hexdigest()
    if (
        soak["state"] != "COMPLETE"
        or soak["binding"]["requested_seconds"] != 180
        or soak["verified_elapsed_ns"] < 180 * 10**9
        or soak["score_or_frame_failures"]
        or soak["resumes"] < 1
        or not soak["injected_worker_terminations"]
        or soak["binding"]["pipeline_sha256"] != build["sha256"]
        or soak["binding"]["qualifier_source_sha256"] != qualifier_hash
        or soak["jobs"] <= crash["jobs"]
        or soak["verified_elapsed_ns"] <= crash["verified_elapsed_ns"]
    ):
        raise RuntimeError("Crash/restart duration, parity, source or binary evidence differs")
    checkpoint_hash = soak.pop("checkpoint_sha256")
    if checkpoint_hash != digest(soak):
        raise RuntimeError("Completed checkpoint fingerprint differs")
    vrf = read("artifacts/solution-launch/vrf-public-preflight.json")
    fingerprint = vrf.pop("review_sha256")
    if (
        digest(vrf) != fingerprint
        or vrf["chain_id"] != 421614
        or vrf["transactions_submitted"]
        or vrf["tokens_minted"]
    ):
        raise RuntimeError("Read-only real-coordinator preflight evidence differs")
    ongoing = read("artifacts/solution-launch/qualification-24h-observation.json")
    if (
        ongoing["binding"]["requested_seconds"] != 86400
        or ongoing["binding"]["qualifier_source_sha256"] != qualifier_hash
        or ongoing["score_or_frame_failures"]
        or ongoing["binding"]["pipeline_sha256"] != build["sha256"]
    ):
        raise RuntimeError("Long qualification configuration differs")
    keys = [
        "scripts/qualify_solution_runtime.py",
        "scripts/prepare_solution_launch.py",
        "scripts/solution_recovery.py",
        "src/machine_engine/solution_launch.py",
        "src/machine_engine/solution_recovery.py",
        "src/machine_engine/solution_operations.py",
        "tests/test_solution_launch.py",
    ]
    report = {
        "schema": "solution-launch-validation-1",
        "observed_at": int(time.time()),
        "distinct_changed_and_connection_tests": 24,
        "native_pipeline_sha256": build["sha256"],
        "qualification_180_seconds_complete": True,
        "parent_crash_checkpoint_resumed": True,
        "native_worker_terminations": soak["injected_worker_terminations"],
        "verified_jobs": soak["jobs"],
        "qualified_elapsed_ns": soak["verified_elapsed_ns"],
        "score_or_frame_failures": 0,
        "qualification_24h_state_at_observation": ongoing["state"],
        "qualification_24h_verified_elapsed_ns_at_observation": ongoing["verified_elapsed_ns"],
        "qualification_24h_completed": ongoing["state"] == "COMPLETE"
        and ongoing["verified_elapsed_ns"] >= 86400 * 10**9,
        "actual_vrf_coordinator_read": True,
        "actual_subscription_configured": vrf["subscription"] is not None,
        "off_host_restore_verified": False,
        "independent_security_review": "NOT_OBTAINED",
        "public_mining_deployment": None,
        "chain_transactions": 0,
        "tokens_minted": 0,
        "release_state": "PRE_RELEASE_PUBLIC_ISSUANCE_BLOCKED",
        "sources": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in keys},
    }
    report["validation_sha256"] = digest(report)
    (folder / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
