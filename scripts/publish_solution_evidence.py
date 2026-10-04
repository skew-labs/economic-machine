"""Export only allowlisted synthetic qualification metadata with public read access."""

import argparse
import json
import os
import re
import secrets
from pathlib import Path

from economic_machine.values import MachineError, digest

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "schema",
    "binding",
    "state",
    "verified_elapsed_ns",
    "jobs",
    "batches",
    "injected_worker_terminations",
    "resumes",
    "score_or_frame_failures",
    "peak_child_rss_kib",
    "round_trip_buckets_ns",
    "native_buckets_ns",
    "maximum_round_trip_ns",
    "maximum_native_ns",
    "paid_api_calls",
    "chain_transactions",
    "checkpoint_sha256",
    "p99_round_trip_upper_bound_ns",
    "p99_native_upper_bound_ns",
    "failure_type",
}
BINDING = {
    "qualifier_source_sha256",
    "pipeline_sha256",
    "fixture_sha256",
    "requested_seconds",
    "fault_injection_every_batches",
    "interval_milliseconds",
}


def publish(source, target):
    source, target = Path(source), Path(target)
    scope = (ROOT / "artifacts/solution-launch").resolve()
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > 65536
        or target.is_symlink()
        or target.parent.is_symlink()
        or not target.parent.resolve().is_relative_to(scope)
        or target.name
        not in {
            "soak-initial.json",
            "soak-complete.json",
            "soak-crash-checkpoint.json",
            "qualification-24h-observation.json",
        }
    ):
        raise MachineError("SOLUTION_PUBLIC_EVIDENCE_PATH")
    raw = json.loads(source.read_bytes())
    if (
        not isinstance(raw, dict)
        or set(raw) - FIELDS
        or raw.get("schema") != "solution-qualification-1"
        or not isinstance(raw.get("binding"), dict)
        or set(raw["binding"]) - BINDING
    ):
        raise MachineError("SOLUTION_PUBLIC_EVIDENCE_FIELDS")
    body = {key: value for key, value in raw.items() if key != "checkpoint_sha256"}
    if raw.get("checkpoint_sha256") != digest(body):
        raise MachineError("SOLUTION_PUBLIC_EVIDENCE_HASH")
    if raw.get("state") not in {"RUNNING", "PAUSED", "COMPLETE", "FAILED"}:
        raise MachineError("SOLUTION_PUBLIC_EVIDENCE_STATE")
    for name, value in raw["binding"].items():
        if name.endswith("sha256"):
            if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
                raise MachineError("SOLUTION_PUBLIC_EVIDENCE_HASH_FIELD")
        elif type(value) is not int or not 0 <= value <= 604800:
            raise MachineError("SOLUTION_PUBLIC_EVIDENCE_INTEGER")
    for name, value in raw.items():
        if name in {"schema", "binding", "state", "checkpoint_sha256"}:
            continue
        if name == "failure_type":
            if value not in {"MachineError", "TimeoutExpired", "OSError", "RuntimeError", "ValueError"}:
                raise MachineError("SOLUTION_PUBLIC_EVIDENCE_FAILURE_TYPE")
        elif name in {"native_buckets_ns", "round_trip_buckets_ns"}:
            if (
                not isinstance(value, dict)
                or len(value) > 128
                or any(
                    re.fullmatch("[0-9]{1,30}", key) is None or type(count) is not int or count < 0
                    for key, count in value.items()
                )
            ):
                raise MachineError("SOLUTION_PUBLIC_EVIDENCE_HISTOGRAM")
        elif type(value) is not int or not 0 <= value < 2**128:
            raise MachineError("SOLUTION_PUBLIC_EVIDENCE_INTEGER")
    # Unknown fields are rejected rather than accidentally publishing a secret.
    # These schemas describe fixed synthetic graphs and contain no wallet salts.
    staging = target.parent / (".publish-" + secrets.token_hex(8))
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write(json.dumps(raw, sort_keys=True).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, target)
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        staging.unlink(missing_ok=True)
    return {"public_metadata_only": True, "source_permissions_unchanged": True, "public_mode": "0644"}


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote evidence publication only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.source, args.target)))


if __name__ == "__main__":
    main()
