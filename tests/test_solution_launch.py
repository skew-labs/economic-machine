"""Recovery and launch boundaries: actual private WAL/native processes and hostile RPCs."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from eth_abi import decode, encode
from qualify_solution_runtime import Checkpoint, fixtures, percentile, qualify, record, verify

from economic_machine.values import MachineError, digest
from machine_engine.solution import graph
from machine_engine.solution_client import seal
from machine_engine.solution_launch import COORDINATOR, KEY_HASH, LaunchReview, deployment_data
from machine_engine.solution_operations import MiningJournal
from machine_engine.solution_recovery import publish_noreplace, recover, snapshot

ROOT = Path(__file__).resolve().parents[1]
OWNER = "0x" + "2" * 40
CONTRACT = "0x" + "1" * 40


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.source = self.base / "source"
        self.journal = MiningJournal(self.source)
        self.key = os.urandom(32)
        self.binding = {
            "seed": 12345,
            "round": 1,
            "problem": 0,
            "contract": CONTRACT,
            "miner": OWNER,
            "chain": 421614,
        }
        self.job = self.journal.reserve(self.binding, 1000, 2000, now=86400)
        self.bits = "42"
        self.graph_sha = digest(graph(12345, 0))
        self.journal.solved(self.job, {"bits": self.bits, "graph_sha256": self.graph_sha})
        self.secret = self.source / (self.job + ".secret.json")
        payload = seal(
            {
                "seed": "12345",
                "problem": "0",
                "budget": "1000",
                "search_seed": "42",
                "algorithm": "integer_anneal",
                "bits": self.bits,
            },
            contract=CONTRACT,
            round_id=1,
            miner=OWNER,
            secret_file=self.secret,
        )
        self.intent = self.journal.prepare(self.job, "commit", payload)
        self.journal.handoff(self.intent)
        self.journal.record_hash(self.intent, "0x" + "a" * 64)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_encrypted_wal_secret_restore_preserves_budget_and_holds_handoffs(self):
        payload, proof = snapshot(self.source, self.key)
        self.assertEqual(proof["verified_commit_secrets"], 1)
        self.assertNotIn(json.loads(self.secret.read_text())["salt"].encode(), payload)
        destination = self.base / "recovered"
        report = recover(payload, self.key, destination)
        restored = MiningJournal(destination)
        try:
            self.assertEqual((destination / self.secret.name).read_bytes(), self.secret.read_bytes())
            self.assertEqual(restored.intent(self.intent)["state"], "HELD")
            self.assertEqual(restored.intent(self.intent)["tx_hash"], "0x" + "a" * 64)
            self.assertEqual(restored.db.execute("SELECT reserved FROM budgets").fetchone()[0], 1000)
            for mutation in [
                lambda: restored.reserve({"seed": 2}, 1000, 2000, now=86400),
                lambda: restored.handoff(self.intent),
                lambda: restored.prepare(self.job, "claim", {}),
            ]:
                with self.assertRaisesRegex(MachineError, "RECOVERY_RECONCILIATION_REQUIRED"):
                    mutation()

            class Chain:
                def outcome(self, *_):
                    return {"state": "UNKNOWN"}

            self.assertEqual(restored.reconcile(self.intent, Chain())["state"], "UNKNOWN")
            self.assertTrue((destination / "RECOVERY_HOLD").exists())
            (destination / "RECOVERY_HOLD").unlink()
            with self.assertRaisesRegex(MachineError, "RECOVERY_RECONCILIATION_REQUIRED"):
                restored.reserve({"seed": 3}, 1000, 2000, now=86400)
            self.assertTrue(report["recovery_hold"])
            self.assertFalse(report["off_host_verified"])
        finally:
            restored.close()
        with self.assertRaises(MachineError):
            recover(payload, self.key, destination)

    def test_lost_salt_changed_binding_and_permissive_secret_fail_closed(self):
        raw = self.secret.read_bytes()
        self.secret.unlink()
        with self.assertRaises(MachineError):
            snapshot(self.source, self.key)
        self.secret.write_bytes(raw)
        self.secret.chmod(0o644)
        with self.assertRaises(MachineError):
            snapshot(self.source, self.key)
        self.secret.chmod(0o600)
        changed = json.loads(raw)
        changed["graph_sha256"] = "0" * 64
        self.secret.write_text(json.dumps(changed))
        with self.assertRaisesRegex(MachineError, "SECRET_BINDING"):
            snapshot(self.source, self.key)

    def test_modified_ciphertext_and_wrong_key_never_publish_restore(self):
        payload, _ = snapshot(self.source, self.key)
        for altered, key in [(payload[:-1] + bytes([payload[-1] ^ 1]), self.key), (payload, os.urandom(32))]:
            destination = self.base / "rejected"
            with self.assertRaises(MachineError):
                recover(altered, key, destination)
            self.assertFalse(destination.exists())

    def test_atomic_publication_never_replaces_competing_directory(self):
        source = self.base / "prepared"
        source.mkdir(mode=0o700)
        (source / "private").write_bytes(b"fixture")
        target = self.base / "competitor"
        target.mkdir(mode=0o700)
        with self.assertRaises(MachineError):
            publish_noreplace(source, target)
        self.assertTrue(source.exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_inflight_uncommitted_database_state_is_not_in_archive(self):
        self.journal.db.execute("BEGIN IMMEDIATE")
        self.journal.db.execute("UPDATE intents SET state='UNSIGNED'")
        self.journal.db.execute("ROLLBACK")
        payload, _ = snapshot(self.source, self.key)
        destination = self.base / "rolled-back"
        recover(payload, self.key, destination)
        restored = MiningJournal(destination)
        try:
            self.assertEqual(restored.intent(self.intent)["tx_hash"], "0x" + "a" * 64)
        finally:
            restored.close()


class Provider:
    def __init__(self, identity):
        self.identity = identity
        self.chain = 421614
        self.changed = False
        self.now = 1000
        self.balance = 10**18
        self.owner = OWNER
        self.calls = []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_getBlockByNumber":
            number = 104 if params[0] == "latest" else int(params[0], 16)
            return {
                "number": hex(number),
                "timestamp": hex(self.now),
                "hash": "0x" + format(number + (1 if self.changed else 0), "064x"),
            }
        if method == "eth_getCode":
            if params[0] != COORDINATOR:
                raise AssertionError("Wrong coordinator")
            return "0x6000"
        if method == "eth_call":
            return (
                "0x"
                + encode(
                    ["uint96", "uint96", "uint64", "address", "address[]"],
                    [self.balance, 0, 0, self.owner, []],
                ).hex()
            )
        raise AssertionError("Unexpected read or write " + method)


class Launch(unittest.TestCase):
    def review(self):
        self.a = Provider("a")
        self.b = Provider("b")
        return LaunchReview(self.a, self.b)

    def test_real_address_review_without_subscription_stays_blocked(self):
        review = self.review()
        report = review.inspect(OWNER, now=1010)
        self.assertEqual(report["coordinator"], COORDINATOR)
        self.assertEqual(report["tokens_minted"], 0)
        self.assertIn("REAL_VRF_SUBSCRIPTION_REQUIRED", report["remaining_gates"])
        self.assertEqual(report["status"], "BLOCKED_NO_PUBLIC_ISSUANCE")
        self.assertNotIn("eth_call", self.a.calls)
        fingerprint = report.pop("review_sha256")
        self.assertEqual(digest(report), fingerprint)

    def test_subscription_owner_link_balance_and_registration_are_independent(self):
        review = self.review()
        report = review.inspect(OWNER, 42, now=1010)
        self.assertEqual(report["subscription"]["id"], "42")
        self.assertIn("NEW_CONSUMER_REGISTRATION_REQUIRED_AFTER_DEPLOYMENT", report["remaining_gates"])
        for provider in [self.a, self.b]:
            provider.balance = 1
            provider.owner = CONTRACT
        report = review.inspect(OWNER, 42, now=1010)
        self.assertIn("SUBSCRIPTION_OWNER_MISMATCH", report["remaining_gates"])
        self.assertIn("LINK_BALANCE_BELOW_REVIEW_FLOOR", report["remaining_gates"])

    def test_wrong_chain_disagreement_stale_block_and_subid_rejected(self):
        for fault in ["chain", "changed", "now"]:
            review = self.review()
            if fault == "chain":
                self.a.chain = self.b.chain = 1
            elif fault == "changed":
                self.a.changed = True
            else:
                self.a.now = self.b.now = 500
            with self.subTest(fault=fault), self.assertRaises(MachineError):
                review.inspect(OWNER, now=1010)
        for subscription in [True, 0, -1, 2**256]:
            with self.subTest(subscription=subscription), self.assertRaises(MachineError):
                self.review().inspect(OWNER, subscription, now=1010)
        with self.assertRaises(MachineError):
            LaunchReview(Provider("same"), Provider("same"))

    def test_creation_calldata_exact_official_constructor_binding(self):
        payload = bytes.fromhex(deployment_data("6000", 42)[2:])
        self.assertEqual(payload[:2], bytes.fromhex("6000"))
        coordinator, key, subscription = decode(["address", "bytes32", "uint256"], payload[2:])
        self.assertEqual(coordinator.lower(), COORDINATOR.lower())
        self.assertEqual(key.hex(), KEY_HASH)
        self.assertEqual(subscription, 42)


class Qualification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_checkpoint_binding_exclusion_and_no_downtime_credit(self):
        binding = {"pipeline": "hash", "seconds": 86400}
        checkpoint = Checkpoint(self.path, binding)
        checkpoint.state["verified_elapsed_ns"] = 123
        checkpoint.state["state"] = "PAUSED"
        checkpoint.save()
        with self.assertRaisesRegex(MachineError, "ALREADY_RUNNING"):
            Checkpoint(self.path, binding)
        checkpoint.close()
        resumed = Checkpoint(self.path, binding)
        self.assertEqual(resumed.state["verified_elapsed_ns"], 123)
        self.assertEqual(resumed.state["resumes"], 1)
        resumed.close()
        with self.assertRaises(MachineError):
            Checkpoint(self.path, {"pipeline": "changed"})

    def test_tampered_checkpoint_and_failed_run_cannot_be_resumed(self):
        checkpoint = Checkpoint(self.path, {})
        checkpoint.state["state"] = "FAILED"
        checkpoint.save()
        checkpoint.close()
        with self.assertRaisesRegex(MachineError, "FAILED_REQUIRES_NEW_RUN"):
            Checkpoint(self.path, {})
        raw = json.loads(self.path.read_text())
        raw["jobs"] = 999
        self.path.write_text(json.dumps(raw))
        with self.assertRaises(MachineError):
            Checkpoint(self.path, {})

    def test_real_native_resume_fault_and_output_verification(self):
        build = json.loads((ROOT / "artifacts/solution-operations/native-build.json").read_text())
        first = qualify(
            build["executable"],
            build["sha256"],
            self.path,
            10,
            stop=lambda: self.path.exists(),
            inject_every=2,
        )
        self.assertEqual(first["state"], "PAUSED")
        # Stop after a few batches without waiting ten seconds; then resume.
        count = [0]

        def stop():
            count[0] += 1
            return count[0] > 5

        second = qualify(build["executable"], build["sha256"], self.path, 10, stop=stop, inject_every=2)
        self.assertGreater(second["jobs"], 0)
        self.assertEqual(second["state"], "PAUSED")
        self.assertGreater(second["injected_worker_terminations"], 0)
        payload, edges = fixtures()
        result = subprocess.run(
            [build["executable"]], input=payload, capture_output=True, timeout=10, check=False
        )
        self.assertEqual(len(verify(result.stdout, edges)), 16)
        with self.assertRaises(MachineError):
            verify(result.stdout[:-1], edges)
        damaged = bytearray(result.stdout)
        damaged[0] ^= 1
        with self.assertRaises(MachineError):
            verify(bytes(damaged), edges)
        with self.assertRaises(MachineError):
            qualify(build["executable"], "0" * 64, self.path, 10)

    def test_histogram_remains_bounded_and_reports_percentile_upper_bounds(self):
        histogram = {}
        for value in [10, 15, 100, 120]:
            record(histogram, value)
        self.assertEqual(percentile(histogram, 99), 128)
        self.assertEqual(sum(histogram.values()), 4)
        self.assertLessEqual(len(histogram), 3)
