"""Compile DataPass plus token-emitting useful work on the authorized builder."""
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
    names = ["SkewLaunchBundle.sol", "SkewArtifactMining.sol", "SkewSolutionMining.sol", "SkewDataPass.sol", "TestCommerceToken.sol"]
    sources = {name: {"content": (ROOT / "contracts" / name).read_text()} for name in names}
    settings = {"optimizer": {"enabled": True, "runs": 200}, "viaIR": True, "evmVersion": "shanghai",
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object",
                                                "evm.deployedBytecode.immutableReferences"]}}}
    run = subprocess.run([str(compiler), "--standard-json"],
        input=json.dumps({"language": "Solidity", "sources": sources, "settings": settings}),
        text=True, capture_output=True, check=True, timeout=90)
    compiled = json.loads(run.stdout)
    if compiled.get("errors"):
        raise RuntimeError(json.dumps(compiled["errors"]))
    output = {}
    for group in compiled["contracts"].values():
        for name, item in group.items():
            if name not in {"SkewLaunchBundle", "SkewArtifactMining", "SkewSolutionToken", "SkewDataPass", "TestCommerceToken"}:
                continue
            evm = item["evm"]
            output[name] = {"abi": item["abi"], "bytecode": evm["bytecode"]["object"],
                            "runtime": evm["deployedBytecode"]["object"],
                            "immutable_references": evm["deployedBytecode"]["immutableReferences"]}
    if any(len(v["runtime"]) // 2 > 24576 or len(v["bytecode"]) // 2 > 49152 for v in output.values()):
        raise RuntimeError("Contract size limit")
    folder = ROOT / "artifacts/datapass-mining"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "contracts.json"
    target.write_text(json.dumps(output, sort_keys=True) + "\n")
    manifest = {"compiler_sha256": sha, "sources": {n: hashlib.sha256(v["content"].encode()).hexdigest() for n, v in sources.items()},
                "settings": settings, "runtime_bytes": {n: len(v["runtime"]) // 2 for n, v in output.items()},
                "artifact_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "warnings": 0,
                "public_deployment": False}
    (folder / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
