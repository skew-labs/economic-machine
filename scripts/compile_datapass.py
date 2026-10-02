"""Compile the new contract only; existing escrow compilation is not repeated."""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Compile on the authorized remote host")
    compiler = ROOT / "tools/solc-0.8.24"
    prior = json.loads((ROOT / "artifacts/contract-build.json").read_text())
    actual = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if actual != prior["compiler_sha256"]:
        raise RuntimeError("previously verified official compiler changed")
    names = ["SkewDataPass.sol", "TestCommerceToken.sol", "DataPassAdversaries.sol"]
    sources = {name: {"content": (ROOT / "contracts" / name).read_text()} for name in names}
    request = {"language": "Solidity", "sources": sources,
        "settings": {"optimizer": {"enabled": True, "runs": 200}, "viaIR": True, "evmVersion": "shanghai",
                     "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}}}}
    result = subprocess.run([str(compiler), "--standard-json"], input=json.dumps(request),
        text=True, capture_output=True, check=True, timeout=90)
    compiled = json.loads(result.stdout)
    errors = [e for e in compiled.get("errors", []) if e["severity"] == "error"]
    if errors:
        raise RuntimeError(json.dumps(errors))
    output = ROOT / "artifacts/contracts.json"
    artifacts = json.loads(output.read_text())
    for contracts in compiled["contracts"].values():
        for name, contract in contracts.items():
            if contract["evm"]["bytecode"]["object"]:
                artifacts[name] = {"abi": contract["abi"], "bytecode": contract["evm"]["bytecode"]["object"],
                                   "runtime": contract["evm"]["deployedBytecode"]["object"]}
    output.write_text(json.dumps(artifacts, sort_keys=True))
    manifest = {"compiler_version": prior["compiler_version"], "compiler_sha256": actual,
                "settings": request["settings"], "sources": {name: hashlib.sha256(v["content"].encode()).hexdigest() for name, v in sources.items()},
                "contracts": {name: {"bytecode_bytes": len(artifacts[name]["bytecode"]) // 2,
                          "bytecode_sha256": hashlib.sha256(bytes.fromhex(artifacts[name]["bytecode"])).hexdigest(),
                          "runtime_sha256": hashlib.sha256(bytes.fromhex(artifacts[name]["runtime"])).hexdigest()} for name in ["SkewDataPass", "DataPassHostileToken", "DataPassHostileReceiver"]},
                "deployment": "NONE", "evidence_scope": "EVM_LOCAL_TEST_ON_REMOTE_HOST_NOT_PUBLIC_ARBITRUM"}
    folder = ROOT / "artifacts/atlas-release"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "datapass-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["contracts"]))


if __name__ == "__main__":
    main()
