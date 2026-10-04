"""Read-only real-coordinator launch review, never a wallet or broadcaster."""

import hashlib
import time

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from economic_machine.values import MachineError, digest

from .solution_chain import FinalizedChain, quantity

COORDINATOR = "0x5CE8D5A2BC84beb22a398CCA51996F7930313D61"
KEY_HASH = "1770bdc7eec7771f7ba4ffd640f34260d7f095b79c92d34a5b2551d6f6cfd2be"
DOCS = "https://docs.chain.link/vrf/v2-5/supported-networks"


class LaunchReview:
    def __init__(self, first, second):
        if first.identity == second.identity:
            raise MachineError("SOLUTION_INDEPENDENT_RPC_HOSTS_REQUIRED")
        self.providers = (first, second)

    def pair(self, method, params):
        rows = [rpc(method, params) for rpc in self.providers]
        if rows[0] != rows[1]:
            raise MachineError("SOLUTION_LAUNCH_RPC_DISAGREEMENT")
        return rows[0]

    def inspect(self, owner, subscription=None, minimum_link=10**18, *, now=None):
        owner = to_checksum_address(owner)
        if int(owner, 16) == 0:
            raise MachineError("SOLUTION_LAUNCH_OWNER")
        if type(minimum_link) is not int or not 1 <= minimum_link < 2**96:
            raise MachineError("SOLUTION_LAUNCH_LINK_FLOOR")
        if subscription is not None and (type(subscription) is not int or not 1 <= subscription < 2**256):
            raise MachineError("SOLUTION_LAUNCH_SUBSCRIPTION")
        if quantity(self.pair("eth_chainId", [])) != 421614:
            raise MachineError("SOLUTION_LAUNCH_TESTNET_ONLY")
        heads = [
            FinalizedChain.block(rpc("eth_getBlockByNumber", ["latest", False])) for rpc in self.providers
        ]
        number = min(row["number"] for row in heads) - 4
        if number < 0:
            raise MachineError("SOLUTION_LAUNCH_HEAD")
        tag = hex(number)
        blocks = [FinalizedChain.block(rpc("eth_getBlockByNumber", [tag, False])) for rpc in self.providers]
        now = int(time.time()) if now is None else now
        if blocks[0] != blocks[1] or not 0 <= now - blocks[0]["timestamp"] <= 120:
            raise MachineError("SOLUTION_LAUNCH_STALE_OR_REORG")
        code = self.pair("eth_getCode", [COORDINATOR, tag])
        if not isinstance(code, str) or not code.startswith("0x") or len(code) < 4:
            raise MachineError("SOLUTION_LAUNCH_COORDINATOR_CODE")
        try:
            code_hash = hashlib.sha256(bytes.fromhex(code[2:])).hexdigest()
        except ValueError:
            raise MachineError("SOLUTION_LAUNCH_COORDINATOR_CODE") from None
        gates = [
            "INDEPENDENT_SECURITY_REVIEW",
            "PUBLIC_REAL_VRF_LIFECYCLE",
            "LONG_DURATION_OPERATIONS",
            "OFF_HOST_RESTORE_AND_SEPARATE_KEY_CUSTODY",
            "OWNER_SIGNATURE_AND_GAS_APPROVAL",
        ]
        sub = None
        if subscription is None:
            gates += [
                "REAL_VRF_SUBSCRIPTION_REQUIRED",
                "LINK_FUNDING_REQUIRED",
                "CONSUMER_REGISTRATION_REQUIRED",
            ]
        else:
            data = keccak(text="getSubscription(uint256)")[:4] + encode(["uint256"], [subscription])
            response = self.pair("eth_call", [{"to": COORDINATOR, "data": "0x" + data.hex()}, tag])
            try:
                balance, native, count, sub_owner, consumers = decode(
                    ["uint96", "uint96", "uint64", "address", "address[]"], bytes.fromhex(response[2:])
                )
            except (ValueError, TypeError, DecodingError):
                raise MachineError("SOLUTION_LAUNCH_SUBSCRIPTION_RESPONSE") from None
            sub = {
                "id": str(subscription),
                "owner": to_checksum_address(sub_owner),
                "link_balance_juels": str(balance),
                "native_balance_wei": str(native),
                "request_count": count,
                "consumers": [to_checksum_address(p) for p in consumers],
                "minimum_link_juels": str(minimum_link),
                "fee_sufficiency": "NOT_PROVEN_PRICE_AND_L2_OVERHEAD_VARIABLE",
            }
            if sub["owner"] != owner:
                gates.append("SUBSCRIPTION_OWNER_MISMATCH")
            if balance < minimum_link:
                gates.append("LINK_BALANCE_BELOW_REVIEW_FLOOR")
            gates.append("NEW_CONSUMER_REGISTRATION_REQUIRED_AFTER_DEPLOYMENT")
        after = [FinalizedChain.block(rpc("eth_getBlockByNumber", [tag, False])) for rpc in self.providers]
        if any(block != blocks[0] for block in after):
            raise MachineError("SOLUTION_LAUNCH_ANCHOR_CHANGED")
        result = {
            "schema": "solution-launch-review-1",
            "chain_id": 421614,
            "owner": owner,
            "coordinator": COORDINATOR,
            "coordinator_observed_runtime_sha256": code_hash,
            "official_network_reference": DOCS,
            "key_hash": "0x" + KEY_HASH,
            "anchor": blocks[0],
            "state_assurance": "DUAL_RPC_RECENT_CANONICAL_NOT_CRYPTOGRAPHIC_STATE_PROOF",
            "subscription": sub,
            "remaining_gates": gates,
            "status": "BLOCKED_NO_PUBLIC_ISSUANCE",
            "automatic_signing": False,
            "transactions_submitted": 0,
            "tokens_minted": 0,
        }
        result["review_sha256"] = digest(result)
        return result


def deployment_data(bytecode, subscription):
    if type(subscription) is not int or not 1 <= subscription < 2**256:
        raise MachineError("SOLUTION_LAUNCH_SUBSCRIPTION")
    if not isinstance(bytecode, str) or not 1 <= len(bytecode) <= 49152 * 2:
        raise MachineError("SOLUTION_LAUNCH_CREATION_CODE")
    try:
        data = bytes.fromhex(bytecode)
    except ValueError:
        raise MachineError("SOLUTION_LAUNCH_CREATION_CODE") from None
    args = encode(["address", "bytes32", "uint256"], [COORDINATOR, bytes.fromhex(KEY_HASH), subscription])
    return "0x" + (data + args).hex()
