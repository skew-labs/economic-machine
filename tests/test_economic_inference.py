"""Inference suggestions are inert until the economic engine reviews them."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from economic_machine.compiler import compile_program
from economic_machine.inference import assess_inference
from economic_machine.runtime import MachineRuntime
from economic_machine.state import state_root
from economic_machine.values import MachineError


BASE = Path(__file__).resolve().parents[1] / "cases"
AT = "2026-09-25T12:01:00+00:00"


class EconomicInferenceTests(unittest.TestCase):
    def setUp(self):
        self.world = json.loads((BASE / "economic_state_demo.json").read_text())
        self.program = json.loads((BASE / "economic_program_graph_demo.json").read_text())
        self.scope = {
            "schema_version": "economic-inference-scope-1",
            "request_hash": "a" * 64,
            "state_root": state_root(self.world),
            "program_id": self.program["program_id"],
            "owner_id": self.program["owner_id"],
            "agent_id": self.program["agent_id"],
            "network": self.program["network"],
            "sandbox": copy.deepcopy(self.program["sandbox"]),
            "allowed_fact_paths": ["risk.coverage_ratio"],
        }
        self.draft = {
            "schema_version": "economic-inference-draft-1",
            "model_id": "qwen-32b",
            "model_hash": "b" * 64,
            "request_hash": self.scope["request_hash"],
            "state_root": self.scope["state_root"],
            "created_at": "2026-09-25T12:00:00+00:00",
            "expires_at": "2026-09-25T12:05:00+00:00",
            "confidence": "0.92",
            "ambiguities": [],
            "program": copy.deepcopy(self.program),
        }

    def test_valid_model_proposal_is_audited_but_never_registered(self):
        pure = assess_inference(self.draft, self.scope, self.world, at=AT)
        self.assertEqual(pure["status"], "REVIEW_REQUIRED")
        self.assertEqual(pure["program_hash"], compile_program(self.program)["program_hash"])
        self.assertEqual((pure["registration_status"], pure["execution_authority"],
                          pure["chain_status"]),
                         ("NOT_REGISTERED", "NONE", "NOT_SUBMITTED"))
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "inference.sqlite3")
            runtime.install_state(self.world)
            first = runtime.record_inference(self.draft, self.scope, at=AT)
            self.assertFalse(first["idempotent"])
            self.assertTrue(runtime.record_inference(self.draft, self.scope, at=AT)
                            ["idempotent"])
            self.assertEqual(first["assessment_hash"], pure["assessment_hash"])
            self.assertTrue(runtime.verify_inference(first["assessment_hash"]))
            status = runtime.status()
            self.assertEqual((status["inference_assessments"], status["receipts"],
                              status["active_reservations"], status["active_programs"]),
                             (1, 0, 0, []))
            with self.assertRaisesRegex(MachineError, "not registered"):
                runtime.evaluate(self.program["program_id"], at=AT)

    def test_missing_information_abstains_without_compiling_program(self):
        draft = copy.deepcopy(self.draft)
        draft["ambiguities"] = ["WITHDRAWAL_TIME_UNKNOWN", "AMOUNT_UNIT_UNKNOWN"]
        draft["program"] = None
        result = assess_inference(draft, self.scope, self.world, at=AT)
        self.assertEqual(result["status"], "ABSTAIN")
        self.assertEqual(result["reason_codes"],
                         ["MISSING_INFORMATION", "WITHDRAWAL_TIME_UNKNOWN",
                          "AMOUNT_UNIT_UNKNOWN"])
        self.assertIsNone(result["program_hash"])
        self.assertEqual(result, assess_inference(draft, self.scope, self.world, at=AT))

    def test_stale_or_context_mismatched_inference_never_becomes_program(self):
        stale = assess_inference(self.draft, self.scope, self.world,
                                 at="2026-09-25T12:06:00+00:00")
        self.assertEqual((stale["status"], stale["reason_codes"]),
                         ("ABSTAIN", ["STALE_INFERENCE"]))
        mismatch = copy.deepcopy(self.draft)
        mismatch["request_hash"] = "c" * 64
        result = assess_inference(mismatch, self.scope, self.world, at=AT)
        self.assertEqual((result["status"], result["reason_codes"]),
                         ("REJECTED", ["CONTEXT_MISMATCH"]))
        changed = copy.deepcopy(self.world)
        changed["balances"]["USDT"] = "900"
        with self.assertRaisesRegex(MachineError, "different state"):
            assess_inference(self.draft, self.scope, changed, at=AT)

    def test_engine_rejects_overpowered_or_uncompilable_proposals(self):
        narrow = copy.deepcopy(self.scope)
        narrow["sandbox"]["capital_limit"] = "100"
        narrow["sandbox"]["max_exposure"] = "100"
        self.assertEqual(assess_inference(self.draft, narrow, self.world, at=AT)
                         ["reason_codes"], ["OUTSIDE_SCOPE"])
        forbidden = copy.deepcopy(self.draft)
        forbidden["program"]["instructions"][4]["op"] = "HEDGE"
        self.assertEqual(assess_inference(forbidden, self.scope, self.world, at=AT)
                         ["reason_codes"], ["COMPILE_REJECTED"])
        foreign = copy.deepcopy(self.draft)
        foreign["program"]["owner_id"] = "another-owner"
        self.assertEqual(assess_inference(foreign, self.scope, self.world, at=AT)
                         ["reason_codes"], ["OUTSIDE_SCOPE"])
        unrelated = copy.deepcopy(self.draft)
        unrelated["program"]["instructions"][0]["path"] = "wallet.private_balance"
        self.assertEqual(assess_inference(unrelated, self.scope, self.world, at=AT)
                         ["reason_codes"], ["OUTSIDE_SCOPE"])

    def test_binary_float_and_unbounded_model_output_are_rejected(self):
        draft = copy.deepcopy(self.draft)
        draft["confidence"] = 0.92
        with self.assertRaises(MachineError):
            assess_inference(draft, self.scope, self.world, at=AT)
        draft = copy.deepcopy(self.draft)
        draft["program"]["unreviewed_text"] = "x" * 70000
        with self.assertRaisesRegex(MachineError, "size limit"):
            assess_inference(draft, self.scope, self.world, at=AT)

    def test_inference_audit_replay_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inference.sqlite3"
            runtime = MachineRuntime(path)
            runtime.install_state(self.world)
            result = runtime.record_inference(self.draft, self.scope, at=AT)
            self.assertTrue(runtime.verify_inference(result["assessment_hash"]))
            with sqlite3.connect(path) as db:
                db.execute("UPDATE inference_assessments SET candidate_json='{}' "
                           "WHERE assessment_hash=?", (result["assessment_hash"],))
            self.assertFalse(runtime.verify_inference(result["assessment_hash"]))

    def test_tampered_journal_blocks_new_inference_assessment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inference.sqlite3"
            runtime = MachineRuntime(path)
            runtime.install_state(self.world)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE events SET event_json='{}' WHERE ordinal=1")
            with self.assertRaisesRegex(MachineError, "journal integrity failed"):
                runtime.record_inference(self.draft, self.scope, at=AT)
            self.assertEqual(runtime.status()["inference_assessments"], 0)

    def test_unknown_state_opens_one_outbox_item_until_verified_state_recovers(self):
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["quality"] = "MISSING"
        world["facts"]["risk.coverage_ratio"]["value"] = None
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "escalations.sqlite3")
            runtime.install_state(world)
            runtime.register_program(self.program)
            first = runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(first["terminal_state"], "ESCALATED")
            self.assertEqual(len(runtime.pending_escalations()), 1)
            self.assertEqual(runtime.status()["open_escalations"], 1)
            before = runtime.status()["events"]
            runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(runtime.status()["events"], before)
            self.assertEqual(runtime.pending_escalations()[0]["receipt_hash"],
                             first["receipt_hash"])
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": world["owner_id"], "network": world["network"],
                     "sequence": 1, "previous_root": state_root(world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {"risk.coverage_ratio": {
                         **world["facts"]["risk.coverage_ratio"], "quality": "VALID",
                         "value": "1.6", "observed_at": "2026-09-25T12:02:00+00:00"}},
                     "quote_updates": {},
                     "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}}}
            recovered = runtime.ingest(delta, event_id="risk-recovered-001")
            self.assertEqual((recovered["kernel_calls"], recovered["llm_calls"]), (1, 0))
            self.assertEqual(runtime.pending_escalations(), [])
            self.assertEqual(runtime.status()["open_escalations"], 0)
            self.assertTrue(runtime.status()["journal_integrity"])
            self.assertTrue(runtime.verify_receipt(first["receipt_hash"]))
            self.assertTrue(runtime.verify_receipt(recovered["receipt_hashes"][0]))

    def test_pause_and_revision_retire_unknown_state_requests(self):
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["quality"] = "MISSING"
        world["facts"]["risk.coverage_ratio"]["value"] = None
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "escalations.sqlite3")
            runtime.install_state(world)
            runtime.register_program(self.program)
            runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(len(runtime.pending_escalations()), 1)
            runtime.pause_program(self.program["program_id"], reason="MANUAL_RISK_STOP")
            self.assertEqual(runtime.pending_escalations(), [])
            runtime.resume_program(self.program["program_id"], reason="REVIEWED")
            runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(len(runtime.pending_escalations()), 1)
            revision = copy.deepcopy(self.program)
            revision["sandbox"]["max_total_cost"] = "9"
            runtime.register_program(revision)
            self.assertEqual(runtime.pending_escalations(), [])
            with self.assertRaisesRegex(MachineError, "reserved event id"):
                runtime.ingest({}, event_id="escalation:123")
            self.assertTrue(runtime.status()["journal_integrity"])

    def test_tampered_journal_blocks_unknown_state_outbox_read(self):
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["quality"] = "MISSING"
        world["facts"]["risk.coverage_ratio"]["value"] = None
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "escalations.sqlite3"
            runtime = MachineRuntime(path)
            runtime.install_state(world)
            runtime.register_program(self.program)
            runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(len(runtime.pending_escalations()), 1)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE events SET event_json='{}' WHERE ordinal=1")
            with self.assertRaisesRegex(MachineError, "journal integrity failed"):
                runtime.pending_escalations()

    def test_tampered_outbox_row_is_rejected(self):
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["quality"] = "MISSING"
        world["facts"]["risk.coverage_ratio"]["value"] = None
        for mutation in ("UPDATE escalations SET reason_code='OTHER'",
                         "DELETE FROM escalations"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "escalations.sqlite3"
                runtime = MachineRuntime(path)
                runtime.install_state(world)
                runtime.register_program(self.program)
                runtime.evaluate(self.program["program_id"], at=AT)
                self.assertEqual(len(runtime.pending_escalations()), 1)
                with sqlite3.connect(path) as db:
                    db.execute(mutation)
                self.assertTrue(runtime.status()["journal_integrity"])
                with self.assertRaisesRegex(MachineError, "escalation projection"):
                    runtime.pending_escalations()


if __name__ == "__main__":
    unittest.main()
