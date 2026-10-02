"""Read-only public x402 evidence audit. No wallet, credentials or send calls.

Checks the fixed 0.01 test-USDC transfer, consumed nonce, canonical finalized
receipt, both balances, and delivered public-block hash on two independent RPCs.
Off-chain HTTP delivery remains recorded evidence, not a cryptographic guarantee.
"""

import argparse
import copy
import json
import re
from pathlib import Path

from eth_utils import keccak
from web3 import Web3

from economic_machine.values import digest

RPCS = ["https://sepolia-rollup.arbitrum.io/rpc", "https://arbitrum-sepolia.drpc.org"]
TOKEN = "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"
AMOUNT = 10_000
BALANCE_ABI = [{"type": "function", "name": name, "stateMutability": "view", "inputs": inputs,
                "outputs": [{"type": output}]} for name, inputs, output in [
    ("balanceOf", [{"name": "owner", "type": "address"}], "uint256"),
    ("authorizationState", [{"name": "payer", "type": "address"}, {"name": "nonce", "type": "bytes32"}], "bool")]]


def check_record(proof):
    if (proof["verified"] is not True or proof["mainnet"] is not False or proof["chain_id"] != 421614
            or proof["token"].lower() != TOKEN or proof["test_usdc"] != "0.01"
            or not re.fullmatch(r"0x[0-9a-f]{64}", proof["tx_hash"])
            or not re.fullmatch(r"0x[0-9a-f]{64}", proof["authorization_nonce"])):
        raise ValueError("record does not describe the approved Arbitrum Sepolia test payment")
    artifact = proof["artifact"]
    if (digest(artifact) != proof["artifact_hash"] or artifact["terms_hash"] != proof["terms_hash"]
            or artifact["data"]["chain_id"] != 421614
            or artifact["data_version"] != "block-" + str(artifact["data"]["block_number"])):
        raise ValueError("delivery hash, version or terms changed")
    for role, expected in [("buyer", -AMOUNT), ("seller", AMOUNT)]:
        before, after = proof["before"][role], proof["after_at_receipt_block"][role]
        if (before["address"].lower() != after["address"].lower()
                or after["usdc_atoms"] - before["usdc_atoms"] != expected
                or proof["balance_deltas"][role]["usdc_atoms"] != expected):
            raise ValueError("recorded payer or recipient balance delta changed")
    if proof["before"]["buyer"]["address"].lower() == proof["before"]["seller"]["address"].lower():
        raise ValueError("buyer and seller must be distinct")
    if (proof["mandate"]["reserved"] != 0 or proof["mandate"]["spent"] != AMOUNT
            or proof["seller_broadcasts"] != 1 or proof["replayed_submit_did_not_pay_again"] is not True):
        raise ValueError("recorded capital or at-most-once evidence changed")


def audit_rpc(proof, url, *, historical_balances=True):
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
    if w3.eth.chain_id != 421614:
        raise ValueError("RPC returned the wrong chain")
    receipt = w3.eth.get_transaction_receipt(proof["tx_hash"])
    final = w3.eth.get_block("finalized")
    block = w3.eth.get_block(receipt.blockNumber)
    if (receipt.status != 1 or receipt.blockNumber != proof["receipt_block"] or receipt.blockNumber > final.number
            or block.hash != receipt.blockHash or "0x" + receipt.blockHash.hex() != proof["receipt_block_hash"]):
        raise ValueError("RPC does not confirm a successful canonical finalized receipt")
    payer = Web3.to_checksum_address(proof["before"]["buyer"]["address"])
    recipient = Web3.to_checksum_address(proof["before"]["seller"]["address"])
    nonce = proof["authorization_nonce"]
    def topic(addr):
        return "0x" + addr[2:].lower().rjust(64, "0")
    transfers, used = [], []
    for log in receipt.logs:
        if log.address.lower() != TOKEN or log.get("removed", False):
            continue
        topics = ["0x" + bytes(t).hex() for t in log.topics]
        if topics == ["0x" + keccak(text="Transfer(address,address,uint256)").hex(), topic(payer), topic(recipient)]:
            transfers.append(int.from_bytes(log.data, "big"))
        if topics == ["0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex(), topic(payer), nonce]:
            used.append(log)
    if transfers != [AMOUNT] or len(used) != 1:
        raise ValueError("exact transfer and EIP-3009 authorization logs missing")
    token = w3.eth.contract(address=Web3.to_checksum_address(TOKEN), abi=BALANCE_ABI)
    if not token.functions.authorizationState(payer, bytes.fromhex(nonce[2:])).call(block_identifier=final.number):
        raise ValueError("nonce is not consumed at the finalized block")
    if historical_balances:
        for role in ["buyer", "seller"]:
            before, after = proof["before"][role], proof["after_at_receipt_block"][role]
            owner = Web3.to_checksum_address(before["address"])
            for recorded, height in [(before, proof["before_block"]), (after, receipt.blockNumber)]:
                if (token.functions.balanceOf(owner).call(block_identifier=height) != recorded["usdc_atoms"]
                        or w3.eth.get_balance(owner, block_identifier=height) != recorded["eth_wei"]):
                    raise ValueError("historical balances disagree with the recorded outcome")
    data = proof["artifact"]["data"]
    source = w3.eth.get_block(data["block_number"])
    if ("0x" + source.hash.hex() != data["block_hash"] or source.timestamp != data["timestamp"]
            or source.gasUsed != data["gas_used"] or len(source.transactions) != data["transactions"]):
        raise ValueError("delivered snapshot disagrees with public block state")
    return {"rpc": url, "chain_id": 421614, "receipt_status": receipt.status,
            "finalized_block": final.number, "exact_transfer_logs": 1, "authorization_used_logs": 1,
            "historical_balances_match": True if historical_balances else None,
            "historical_balances_checked": historical_balances,
            "nonce_consumed_at_finalized": True, "public_snapshot_matches": True}


def negative_checks(proof):
    cases = []
    for name, field, changed, expected in [
        ("amount", "test_usdc", "0.02", "approved Arbitrum Sepolia"),
        ("token", "token", "0x" + "11" * 20, "approved Arbitrum Sepolia")]:
        bad = copy.deepcopy(proof)
        bad[field] = changed
        cases.append((name, bad, False, expected))
    bad = copy.deepcopy(proof)
    bad["artifact"]["data"]["gas_used"] += 1
    cases.append(("delivery_hash", bad, False, "delivery hash"))
    bad = copy.deepcopy(proof)
    bad["mandate"]["reserved"] = 1
    cases.append(("capital_hold", bad, False, "capital"))
    bad = copy.deepcopy(proof)
    bad["artifact"]["data"]["gas_used"] += 1
    bad["artifact_hash"] = digest(bad["artifact"])
    cases.append(("rehashed_false_block_data", bad, True, "public block state"))
    bad = copy.deepcopy(proof)
    for section in ["before", "after_at_receipt_block"]:
        bad[section]["seller"]["address"] = "0x" + "11" * 20
    cases.append(("substituted_recipient", bad, True, "transfer and EIP-3009"))
    result = []
    for name, bad, live_read, expected in cases:
        try:
            check_record(bad)
            if live_read:
                audit_rpc(bad, RPCS[0])
        except ValueError as exc:
            if expected not in str(exc):
                raise RuntimeError("negative check failed for an unrelated reason: " + name) from exc
            result.append({"case": name, "rejected": True, "live_rpc_read": live_read})
        else:
            raise RuntimeError("corrupted evidence accepted: " + name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proof", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--negative-checks", action="store_true")
    args = parser.parse_args()
    proof = json.loads(args.proof.read_text())
    check_record(proof)
    observations = [audit_rpc(proof, url) for url in RPCS]
    result = {"verified": True, "read_only": True, "private_key_required": False,
              "tx_hash": proof["tx_hash"], "observations": observations,
              "assurance": "TWO_RPC_FINALIZED_TAGS_NOT_INDEPENDENT_L1_PROOF",
              "offchain_delivery_assurance": "RECORDED_HTTP_ARTIFACT_HASH_AND_PUBLIC_BLOCK_READBACK"}
    if args.negative_checks:
        result["negative_checks"] = negative_checks(proof)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
