"""Read-only network inspection and constructor review; never deploys or signs."""

import argparse
import hashlib
import json
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import is_address, keccak, to_checksum_address

from economic_machine.values import MachineError
from machine_engine.solution_chain import FinalizedChain, ReadRPC, quantity
from machine_engine.solution_networks import NETWORKS, SECURITY, SOURCE

ROOT = Path(__file__).resolve().parents[1]


def inspect_network(first, second, chain_id=42161, subscription=None):
    if type(chain_id) is not int or chain_id not in NETWORKS:
        raise MachineError("SOLUTION_CHAIN_CONFIG")
    config = NETWORKS[chain_id]
    if first.identity == second.identity:
        raise MachineError("SOLUTION_INDEPENDENT_RPC_HOSTS_REQUIRED")
    if quantity(first("eth_chainId", [])) != chain_id or quantity(second("eth_chainId", [])) != chain_id:
        raise MachineError("SOLUTION_WRONG_CHAIN")
    a = FinalizedChain.block(first("eth_getBlockByNumber", ["finalized", False]))
    b = FinalizedChain.block(second("eth_getBlockByNumber", ["finalized", False]))
    height = min(a["number"], b["number"])
    anchor = FinalizedChain.block(first("eth_getBlockByNumber", [hex(height), False]))
    if FinalizedChain.block(second("eth_getBlockByNumber", [hex(height), False])) != anchor:
        raise MachineError("SOLUTION_RPC_DISAGREEMENT")
    codes = {}
    for label in ["coordinator", "link"]:
        address = config[label]
        code = first("eth_getCode", [address, hex(height)])
        if code != second("eth_getCode", [address, hex(height)]) or code == "0x":
            raise MachineError("SOLUTION_NETWORK_CODE_DISAGREEMENT")
        codes[label] = {
            "address": address,
            "code_sha256": hashlib.sha256(bytes.fromhex(code[2:])).hexdigest(),
            "size": len(code[2:]) // 2,
        }
    detail = None
    if subscription is not None:
        if type(subscription) is not int or not 0 < subscription < 2**256:
            raise MachineError("SOLUTION_SUBSCRIPTION_BOUND")
        data = (
            "0x" + (keccak(text="getSubscription(uint256)")[:4] + encode(["uint256"], [subscription])).hex()
        )
        query = [{"to": config["coordinator"], "data": data}, hex(height)]
        result = first("eth_call", query)
        if result != second("eth_call", query):
            raise MachineError("SOLUTION_RPC_DISAGREEMENT")
        balance, native, requests, owner, consumers = decode(
            ["uint96", "uint96", "uint64", "address", "address[]"], bytes.fromhex(result[2:])
        )
        detail = {
            "id": str(subscription),
            "link_balance_juels": str(balance),
            "native_balance_wei": str(native),
            "requests": requests,
            "owner": to_checksum_address(owner),
            "consumers": [to_checksum_address(c) for c in consumers],
        }
    for provider in [first, second]:
        if FinalizedChain.block(provider("eth_getBlockByNumber", [hex(height), False])) != anchor:
            raise MachineError("SOLUTION_REORG_DURING_READ")
    return {
        "schema": "solution-network-read-1",
        "chain_id": chain_id,
        "network": config["name"],
        "anchor": anchor,
        "providers": [first.identity, second.identity],
        "contracts": codes,
        "subscription": detail,
        "configuration_source": SOURCE,
        "security_source": SECURITY,
        "own_contract_deployed": False,
        "signatures": 0,
        "submitted_transactions": 0,
        "assurance": "TWO_RPC_HOSTS_AGREE_ON_FINALIZED_STATE_NOT_INDEPENDENT_OPERATOR_PROOF",
    }


def deployment_review(owner, subscription, reserve, *, chain_id=42161):
    if type(chain_id) is not int or chain_id not in NETWORKS:
        raise MachineError("SOLUTION_CHAIN_CONFIG")
    if (
        not is_address(owner)
        or int(owner, 16) == 0
        or type(subscription) is not int
        or not 0 < subscription < 2**256
    ):
        raise MachineError("SOLUTION_DEPLOYMENT_PUBLIC_OWNER_SUBSCRIPTION_REQUIRED")
    if type(reserve) is not int or not 0 < reserve < 2**96:
        raise MachineError("SOLUTION_DEPLOYMENT_RESERVE_BOUND")
    config = NETWORKS[chain_id]
    folder = ROOT / "artifacts/solution-mainnet"
    build = json.loads((folder / "contract-build.json").read_text())
    raw = (folder / "contracts.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != build["artifact_sha256"]:
        raise MachineError("SOLUTION_DEPLOYMENT_ARTIFACT_HASH")
    for name, expected in build["sources"].items():
        if hashlib.sha256((ROOT / "contracts" / name).read_bytes()).hexdigest() != expected:
            raise MachineError("SOLUTION_DEPLOYMENT_SOURCE_HASH")
    compiled = json.loads(raw)["SkewSolutionMining"]
    args = [
        to_checksum_address(config["coordinator"]),
        bytes.fromhex(config["key_hash"][2:]),
        subscription,
        to_checksum_address(owner),
        reserve,
    ]
    initcode = (
        compiled["bytecode"] + encode(["address", "bytes32", "uint256", "address", "uint96"], args).hex()
    )
    return {
        "schema": "solution-deployment-review-1",
        "chain_id": chain_id,
        "constructor": {
            "coordinator": args[0],
            "key_hash": config["key_hash"],
            "subscription": str(subscription),
            "governor": args[3],
            "minimum_link_reserve_juels": str(reserve),
        },
        "unsigned_transaction": {
            "chainId": chain_id,
            "from": args[3],
            "value": "0x0",
            "data": "0x" + initcode,
        },
        "initcode_sha256": hashlib.sha256(bytes.fromhex(initcode)).hexdigest(),
        "build": build,
        "token": {
            "name": "Skew Solution",
            "symbol": "SKEW",
            "initial_supply": "0",
            "cap_tokens": 160000,
            "reward_tokens_per_problem": 1,
            "problems_per_round": 16,
            "maximum_rounds": 10000,
        },
        "initial_admission": "LOCKED_UNTIL_GOVERNOR_ACTIVATION_AND_VRF_CONSUMER_FUNDING",
        "separate_paid_steps": [
            "deployment gas",
            "VRF subscription registration/funding",
            "consumer registration",
            "activation",
            "keeper requests",
        ],
        "public_launch_ready": False,
        "independent_security_review": "PENDING",
        "requires_owner_approval_of_exact_review_and_fees": True,
        "signed": False,
        "sent": False,
    }


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote verification required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-a", default="https://arb1.arbitrum.io/rpc")
    parser.add_argument("--rpc-b", default="https://arbitrum-one.public.blastapi.io")
    parser.add_argument("--chain-id", type=int, choices=sorted(NETWORKS), default=42161)
    parser.add_argument("--subscription", type=int)
    parser.add_argument("--owner")
    parser.add_argument("--minimum-link-reserve-juels", type=int)
    args = parser.parse_args()
    folder = ROOT / "artifacts/solution-mainnet"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        result = inspect_network(ReadRPC(args.rpc_a), ReadRPC(args.rpc_b), args.chain_id, args.subscription)
    except MachineError as error:
        failure = {
            "state": "UNAVAILABLE",
            "reason": str(error),
            "chain_id": args.chain_id,
            "providers": [ReadRPC(args.rpc_a).identity, ReadRPC(args.rpc_b).identity],
            "submitted_transactions": 0,
        }
        (folder / "network-read-failure.json").write_text(json.dumps(failure, indent=2) + "\n")
        raise
    (folder / "network-read.json").write_text(json.dumps(result, indent=2) + "\n")
    if args.owner:
        review = deployment_review(
            args.owner, args.subscription, args.minimum_link_reserve_juels, chain_id=args.chain_id
        )
        (folder / "deployment-review.json").write_text(json.dumps(review, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
