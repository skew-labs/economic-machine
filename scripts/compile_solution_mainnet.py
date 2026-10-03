"""Reproducible mainnet-candidate build, without deployment or wallet authority."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote compiler required")
    compiler = ROOT / "tools/solc-0.8.24"
    sha = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if sha != "fb03a29a517452b9f12bcf459ef37d0a543765bb3bbc911e70a87d6a37c30d5f":
        raise RuntimeError("Compiler pin differs")
    names = ["SkewSolutionMining.sol", "SkewVRFMock.sol"]
    sources = {name: {"content": (ROOT / "contracts" / name).read_text()} for name in names}
    settings = {
        "optimizer": {"enabled": True, "runs": 200},
        "viaIR": True,
        "evmVersion": "shanghai",
        "outputSelection": {
            "*": {
                "*": [
                    "abi",
                    "evm.bytecode.object",
                    "evm.deployedBytecode.object",
                    "evm.deployedBytecode.immutableReferences",
                ]
            }
        },
    }
    run = subprocess.run(
        [str(compiler), "--standard-json"],
        input=json.dumps({"language": "Solidity", "sources": sources, "settings": settings}),
        text=True,
        capture_output=True,
        check=True,
        timeout=90,
    )
    result = json.loads(run.stdout)
    if result.get("errors"):
        raise RuntimeError(json.dumps(result["errors"]))
    contracts = {
        name: {
            "abi": item["abi"],
            "bytecode": item["evm"]["bytecode"]["object"],
            "runtime": item["evm"]["deployedBytecode"]["object"],
            "immutable_references": item["evm"]["deployedBytecode"]["immutableReferences"],
        }
        for group in result["contracts"].values()
        for name, item in group.items()
        if item["evm"]["bytecode"]["object"]
    }
    if any(
        len(item["runtime"]) // 2 > 24576 or len(item["bytecode"]) // 2 > 49152 for item in contracts.values()
    ):
        raise RuntimeError("EVM creation/runtime size limit")
    folder = ROOT / "artifacts/solution-mainnet"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "contracts.json").write_text(json.dumps(contracts, sort_keys=True) + "\n")
    proof = {
        "compiler_sha256": sha,
        "sources": {
            name: hashlib.sha256(item["content"].encode()).hexdigest() for name, item in sources.items()
        },
        "runtime_sizes": {name: len(item["runtime"]) // 2 for name, item in contracts.items()},
        "warnings": 0,
        "actual_deployments": 0,
        "audit": "INDEPENDENT_REVIEW_REQUIRED",
        "test_randomness": "MOCK_ONLY",
        "artifact_sha256": hashlib.sha256((folder / "contracts.json").read_bytes()).hexdigest(),
    }
    (folder / "contract-build.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
