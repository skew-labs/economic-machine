"""Read-only Arbitrum launch quotation and independently reconciled deployment.

The owner signs with MetaMask. This program never loads a key or broadcasts.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx
from eth_abi import decode, encode

from economic_machine.values import MachineError, canonical
from machine_commerce.datapass import NETWORKS, address, call_data
from machine_commerce.fuel_rpc import source_label

ROOT = Path(__file__).resolve().parents[1]
READS = {"eth_chainId", "eth_getBalance", "eth_getTransactionCount", "eth_getCode", "eth_call",
         "eth_gasPrice", "eth_estimateGas", "eth_getTransactionReceipt", "eth_getTransactionByHash",
         "eth_getBlockByNumber"}


def _rpc(url, method, params):
    if method not in READS or url not in NETWORKS[42161]["rpcs"]:
        raise MachineError("READ_ONLY_MAINNET_RPC_REQUIRED")
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not 200 <= response.status_code < 300:
            raise MachineError("RPC_READ_FAILED")
        if len(response.content) > 512000:
            raise MachineError("RPC_RESPONSE_LIMIT")
        result = response.json()
    if "error" in result or result.get("id") != 1 or "result" not in result:
        raise MachineError("RPC_READ_FAILED")
    return result["result"]


def rpc(url, method, params):
    try:
        return _rpc(url, method, params)
    except (httpx.HTTPError, ValueError):
        raise MachineError("RPC_READ_FAILED") from None


def compiled():
    folder = ROOT / "artifacts/datapass-mining"
    manifest = json.loads((folder / "build.json").read_text())
    payload = (folder / "contracts.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest["artifact_sha256"]:
        raise MachineError("COMPILER_ARTIFACT_CHANGED")
    for name, sha in manifest["sources"].items():
        if hashlib.sha256((ROOT / "contracts" / name).read_bytes()).hexdigest() != sha:
            raise MachineError("SOURCE_CHANGED_RECOMPILE_REQUIRED")
    return json.loads(payload), manifest


def prepare(owner, maximum_wei):
    owner = address(owner)
    if type(maximum_wei) is not int or not 0 < maximum_wei <= 500_000_000_000_000:
        raise MachineError("DEPLOYMENT_GAS_CAP_TOO_HIGH")
    contracts, manifest = compiled()
    data = "0x" + contracts["SkewLaunchBundle"]["bytecode"] + encode(["address", "address"], [owner, NETWORKS[42161]["asset"]]).hex()
    observations = []
    for index, url in enumerate(NETWORKS[42161]["rpcs"]):
        if int(rpc(url, "eth_chainId", []), 16) != 42161:
            raise MachineError("WRONG_CHAIN")
        latest = int(rpc(url, "eth_getTransactionCount", [owner, "latest"]), 16)
        pending = int(rpc(url, "eth_getTransactionCount", [owner, "pending"]), 16)
        if latest != pending:
            raise MachineError("OWNER_PENDING_TRANSACTION")
        balance = int(rpc(url, "eth_getBalance", [owner, "latest"]), 16)
        usdc = int(rpc(url, "eth_call", [{"to": NETWORKS[42161]["asset"],
            "data": call_data("balanceOf(address)", ["address"], [owner])}, "latest"]), 16)
        price = int(rpc(url, "eth_gasPrice", []), 16)
        tx = {"from": owner, "data": data, "value": "0x0"}
        # With zero ETH the quote may require a balance-only simulation override.
        # It never changes the actual wallet balance or marks it funded.
        try:
            gas = int(rpc(url, "eth_estimateGas", [tx]), 16)
            override = False
        except MachineError:
            gas = int(rpc(url, "eth_estimateGas", [tx, "latest", {owner: {"balance": hex(10**18)}}]), 16)
            override = True
        observations.append({"rpc": source_label(index), "nonce": latest, "eth_wei": str(balance), "usdc_atoms": str(usdc),
                             "gas_estimate": gas, "gas_price_wei": price, "balance_override_for_quote_only": override})
    if len({row["nonce"] for row in observations}) != 1:
        raise MachineError("NONCE_DISAGREEMENT")
    gas = max(row["gas_estimate"] for row in observations) * 125 // 100
    price = max(row["gas_price_wei"] for row in observations) * 2
    maximum = gas * price
    if maximum > maximum_wei:
        raise MachineError("DEPLOYMENT_QUOTE_EXCEEDS_OWNER_CAP")
    funded = min(int(row["eth_wei"]) for row in observations) >= maximum
    now = int(time.time())
    return {"schema": "skew-launch-review-1", "chain_id": 42161, "owner": owner,
            "transaction": {"from": owner, "chainId": "0xa4b1", "value": "0x0", "data": data,
                            "nonce": hex(observations[0]["nonce"]), "gas": hex(gas), "gasPrice": hex(price)},
            "created_at": now, "expires_at": now + 300, "observations": observations,
            "maximum_gas_wei": str(maximum), "owner_cap_wei": str(maximum_wei),
            "status": "READY_FOR_OWNER_SIGNATURE" if funded else "NEEDS_NATIVE_ETH",
            "initcode_sha256": hashlib.sha256(bytes.fromhex(data[2:])).hexdigest(),
            "build_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
            "token": {"name": "Skew Solution", "symbol": "SKEW", "initial_supply": "0",
                      "cap_tokens": "160000", "tokens_per_accepted_job": "1", "premine": False},
            "broadcasts": 0, "mainnet_deployed": False}


def reconcile(review, tx_hash):
    from machine_commerce.datapass import bytes32
    bytes32(tx_hash)
    contracts, _ = compiled()
    expected = review["transaction"]
    if hashlib.sha256(bytes.fromhex(expected["data"][2:])).hexdigest() != review["initcode_sha256"]:
        raise MachineError("REVIEW_CHANGED")
    # A pruned RPC may retain the deployment receipt but not its old state trie.
    # Verify runtime/configuration at one shared finalized block, not two moving
    # `latest` states. Deployment inclusion and source-bound calldata stay checked.
    final_blocks = [rpc(url, "eth_getBlockByNumber", ["finalized", False])
                    for url in NETWORKS[42161]["rpcs"]]
    state_at = hex(min(int(block["number"], 16) for block in final_blocks))
    observations = []
    for index, url in enumerate(NETWORKS[42161]["rpcs"]):
        if int(rpc(url, "eth_chainId", []), 16) != 42161:
            raise MachineError("WRONG_CHAIN")
        receipt = rpc(url, "eth_getTransactionReceipt", [tx_hash])
        transaction = rpc(url, "eth_getTransactionByHash", [tx_hash])
        if not receipt or not transaction:
            return {"status": "UNKNOWN_RECONCILE_ONLY", "tx_hash": tx_hash, "safe_to_retry": False}
        if int(receipt["status"], 16) != 1:
            raise MachineError("DEPLOYMENT_REVERTED")
        if (transaction["from"].lower() != review["owner"].lower() or transaction.get("to") is not None
                or transaction["input"].lower() != expected["data"].lower() or int(transaction["value"], 16) != 0
                or int(transaction["nonce"], 16) != int(expected["nonce"], 16)):
            raise MachineError("DEPLOYMENT_TRANSACTION_MISMATCH")
        final = rpc(url, "eth_getBlockByNumber", ["finalized", False])
        block = rpc(url, "eth_getBlockByNumber", [receipt["blockNumber"], False])
        if block["hash"].lower() != receipt["blockHash"].lower():
            raise MachineError("DEPLOYMENT_REORG")
        if min(int(final["number"], 16), int(state_at, 16)) < int(receipt["blockNumber"], 16):
            return {"status": "INCLUDED_AWAITING_FINALITY", "tx_hash": tx_hash, "safe_to_retry": False}
        bundle = address(receipt["contractAddress"])
        at = state_at
        state_block = rpc(url, "eth_getBlockByNumber", [at, False])
        if int(state_block["number"], 16) != int(at, 16):
            raise MachineError("STATE_BLOCK_MISMATCH")
        def call(target, signature, types, url=url, at=at):
            raw = rpc(url, "eth_call", [{"to": target, "data": call_data(signature, [], [])}, at])
            return decode(types, bytes.fromhex(raw[2:]))[0]
        addresses = {"SkewLaunchBundle": bundle, "SkewDataPass": address(call(bundle, "dataPass()", ["address"])),
                     "SkewArtifactMining": address(call(bundle, "mining()", ["address"])),
                     "SkewSolutionToken": address(call(bundle, "token()", ["address"]))}
        runtime = {}
        for name, target in addresses.items():
            code = bytes.fromhex(rpc(url, "eth_getCode", [target, at])[2:])
            template = bytes.fromhex(contracts[name]["runtime"])
            a, b = bytearray(code), bytearray(template)
            for slots in contracts[name]["immutable_references"].values():
                for slot in slots:
                    start, length = slot["start"], slot["length"]
                    a[start:start+length] = b"\0" * length; b[start:start+length] = b"\0" * length
            if not code or len(code) != len(template) or a != b:
                raise MachineError("DEPLOYED_RUNTIME_MISMATCH")
            runtime[name] = hashlib.sha256(code).hexdigest()
        mining, token, passport = addresses["SkewArtifactMining"], addresses["SkewSolutionToken"], addresses["SkewDataPass"]
        if (address(call(passport, "owner()", ["address"])) != address(review["owner"])
                or address(call(mining, "publisher()", ["address"])) != address(review["owner"])
                or address(call(mining, "dataPass()", ["address"])) != passport
                or address(call(mining, "rewardToken()", ["address"])) != token
                or address(call(token, "mining()", ["address"])) != mining
                or call(token, "totalSupply()", ["uint256"]) != 0
                or call(token, "cap()", ["uint256"]) != 160000 * 10**18):
            raise MachineError("DEPLOYED_CONFIGURATION_MISMATCH")
        actual = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
        if actual > int(review["owner_cap_wei"]):
            raise MachineError("ACTUAL_FEE_EXCEEDS_CAP")
        if rpc(url, "eth_getBlockByNumber", [at, False])["hash"] != state_block["hash"]:
            raise MachineError("FINALIZED_STATE_CHANGED")
        observations.append({"rpc": source_label(index), "block_hash": block["hash"], "addresses": addresses,
                             "runtime_sha256": runtime, "actual_gas_wei": str(actual),
                             "state_block_number": int(at, 16), "state_block_hash": state_block["hash"]})
    if any(observations[0][k] != observations[1][k] for k in ["block_hash", "addresses", "runtime_sha256", "actual_gas_wei", "state_block_number", "state_block_hash"]):
        raise MachineError("RPC_DEPLOYMENT_DISAGREEMENT")
    return {"status": "MAINNET_DEPLOYED_FINALIZED", "tx_hash": tx_hash, "observations": observations,
            "tokens_minted": "0", "purchase_completed": False, "broadcasts_by_script": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner")
    parser.add_argument("--maximum-wei", type=int, default=500000000000000)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--tx-hash")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Run on the authorized remote host")
    result = reconcile(json.loads(args.review.read_text()), args.tx_hash) if args.review else prepare(args.owner, args.maximum_wei)
    with args.output.open("x") as file:
        json.dump(result, file, indent=2); file.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "transaction"}))


if __name__ == "__main__":
    main()
