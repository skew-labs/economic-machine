"""Exact unsigned administrative operations, separate from participant authority.

No key, wallet creation, send method, retry or discretionary destination exists.
A VRF subscription ID must come from an actual creation receipt/readback; it is
never predicted from a nonce or fabricated to make a deployment plan look ready.
"""

from eth_abi import encode
from eth_utils import is_address, keccak, to_checksum_address

from economic_machine.values import MachineError, digest

from .solution_networks import NETWORKS


def address(value):
    if not isinstance(value, str) or not is_address(value) or int(value, 16) == 0:
        raise MachineError("SOLUTION_SETUP_PUBLIC_ADDRESS_REQUIRED")
    return to_checksum_address(value)


def subscription_id(value):
    if type(value) is not int or not 0 < value < 2**256:
        raise MachineError("SOLUTION_SETUP_CONFIRMED_SUBSCRIPTION_REQUIRED")
    return value


def network(chain_id):
    if type(chain_id) is not int or chain_id not in NETWORKS:
        raise MachineError("SOLUTION_SETUP_NETWORK_REQUIRED")
    return NETWORKS[chain_id]


def intent(owner, destination, signature, types, values, *, chain_id=42161, effect):
    network(chain_id)
    data = keccak(text=signature)[:4] + encode(types, values)
    payload = {
        "schema": "solution-admin-intent-1",
        "action": signature.split("(")[0],
        "transaction": {
            "from": address(owner),
            "to": address(destination),
            "chainId": chain_id,
            "value": "0x0",
            "data": "0x" + data.hex(),
        },
        "expected_effect": effect,
        "signed": False,
        "sent": False,
        "requires_exact_owner_approval": True,
        "participant_signer_admits_this_action": False,
    }
    payload["intent_sha256"] = digest(payload)
    return payload


def create_subscription(owner, *, chain_id=42161):
    config = network(chain_id)
    return intent(
        owner,
        config["coordinator"],
        "createSubscription()",
        [],
        [],
        chain_id=chain_id,
        effect={
            "subscription_owner": address(owner),
            "subscription_id": "FROM_FINALIZED_CREATION_RECEIPT_AND_READBACK",
        },
    )


def add_consumer(owner, subscription, mining, *, chain_id=42161):
    config = network(chain_id)
    return intent(
        owner,
        config["coordinator"],
        "addConsumer(uint256,address)",
        ["uint256", "address"],
        [subscription_id(subscription), address(mining)],
        chain_id=chain_id,
        effect={"subscription": str(subscription), "registered_consumer": address(mining)},
    )


def fund_subscription(owner, subscription, amount, *, approved_link_cap, existing_balance=0, chain_id=42161):
    config = network(chain_id)
    if (
        type(amount) is not int
        or type(approved_link_cap) is not int
        or type(existing_balance) is not int
        or not 0 < amount <= approved_link_cap < 2**96
        or not 0 <= existing_balance < 2**96
        or amount + existing_balance >= 2**96
    ):
        raise MachineError("SOLUTION_SETUP_LINK_FUNDING_BOUND")
    sub = subscription_id(subscription)
    return intent(
        owner,
        config["link"],
        "transferAndCall(address,uint256,bytes)",
        ["address", "uint256", "bytes"],
        [address(config["coordinator"]), amount, encode(["uint256"], [sub])],
        chain_id=chain_id,
        effect={
            "subscription": str(sub),
            "asset": "LINK",
            "amount_juels": str(amount),
            "maximum_approved_link_juels": str(approved_link_cap),
            "destination": address(config["coordinator"]),
            "native_eth_transferred": "0",
        },
    )


def activate(owner, mining, *, chain_id=42161):
    return intent(
        owner,
        mining,
        "activate()",
        [],
        [],
        chain_id=chain_id,
        effect={
            "activated": True,
            "admission_paused": False,
            "subscription_funding_and_consumer_required": True,
        },
    )


def validate_admin_intent(payload):
    """Rebuild the allowlisted calldata and hash. No arbitrary approve/transfer call."""
    tx = payload.get("transaction", {})
    if set(tx) != {"from", "to", "chainId", "value", "data"} or tx.get("value") != "0x0":
        raise MachineError("SOLUTION_SETUP_ZERO_VALUE_REQUIRED")
    config = network(tx["chainId"])
    effect = payload.get("expected_effect", {})
    action = payload.get("action")
    if action == "createSubscription":
        expected = create_subscription(tx["from"], chain_id=tx["chainId"])
    elif action == "addConsumer":
        expected = add_consumer(
            tx["from"], int(effect["subscription"]), effect["registered_consumer"], chain_id=tx["chainId"]
        )
    elif action == "transferAndCall":
        expected = fund_subscription(
            tx["from"],
            int(effect["subscription"]),
            int(effect["amount_juels"]),
            approved_link_cap=int(effect["maximum_approved_link_juels"]),
            chain_id=tx["chainId"],
        )
    elif action == "activate":
        expected = activate(tx["from"], tx["to"], chain_id=tx["chainId"])
    else:
        raise MachineError("SOLUTION_SETUP_ADMIN_CALL_NOT_ALLOWED")
    if payload != expected:
        raise MachineError("SOLUTION_SETUP_INTENT_BINDING")
    if action in {"createSubscription", "addConsumer"} and tx["to"] != address(config["coordinator"]):
        raise MachineError("SOLUTION_SETUP_COORDINATOR_MISMATCH")
    return expected


def verify_mining_configuration(reader, owner, sub_id, minimum_reserve):
    """Fixed-block readback before any funding/activation, including token invariants."""
    config = network(reader.chain_id)
    if type(minimum_reserve) is not int or not 0 < minimum_reserve < 2**96:
        raise MachineError("SOLUTION_SETUP_RESERVE_REQUIRED")
    anchor = reader.anchor()
    signatures = [
        ("governor()", ["address"], address(owner)),
        ("coordinator()", ["address"], address(config["coordinator"])),
        ("subscription()", ["uint256"], subscription_id(sub_id)),
        ("keyHash()", ["bytes32"], bytes.fromhex(config["key_hash"][2:])),
        ("minimumLinkReserve()", ["uint96"], minimum_reserve),
        ("MAX_ROUNDS()", ["uint256"], 10000),
        ("PROBLEMS()", ["uint256"], 16),
        ("REWARD()", ["uint256"], 10**18),
    ]
    for signature, types, expected in signatures:
        (observed,) = reader.call(anchor, signature, [], [], types)
        if types == ["address"]:
            observed = address(observed)
        if observed != expected:
            raise MachineError("SOLUTION_SETUP_DEPLOYED_CONFIGURATION_MISMATCH")
    (activated,) = reader.call(anchor, "activated()", [], [], ["bool"])
    (paused,) = reader.call(anchor, "admissionPaused()", [], [], ["bool"])
    reward = reader.rewards(anchor, address(owner))
    reader.check_anchor(anchor)
    return {
        "chain_id": reader.chain_id,
        "mining": reader.contract,
        "governor": address(owner),
        "subscription": str(sub_id),
        "minimum_link_reserve_juels": str(minimum_reserve),
        "activated": activated,
        "admission_paused": paused,
        "reward": reward,
        "anchor": anchor,
        "assurance": "DUAL_RPC_FINALIZED_READBACK",
        "signed": False,
        "sent": False,
    }


def match_runtime(actual, compiled):
    """Only compiler-declared immutable slots may differ from the compiled template."""
    if not isinstance(actual, bytes):
        raise MachineError("SOLUTION_SETUP_RUNTIME_BYTES_REQUIRED")
    expected = bytearray.fromhex(compiled["runtime"])
    observed = bytearray(actual)
    if not expected or len(expected) != len(observed):
        raise MachineError("SOLUTION_SETUP_RUNTIME_TEMPLATE_MISMATCH")
    used = set()
    for entries in compiled["immutable_references"].values():
        for entry in entries:
            start, length = entry["start"], entry["length"]
            if (
                type(start) is not int
                or type(length) is not int
                or length != 32
                or start < 0
                or start + length > len(expected)
            ):
                raise MachineError("SOLUTION_SETUP_IMMUTABLE_LAYOUT")
            span = set(range(start, start + length))
            if span & used:
                raise MachineError("SOLUTION_SETUP_IMMUTABLE_LAYOUT")
            used |= span
            observed[start : start + length] = bytes(length)
            expected[start : start + length] = bytes(length)
    if observed != expected:
        raise MachineError("SOLUTION_SETUP_RUNTIME_TEMPLATE_MISMATCH")
    return True
