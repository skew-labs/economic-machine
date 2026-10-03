"""Disposable owner signing tests; no public transaction or persisted user key."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_abi import encode
from eth_utils import keccak
from test_solution_mainnet import Fixture, hx

from economic_machine.values import MachineError
from machine_engine.solution_signer import OwnerOutbox, validate_intent


class OwnerSigner(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.start()
        self.f.tester.mine_blocks(8)
        self.temp = tempfile.TemporaryDirectory()
        self.reader = self.f.reader()
        self.key = self.f.tester.backend.account_keys[1].to_bytes()
        self.directory = Path(self.temp.name) / "signer"
        self.outbox = OwnerOutbox(
            self.directory, self.reader, self.f.miner, per_tx_limit=10**15, daily_limit=2 * 10**15
        )
        data = keccak(text="commit(uint256,uint8,bytes32)")[:4] + encode(
            ["uint256", "uint8", "bytes32"], [1, 0, b"x" * 32]
        )
        self.payload = {
            "transaction": {
                "to": self.f.contract.address,
                "from": self.f.miner,
                "chainId": 42161,
                "value": "0x0",
                "data": hx(data),
            }
        }
        self.fees = {"nonce": 0, "gas": 150000, "maxFeePerGas": 2 * 10**9, "maxPriorityFeePerGas": 0}
        self.now = self.f.w3.eth.get_block("latest").timestamp

    def tearDown(self):
        self.outbox.close()
        self.temp.cleanup()

    def sign(self, payload=None, fees=None):
        return self.outbox.sign(payload or self.payload, fees or self.fees, self.key, now=self.now)

    def broadcast(self, intent, sender):
        with patch("machine_engine.solution_signer.time.time", return_value=self.now):
            return self.outbox.broadcast(intent["id"], sender, approved_hash=intent["tx_hash"])

    def test_signed_private_outbox_restart_and_config_binding(self):
        signed = self.sign()
        self.assertEqual(signed["state"], "SIGNED")
        row = self.outbox.db.execute("SELECT raw FROM signed").fetchone()[0]
        self.assertNotIn("raw", signed)
        self.assertNotIn("salt", json.dumps(signed))
        self.assertGreater(len(row), 100)
        self.assertEqual((self.directory / "signer.sqlite3").stat().st_mode & 0o777, 0o600)
        self.outbox.close()
        self.outbox = OwnerOutbox(
            self.directory, self.reader, self.f.miner, per_tx_limit=10**15, daily_limit=2 * 10**15
        )
        with self.assertRaises(MachineError):
            self.sign()
        with self.assertRaises(MachineError):
            OwnerOutbox(
                self.directory, self.reader, self.f.miner, per_tx_limit=2 * 10**15, daily_limit=2 * 10**15
            )

    def test_copy_owner_foreign_chain_value_admin_and_noncanonical_calldata_blocked(self):
        for change in [
            {"chainId": 421614},
            {"to": self.f.token.address},
            {"value": "0x1"},
            {"from": self.f.other},
            {"data": hx(keccak(text="request()")[:4])},
            {"data": self.payload["transaction"]["data"] + "00"},
        ]:
            bad = {"transaction": self.payload["transaction"] | change}
            with self.subTest(change=change), self.assertRaises(MachineError):
                self.sign(bad)
        self.assertEqual(self.outbox.db.execute("SELECT count(*) FROM signed").fetchone()[0], 0)

    def test_fee_cap_bounds_and_exact_hash_approval(self):
        for change in [
            {"gas": 4000001},
            {"maxFeePerGas": 10**12},
            {"nonce": -1},
            {"gas": True},
            {"maxPriorityFeePerGas": 3 * 10**9},
        ]:
            with self.subTest(change=change), self.assertRaises(MachineError):
                self.sign(fees=self.fees | change)
        signed = self.sign()
        called = []
        with self.assertRaises(MachineError):
            self.outbox.broadcast(signed["id"], lambda raw: called.append(raw), approved_hash="0x" + "0" * 64)
        self.assertFalse(called)

    def test_unknown_network_outcome_never_resends_even_after_restart(self):
        signed = self.sign()
        calls = []

        def timeout(raw):
            calls.append(raw)
            raise TimeoutError("Test-only unavailable transport")

        result = self.broadcast(signed, timeout)
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(len(calls), 1)
        self.outbox.close()
        self.outbox = OwnerOutbox(
            self.directory, self.reader, self.f.miner, per_tx_limit=10**15, daily_limit=2 * 10**15
        )
        with self.assertRaises(MachineError):
            self.broadcast(signed, timeout)
        with self.assertRaises(MachineError):
            self.sign(fees=self.fees | {"nonce": 1})
        self.assertEqual(len(calls), 1)

    def test_known_transaction_hash_survives_wrong_rpc_reply(self):
        signed = self.sign()
        with self.assertRaises(MachineError):
            self.broadcast(signed, lambda raw: "0x" + "f" * 64)
        self.assertEqual(
            self.outbox.db.execute("SELECT state,tx_hash FROM signed").fetchone(),
            ("UNKNOWN", signed["tx_hash"]),
        )

    def test_real_fixture_signature_submission_and_finalized_receipt_reconciliation(self):
        signed = self.sign()
        result = self.broadcast(signed, lambda raw: hx(self.f.w3.eth.send_raw_transaction(raw)))
        self.assertEqual(result["state"], "SUBMITTED_NOT_FINALIZED")
        final = self.outbox.reconcile(signed["id"])
        self.assertEqual(final["state"], "CONFIRMED")
        self.assertEqual(self.f.contract.functions.submissions(1, 0, self.f.miner).call()[1], 1)
        with self.assertRaises(MachineError):
            self.broadcast(signed, lambda raw: self.fail("No resend"))

    def test_durable_daily_budget_and_clock_rollback(self):
        signed = self.sign()
        self.outbox.db.execute(
            "UPDATE signed SET state='CONFIRMED' WHERE id=?", (signed["id"],)
        )  # State fixture only, no false live outcome.
        with self.assertRaises(MachineError):
            self.outbox.sign(self.payload, self.fees | {"nonce": 1}, self.key, now=self.now - 86400)
        self.outbox.db.execute("UPDATE spend SET reserved=?", (2 * 10**15,))
        with self.assertRaises(MachineError):
            self.sign(fees=self.fees | {"nonce": 1})

    def test_phase_and_chain_code_are_rechecked_before_broadcast(self):
        signed = self.sign()
        self.f.send(self.f.contract.functions.pauseAdmission(True))
        self.f.tester.mine_blocks(8)
        with self.assertRaises(MachineError):
            self.broadcast(signed, lambda raw: self.fail("Paused commit cannot transmit"))
        self.assertEqual(self.outbox.db.execute("SELECT state FROM signed").fetchone()[0], "SIGNED")

    def test_reveal_requires_chain_commitment_and_qualified_score(self):
        bits = int(self.f.candidate()["bits"])
        salt = self.f.commit(bits)
        self.f.phase(3)
        data = keccak(text="reveal(uint256,uint8,uint32,bytes32)")[:4] + encode(
            ["uint256", "uint8", "uint32", "bytes32"], [1, 0, bits, salt]
        )
        payload = {"transaction": self.payload["transaction"] | {"data": hx(data)}}
        now = self.f.w3.eth.get_block("latest").timestamp
        self.assertEqual(validate_intent(payload, self.reader, now=now), "reveal")
        bad = data[:-32] + b"t" * 32
        with self.assertRaises(MachineError):
            validate_intent({"transaction": payload["transaction"] | {"data": hx(bad)}}, self.reader, now=now)

    def test_finalized_claim_requires_expected_mint_receipt_and_claimant(self):
        bits = int(self.f.candidate()["bits"])
        salt = self.f.commit(bits)
        self.f.phase(3)
        self.f.send(self.f.contract.functions.reveal(1, 0, bits, salt), self.f.miner)
        self.f.phase(4)
        self.f.send(self.f.contract.functions.finalize(1))
        self.f.tester.mine_blocks(8)
        data = keccak(text="claim(uint256,uint8)")[:4] + encode(["uint256", "uint8"], [1, 0])
        payload = {"transaction": self.payload["transaction"] | {"data": hx(data)}}
        now = self.f.w3.eth.get_block("latest").timestamp
        with self.assertRaises(MachineError):
            validate_intent(payload, self.reader, now=now)
        payload["expected_reward"] = {
            "token": self.f.token.address,
            "round": 1,
            "problem": 0,
            "amount": str(10**18),
        }
        self.assertEqual(validate_intent(payload, self.reader, now=now), "claim")

    def test_canonical_pending_commit_can_progress_before_l1_finality(self):
        bits = int(self.f.candidate()["bits"])
        salt = b"s" * 32
        fingerprint = self.f.contract.functions.commitmentFor(1, 0, self.f.miner, bits, salt).call()
        data = keccak(text="commit(uint256,uint8,bytes32)")[:4] + encode(
            ["uint256", "uint8", "bytes32"], [1, 0, fingerprint]
        )
        self.payload["transaction"]["data"] = hx(data)
        signed = self.sign()
        self.broadcast(signed, lambda raw: hx(self.f.w3.eth.send_raw_transaction(raw)))
        original = self.reader.outcome

        def waiting(tx_hash, payload):
            result = original(tx_hash, payload)
            if result["state"] == "CONFIRMED":
                result["state"] = "PENDING_FINALITY"
            return result

        self.reader.outcome = waiting
        # A recent receipt without the working confirmation margin still blocks progression.
        alternate = {
            "transaction": self.payload["transaction"]
            | {
                "data": hx(
                    keccak(text="commit(uint256,uint8,bytes32)")[:4]
                    + encode(["uint256", "uint8", "bytes32"], [1, 1, b"z" * 32])
                )
            }
        }
        with self.assertRaises(MachineError):
            self.sign(alternate, self.fees | {"nonce": 1})
        self.f.phase(3)
        self.now = self.f.w3.eth.get_block("latest").timestamp
        reveal_data = keccak(text="reveal(uint256,uint8,uint32,bytes32)")[:4] + encode(
            ["uint256", "uint8", "uint32", "bytes32"], [1, 0, bits, salt]
        )
        reveal_payload = {"transaction": self.payload["transaction"] | {"data": hx(reveal_data)}}
        revealed = self.sign(
            reveal_payload, self.fees | {"nonce": 1, "gas": 2000000, "maxFeePerGas": 400000000}
        )
        with patch("machine_engine.solution_signer.time.time", return_value=self.now):
            sent = self.outbox.broadcast(
                revealed["id"],
                lambda raw: hx(self.f.w3.eth.send_raw_transaction(raw)),
                approved_hash=revealed["tx_hash"],
            )
        self.assertEqual(sent["state"], "SUBMITTED_NOT_FINALIZED")
        self.assertTrue(self.f.contract.functions.submissions(1, 0, self.f.miner).call()[2])
        self.assertEqual(
            self.outbox.db.execute("SELECT state FROM signed WHERE id=?", (signed["id"],)).fetchone()[0],
            "PENDING_FINALITY",
        )


if __name__ == "__main__":
    unittest.main()
