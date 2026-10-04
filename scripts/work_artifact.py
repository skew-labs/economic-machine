"""Construct a replay-verified immutable work product; never publish, sign or mint."""
import argparse
import json
import os
from pathlib import Path

from economic_machine.values import canonical, digest
from machine_commerce.work_artifacts import build_artifact, safe_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--publisher-grant", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw, candidate, grant = safe_json(args.input), safe_json(args.candidate), safe_json(args.publisher_grant)
    report = build_artifact(raw, candidate["path"], grant)
    # Exclusive write: a previously published content version is never replaced.
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(canonical(report)); file.flush(); os.fsync(file.fileno())
    print(json.dumps({"status": "REPLAY_VERIFIED_NOT_PUBLISHED", "content_root": report["report_sha256"],
                      "terms_hash": report["terms_sha256"], "rights_root": digest(grant),
                      "reward": "SKEW_TOKEN_AFTER_ONCHAIN_WINNER_AND_MATCHING_PUBLICATION",
                      "rights": "PUBLISHER_ATTESTATION_NOT_LEGAL_PROOF", "broadcasts": 0}))


if __name__ == "__main__":
    main()
