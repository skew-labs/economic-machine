"""Fetch a hash-verified official Solidity compiler and compile on the remote host."""

import argparse
import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://raw.githubusercontent.com/ethereum/solc-bin/gh-pages/linux-amd64/"


def main():
    (ROOT / "artifacts").mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["TestEIP3009Token"])
    args = parser.parse_args()
    with urllib.request.urlopen(BASE + "list.json", timeout=30) as response:
        listing = json.load(response)
    filename = listing["releases"]["0.8.24"]
    metadata = next(build for build in listing["builds"] if build["path"] == filename)
    compiler = ROOT / "tools/solc-0.8.24"
    compiler.parent.mkdir(exist_ok=True)
    if not compiler.exists():
        with urllib.request.urlopen(BASE + filename, timeout=60) as response:
            compiler.write_bytes(response.read())
    expected = metadata["sha256"].removeprefix("0x")
    actual = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError("official solc binary digest mismatch")
    os.chmod(compiler, 0o755)
    sources = {path.name: {"content": path.read_text()} for path in (ROOT / "contracts").glob("*.sol")
               if not args.only or path.stem == args.only}
    request = {"language": "Solidity", "sources": sources,
        "settings": {"optimizer": {"enabled": True, "runs": 200}, "evmVersion": "shanghai",
                     "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}}}}
    result = subprocess.run([str(compiler), "--standard-json"], input=json.dumps(request),
                            text=True, capture_output=True, check=True, timeout=60)
    compiled = json.loads(result.stdout)
    errors = [error for error in compiled.get("errors", []) if error["severity"] == "error"]
    if errors:
        raise RuntimeError(json.dumps(errors))
    artifacts = json.loads((ROOT / "artifacts/contracts.json").read_text()) if args.only else {}
    for contracts in compiled["contracts"].values():
        for name, contract in contracts.items():
            if contract["evm"]["bytecode"]["object"]:
                artifacts[name] = {"abi": contract["abi"], "bytecode": contract["evm"]["bytecode"]["object"]}
    (ROOT / "artifacts/contracts.json").write_text(json.dumps(artifacts, sort_keys=True))
    manifest = {"compiler_version": metadata["longVersion"], "compiler_sha256": actual,
        "sources": {name: hashlib.sha256(content["content"].encode()).hexdigest()
                    for name, content in sources.items()},
        "contracts": {name: {"bytecode_sha256": hashlib.sha256(bytes.fromhex(a["bytecode"])).hexdigest(),
                            "bytecode_bytes": len(a["bytecode"]) // 2} for name, a in artifacts.items()},
        "deployment": "NONE", "chain": "PY_EVM_TEST_ONLY"}
    output = "eip3009-build.json" if args.only else "contract-build.json"
    (ROOT / "artifacts" / output).write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
