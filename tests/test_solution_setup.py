"""VRF administration ABI and spending bounds, with no public transaction."""

import copy
import unittest
from unittest.mock import patch

from eth_abi import decode, encode
from solution_setup import AdminReadRPC, quote
from test_solution_mainnet import Fixture

from economic_machine.values import MachineError
from machine_engine.solution_networks import NETWORKS
from machine_engine.solution_setup import (
    activate,
    add_consumer,
    create_subscription,
    fund_subscription,
    match_runtime,
    validate_admin_intent,
    verify_mining_configuration,
)

OWNER = "0x" + "1" * 40
MINING = "0x" + "2" * 40


class QuoteRPC:
    def __init__(self, identity):
        self.identity = identity
        self.nonce = 4
        self.pending = 4
        self.chain = 42161
        self.balance = 10**18
        self.estimate = 100000
        self.price = 10000000

    def __call__(self, method, params):
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getTransactionCount":
            return hex(self.pending if params[1] == "pending" else self.nonce)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_estimateGas":
            return hex(self.estimate)
        if method == "eth_gasPrice":
            return hex(self.price)
        raise AssertionError(method)


class SetupABI(unittest.TestCase):
    def test_creation_does_not_guess_subscription_and_all_outputs_unsigned(self):
        created = create_subscription(OWNER)
        self.assertEqual(created["transaction"]["to"], NETWORKS[42161]["coordinator"])
        self.assertEqual(created["transaction"]["chainId"], 42161)
        self.assertFalse(created["signed"])
        self.assertFalse(created["sent"])
        self.assertIn("FROM_FINALIZED", created["expected_effect"]["subscription_id"])
        for value in [None, 0, -1, True, "1", 2**256]:
            with self.subTest(value=value), self.assertRaises(MachineError):
                add_consumer(OWNER, value, MINING)

    def test_exact_erc677_link_funding_and_no_native_eth_transfer(self):
        intent = fund_subscription(OWNER, 123, 10**18, approved_link_cap=2 * 10**18)
        tx = intent["transaction"]
        target, amount, data = decode(["address", "uint256", "bytes"], bytes.fromhex(tx["data"][10:]))
        self.assertEqual(target.lower(), NETWORKS[42161]["coordinator"].lower())
        self.assertEqual(amount, 10**18)
        self.assertEqual(data, encode(["uint256"], [123]))
        self.assertEqual(tx["value"], "0x0")
        self.assertEqual(tx["to"], NETWORKS[42161]["link"])
        self.assertEqual(validate_admin_intent(intent), intent)

    def test_link_cap_overflow_and_exact_budget_units(self):
        for amount, cap, balance in [
            (0, 10, 0),
            (11, 10, 0),
            (True, 10, 0),
            (1, 10, 2**96 - 1),
            (1, 2**96, 0),
            (1.1, 10, 0),
        ]:
            with self.subTest(amount=amount), self.assertRaises(MachineError):
                fund_subscription(OWNER, 1, amount, approved_link_cap=cap, existing_balance=balance)

    def test_changed_recipient_selector_chain_value_or_review_hash_fails(self):
        original = fund_subscription(OWNER, 123, 10**18, approved_link_cap=2 * 10**18)
        for change in [{"to": MINING}, {"chainId": 1}, {"value": "0x1"}, {"data": "0xdeadbeef"}]:
            altered = copy.deepcopy(original)
            altered["transaction"].update(change)
            with self.subTest(change=change), self.assertRaises(MachineError):
                validate_admin_intent(altered)
        altered = copy.deepcopy(original)
        altered["intent_sha256"] = "0" * 64
        with self.assertRaises(MachineError):
            validate_admin_intent(altered)

    def test_consumer_and_activation_are_separate_from_participant_authority(self):
        for intent in [add_consumer(OWNER, 123, MINING), activate(OWNER, MINING)]:
            self.assertTrue(intent["requires_exact_owner_approval"])
            self.assertFalse(intent["participant_signer_admits_this_action"])
            self.assertEqual(validate_admin_intent(intent), intent)

    def test_pinned_actual_fixture_config_readback_and_wrong_constructor_rejected(self):
        f = Fixture()
        f.tester.mine_blocks(8)
        reader = f.reader()
        # Explicit mock-only network override; this is not a live coordinator/subscription.
        config = NETWORKS[42161] | {"coordinator": f.vrf.address, "key_hash": "0x" + (b"k" * 32).hex()}
        with patch.dict(NETWORKS, {42161: config}):
            observed = verify_mining_configuration(reader, f.owner, 1, 10**17)
            self.assertFalse(observed["activated"])
            self.assertTrue(observed["admission_paused"])
            self.assertEqual(observed["reward"]["supply"], "0")
            for owner, sub, reserve in [(f.other, 1, 10**17), (f.owner, 2, 10**17), (f.owner, 1, 10**18)]:
                with self.subTest(sub=sub), self.assertRaises(MachineError):
                    verify_mining_configuration(reader, owner, sub, reserve)
        with self.assertRaises(MachineError):
            verify_mining_configuration(reader, f.owner, 1, 10**17)

    def test_runtime_abi_imitation_and_changes_outside_immutable_slots_rejected(self):
        f = Fixture()
        item = f.artifacts["SkewSolutionMining"]
        actual = bytes(f.w3.eth.get_code(f.contract.address))
        self.assertTrue(match_runtime(actual, item))
        corrupted = bytearray(actual)
        corrupted[0] ^= 1
        with self.assertRaises(MachineError):
            match_runtime(bytes(corrupted), item)
        with self.assertRaises(MachineError):
            match_runtime(actual[:-1], item)
        token = f.artifacts["SkewSolutionToken"]
        self.assertTrue(match_runtime(bytes(f.w3.eth.get_code(f.token.address)), token))


class SetupQuote(unittest.TestCase):
    def test_two_provider_nonce_balance_and_fee_envelope_without_signing(self):
        a, b = QuoteRPC("a"), QuoteRPC("b")
        b.estimate = 120000
        review = quote(a, b, create_subscription(OWNER), 10**15)
        self.assertEqual(review["unsigned_transaction"]["gas"], 144000)
        self.assertEqual(review["maximum_gas_wei"], str(144000 * 20000000))
        self.assertEqual(review["unsigned_transaction"]["nonce"], 4)
        self.assertTrue(review["balance_sufficient"])
        self.assertFalse(review["sent"])
        self.assertFalse(review["signed"])

    def test_pending_wallet_and_provider_nonce_disagreement_stop_planning(self):
        for attr, value in [("pending", 5), ("nonce", 5), ("chain", 421614)]:
            a, b = QuoteRPC("a"), QuoteRPC("b")
            setattr(b, attr, value)
            with self.subTest(attr=attr), self.assertRaises(MachineError):
                quote(a, b, create_subscription(OWNER), 10**15)
        a, b = QuoteRPC("a"), QuoteRPC("b")
        b.pending = b.nonce = 6
        with self.assertRaises(MachineError):
            quote(a, b, create_subscription(OWNER), 10**15)

    def test_fee_cap_cannot_be_ignored_and_no_funding_when_balance_missing(self):
        a, b = QuoteRPC("a"), QuoteRPC("b")
        with self.assertRaises(MachineError):
            quote(a, b, create_subscription(OWNER), 1)
        b.balance = 0
        self.assertFalse(quote(a, b, create_subscription(OWNER), 10**15)["balance_sufficient"])

    def test_admin_read_transport_still_has_no_broadcast_method(self):
        provider = AdminReadRPC("https://example.org")
        for method in [
            "eth_sendRawTransaction",
            "eth_sendTransaction",
            "personal_unlockAccount",
            "anvil_impersonateAccount",
        ]:
            with self.subTest(method=method), self.assertRaises(MachineError):
                provider(method, [])


if __name__ == "__main__":
    unittest.main()
