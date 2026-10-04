"""Unsigned release review and adversarial mainnet read checks."""

import unittest

from eth_abi import encode
from solution_mainnet_preflight import deployment_review, inspect_network

from economic_machine.values import MachineError


class NetworkRPC:
    def __init__(self, identity):
        self.identity = identity
        self.chain = 42161
        self.block = 100
        self.code = "0x6000"
        self.changed = False

    def __call__(self, method, params):
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getBlockByNumber":
            height = self.block if params[0] == "finalized" else int(params[0], 16)
            return {
                "number": hex(height),
                "timestamp": hex(1000),
                "hash": "0x" + format(height + self.changed, "064x"),
            }
        if method == "eth_getCode":
            return self.code
        if method == "eth_call":
            return (
                "0x"
                + encode(
                    ["uint96", "uint96", "uint64", "address", "address[]"],
                    [10**18, 0, 0, "0x" + "1" * 40, ["0x" + "2" * 40]],
                ).hex()
            )
        raise AssertionError(method)


class Preflight(unittest.TestCase):
    def test_two_provider_finalized_state_and_subscription(self):
        a, b = NetworkRPC("a"), NetworkRPC("b")
        b.block = 102
        result = inspect_network(a, b, subscription=123)
        self.assertEqual(result["anchor"]["number"], 100)
        self.assertEqual(result["submitted_transactions"], 0)
        self.assertFalse(result["own_contract_deployed"])
        self.assertEqual(result["subscription"]["id"], "123")

    def test_network_code_or_anchor_disagreement_fail_closed(self):
        for field, value in [("chain", 421614), ("code", "0x6001"), ("changed", True)]:
            a, b = NetworkRPC("a"), NetworkRPC("b")
            setattr(b, field, value)
            with self.subTest(field=field), self.assertRaises(MachineError):
                inspect_network(a, b)
        with self.assertRaises(MachineError):
            inspect_network(NetworkRPC("same"), NetworkRPC("same"))

    def test_invalid_network_sub_id_empty_code(self):
        for chain in [True, 1, "42161"]:
            with self.assertRaises(MachineError):
                inspect_network(NetworkRPC("a"), NetworkRPC("b"), chain)
        for sub in [-1, True, 0, 2**256]:
            with self.assertRaises(MachineError):
                inspect_network(NetworkRPC("a"), NetworkRPC("b"), subscription=sub)
        a, b = NetworkRPC("a"), NetworkRPC("b")
        a.code = b.code = "0x"
        with self.assertRaises(MachineError):
            inspect_network(a, b)

    def test_constructor_exact_parameters_and_no_signature_or_activation(self):
        result = deployment_review("0x" + "1" * 40, 123, 10**18)
        self.assertEqual(result["chain_id"], 42161)
        self.assertEqual(result["constructor"]["subscription"], "123")
        self.assertEqual(result["token"]["cap_tokens"], 160000)
        self.assertFalse(result["signed"])
        self.assertFalse(result["sent"])
        self.assertFalse(result["public_launch_ready"])
        self.assertNotIn("to", result["unsigned_transaction"])
        self.assertEqual(result["unsigned_transaction"]["value"], "0x0")
        self.assertGreater(len(result["unsigned_transaction"]["data"]), 10000)
        changed = deployment_review("0x" + "2" * 40, 123, 10**18)
        self.assertNotEqual(result["initcode_sha256"], changed["initcode_sha256"])

    def test_missing_owner_subscription_or_reserve_never_make_review(self):
        for owner, sub, reserve in [
            ("", 1, 1),
            ("0x" + "0" * 40, 1, 1),
            ("0x" + "1" * 40, 0, 1),
            ("0x" + "1" * 40, 1, None),
            ("0x" + "1" * 40, 1, 0),
        ]:
            with self.assertRaises(MachineError):
                deployment_review(owner, sub, reserve)


if __name__ == "__main__":
    unittest.main()
