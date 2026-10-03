"""Actual Sepolia coordinator/subscription reads + unsigned creation data only."""

import argparse
import hashlib
import json
from pathlib import Path

from economic_machine.values import MachineError
from machine_engine.solution_chain import ReadRPC
from machine_engine.solution_launch import LaunchReview, deployment_data

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote read-only review required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--subscription", type=int)
    parser.add_argument("--minimum-link-juels", type=int, default=10**18)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    providers = [
        ReadRPC("https://sepolia-rollup.arbitrum.io/rpc"),
        ReadRPC("https://arbitrum-sepolia-rpc.publicnode.com"),
    ]
    report = LaunchReview(*providers).inspect(args.owner, args.subscription, args.minimum_link_juels)
    if args.subscription:
        proof = json.loads((ROOT / "artifacts/solution/contract-build.json").read_bytes())
        if any(
            hashlib.sha256((ROOT / "contracts" / name).read_bytes()).hexdigest() != sha
            for name, sha in proof["sources"].items()
        ):
            raise MachineError("SOLUTION_BUILD_SOURCE_MISMATCH")
        artifact = json.loads((ROOT / "artifacts/solution/contracts.json").read_bytes())["SolutionMining"]
        report["unsigned_creation"] = {
            "from": report["owner"],
            "chainId": 421614,
            "value": "0x0",
            "data": deployment_data(artifact["bytecode"], args.subscription),
            "nonce": "UNASSIGNED_OWNER_RECHECK",
            "gas": "UNASSIGNED_OWNER_ESTIMATE",
            "status": "NOT_SIGNABLE_REVIEW_TEMPLATE",
        }
        # Include added creation data in the fingerprint, rather than stale preflight hash.
        from economic_machine.values import digest

        report.pop("review_sha256")
        report["review_sha256"] = digest(report)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise MachineError("SOLUTION_NEW_REVIEW_FILE_REQUIRED")
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "unsigned_creation"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - do not expose private provider/configuration errors.
        raise SystemExit("Launch preflight failed: " + type(error).__name__) from None
