"""Economic ISA/kernel/runtime contracts; synthetic state only, run on Cherry."""

import copy
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from economic_machine.cli import main
from economic_machine.compiler import compile_program
from economic_machine.kernel import EconomicKernel
from economic_machine.runtime import MachineRuntime
from economic_machine.settlement import SettlementVerifier, settle
from economic_machine.state import state_root
from economic_machine.values import MachineError


BASE = Path(__file__).resolve().parents[1] / "cases"
AT = "2026-09-25T12:01:00+00:00"


class FixtureVerifier(SettlementVerifier):
    """Test-only stub; no production chain verification capability."""

    def verify_owner_authorization(self, intent, authorization):
        return authorization["signature_ref"] == "fixture_signature"

    def verify_finalized_transaction(self, proof):
        return proof["raw_receipt_hash"] == "d" * 64


class EconomicMachineTests(unittest.TestCase):
    def setUp(self):
        self.program = json.loads((BASE / "economic_program_demo.json").read_text())
        self.world = json.loads((BASE / "economic_state_demo.json").read_text())

    def _receipt(self, program=None, world=None, at=AT):
        return EconomicKernel().evaluate(compile_program(program or self.program),
                                         world or self.world, at=at)

    def _graph_case(self):
        program = json.loads((BASE / "economic_program_graph_demo.json").read_text())
        world = copy.deepcopy(self.world)
        world["quotes"]["quote_C"] = {
            "quote_id": "quote_C", "protocol": "venue_B", "asset": "USDT",
            "amount": "100", "fee": "1", "slippage_cost": "0.5", "gas_cost": "0.5",
            "observed_at": "2026-09-25T12:00:00+00:00",
            "expires_at": "2026-09-25T13:00:00+00:00",
            "source_hash": "d" * 64}
        return program, world

    def test_compile_run_and_exact_replay(self):
        first = self._receipt()
        second = self._receipt()
        self.assertEqual(first, second)
        self.assertEqual(first["terminal_state"], "AWAITING_AUTHORIZATION")
        self.assertEqual(first["phase"], "AWAITING_AUTHORIZATION")
        self.assertEqual(first["phase_trace"][0],
                         {"from": "CREATED", "opcode": "OBSERVE", "to": "OBSERVING"})
        self.assertEqual(first["phase_trace"][-1],
                         {"from": "PREPARED", "opcode": "SETTLE",
                          "to": "AWAITING_AUTHORIZATION"})
        self.assertEqual(first["action"]["quote_id"], "quote_B")
        self.assertEqual(first["action"]["total_cost"], "3")
        self.assertEqual(first["intent"]["expected"]["balance_after"], "797")
        self.assertEqual(first["intent"]["expected"]["exposure_after"], "300")
        self.assertTrue(all(item["pass"] for item in first["invariants"]))
        self.assertEqual((first["signature_status"], first["chain_status"]),
                         ("NOT_SIGNED", "NOT_SUBMITTED"))
        # ISA v1 remains byte-for-byte reproducible after a new ISA is introduced.
        self.assertEqual(first["receipt_hash"],
                         "bc2d07e53c2c131b1627bb522df6db7ca9685ee2d34d7bb6dfd61869282b33af")

    def test_guarded_isa_hold_and_pass_are_distinct_replayable_paths(self):
        program = json.loads((BASE / "economic_program_guarded_demo.json").read_text())
        first = self._receipt(program=program)
        self.assertEqual(first["schema_version"], "economic-receipt-3")
        self.assertEqual(first["terminal_state"], "AWAITING_AUTHORIZATION")
        self.assertTrue(first["trace"][1]["result"]["pass"])
        self.assertEqual(first["intent"]["execution_status"],
                         "SIMULATION_ONLY_NO_CHAIN_ADAPTER")
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        held = self._receipt(program=program, world=world)
        self.assertEqual(held, self._receipt(program=program, world=world))
        self.assertEqual((held["terminal_state"], held["reason_code"]),
                         ("HELD", "GUARD_FALSE:COVERAGE_BELOW_POLICY"))
        self.assertEqual(held["phase_trace"][-1],
                         {"from": "ASSERTING", "opcode": "HALT", "to": "HELD"})
        self.assertEqual([item["op"] for item in held["trace"]],
                         ["OBSERVE", "GUARD", "HALT"])
        self.assertFalse(held["trace"][1]["result"]["pass"])
        self.assertIsNone(held["intent"])
        self.assertIsNone(held["action"])

    def test_guarded_isa_rejects_undefined_or_late_branch_and_v1_guard(self):
        program = json.loads((BASE / "economic_program_guarded_demo.json").read_text())
        old = copy.deepcopy(program)
        old["schema_version"] = "econ-isa-1"
        with self.assertRaisesRegex(MachineError, "requires econ-isa-2"):
            compile_program(old)
        undefined = copy.deepcopy(program)
        undefined["instructions"][1]["register"] = "missing"
        with self.assertRaisesRegex(MachineError, "undefined register"):
            compile_program(undefined)
        late = copy.deepcopy(program)
        guard = late["instructions"].pop(1)
        late["instructions"].insert(4, guard)
        with self.assertRaisesRegex(MachineError, "transition grammar"):
            compile_program(late)
        malformed = copy.deepcopy(program)
        malformed["instructions"][1]["on_false"] = ["HOLD"]
        with self.assertRaisesRegex(MachineError, "bounded false branch"):
            compile_program(malformed)

    def test_guarded_runtime_wakes_only_on_relevant_state_change(self):
        program = json.loads((BASE / "economic_program_guarded_demo.json").read_text())
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "guarded.sqlite3")
            runtime.install_state(world)
            runtime.register_program(program)
            held = runtime.evaluate(program["program_id"], at=AT)
            self.assertEqual(held["terminal_state"], "HELD")
            self.assertEqual(runtime.status()["active_reservations"], 0)
            self.assertTrue(runtime.verify_receipt(held["receipt_hash"]))
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": world["owner_id"], "network": world["network"],
                     "sequence": 1, "previous_root": state_root(world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {"risk.coverage_ratio": {
                         **world["facts"]["risk.coverage_ratio"],
                         "value": "1.6", "observed_at": "2026-09-25T12:02:00+00:00"}},
                     "quote_updates": {},
                     "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}}}
            event = runtime.ingest(delta, event_id="coverage-recovered-1")
            self.assertEqual((event["kernel_calls"], event["llm_calls"]), (1, 0))
            self.assertEqual(runtime.status()["active_reservations"], 1)
            self.assertTrue(runtime.verify_receipt(event["receipt_hashes"][0]))

    def test_guard_false_escalation_and_bad_units_never_prepare(self):
        program = json.loads((BASE / "economic_program_guarded_demo.json").read_text())
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        program["instructions"][1]["on_false"] = "ESCALATE"
        escalated = self._receipt(program=program, world=world)
        self.assertEqual(escalated["terminal_state"], "ESCALATED")
        self.assertIsNone(escalated["intent"])
        world["facts"]["risk.coverage_ratio"]["unit"] = "percent"
        mismatched = self._receipt(program=program, world=world)
        self.assertEqual((mismatched["terminal_state"], mismatched["reason_code"]),
                         ("ESCALATED", "GUARD_UNIT_MISMATCH"))
        self.assertIsNone(mismatched["intent"])

    def test_graph_isa_selects_distinct_fully_verified_allocation_paths(self):
        program, world = self._graph_case()
        larger = self._receipt(program=program, world=world)
        self.assertEqual(larger["schema_version"], "economic-receipt-4")
        self.assertEqual(larger["terminal_state"], "AWAITING_AUTHORIZATION")
        self.assertEqual(larger["action"]["amount"], "200")
        self.assertEqual(larger["action"]["quote_id"], "quote_B")
        self.assertEqual(larger["trace"][1]["result"]["next_instruction"], 2)
        self.assertEqual([step["step"] for step in larger["trace"]], list(range(9)))
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        smaller = self._receipt(program=program, world=world)
        self.assertEqual(smaller, self._receipt(program=program, world=world))
        self.assertEqual(smaller["terminal_state"], "AWAITING_AUTHORIZATION")
        self.assertEqual(smaller["action"]["amount"], "100")
        self.assertEqual(smaller["action"]["quote_id"], "quote_C")
        self.assertEqual(smaller["intent"]["expected"]["balance_after"], "898")
        self.assertEqual(smaller["trace"][1]["result"]["next_instruction"], 9)
        self.assertEqual([step["step"] for step in smaller["trace"]],
                         [0, 1, 9, 10, 11, 12, 13, 14, 15])
        self.assertTrue(all(item["pass"] for item in smaller["invariants"]))

    def test_graph_static_checker_rejects_backward_unreachable_and_unverified_paths(self):
        program, world = self._graph_case()
        old = copy.deepcopy(program)
        old["schema_version"] = "econ-isa-2"
        with self.assertRaisesRegex(MachineError, "requires econ-isa-3"):
            compile_program(old)
        backward = copy.deepcopy(program)
        backward["instructions"][1]["if_false"] = 0
        with self.assertRaisesRegex(MachineError, "distinct forward"):
            compile_program(backward)
        boolean_target = copy.deepcopy(program)
        boolean_target["instructions"][1]["if_false"] = True
        with self.assertRaisesRegex(MachineError, "distinct forward"):
            compile_program(boolean_target)
        unreachable = copy.deepcopy(program)
        unreachable["instructions"].append({"op": "HOLD", "reason": "DEAD_CODE"})
        with self.assertRaisesRegex(MachineError, "unreachable"):
            compile_program(unreachable)
        unverified = copy.deepcopy(program)
        unverified["instructions"][13] = {"op": "PREPARE"}
        with self.assertRaises(MachineError):
            compile_program(unverified)
        # The two arms may use the same SSA register name because they are exclusive.
        self.assertEqual(compile_program(program)["schema_version"], "econ-isa-3")

    def test_graph_hold_branch_and_unit_disagreement_never_prepare(self):
        program, world = self._graph_case()
        program["instructions"] = program["instructions"][:9] + [
            {"op": "HOLD", "reason": "COVERAGE_BELOW_POLICY"}]
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        held = self._receipt(program=program, world=world)
        self.assertEqual((held["terminal_state"], held["reason_code"]),
                         ("HELD", "COVERAGE_BELOW_POLICY"))
        self.assertIsNone(held["intent"])
        self.assertEqual([step["step"] for step in held["trace"]], [0, 1, 9])
        world["facts"]["risk.coverage_ratio"]["unit"] = "percent"
        escalated = self._receipt(program=program, world=world)
        self.assertEqual((escalated["terminal_state"], escalated["reason_code"]),
                         ("ESCALATED", "BRANCH_UNIT_MISMATCH"))
        self.assertIsNone(escalated["intent"])

    def test_graph_runtime_replaces_large_plan_with_smaller_branch_on_event(self):
        program, world = self._graph_case()
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "graph.sqlite3")
            runtime.install_state(world)
            runtime.register_program(program)
            larger = runtime.evaluate(program["program_id"], at=AT)
            self.assertEqual(larger["action"]["amount"], "200")
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": world["owner_id"], "network": world["network"],
                     "sequence": 1, "previous_root": state_root(world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {"risk.coverage_ratio": {
                         **world["facts"]["risk.coverage_ratio"],
                         "value": "1.4", "observed_at": "2026-09-25T12:02:00+00:00"}},
                     "quote_updates": {},
                     "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}}}
            event = runtime.ingest(delta, event_id="coverage-regime-change-1")
            self.assertEqual((event["kernel_calls"], event["llm_calls"]), (1, 0))
            smaller_hash = event["receipt_hashes"][0]
            self.assertEqual(runtime.status()["active_reservations"], 1)
            self.assertEqual(runtime.intent_status(larger["receipt_hash"],
                                                   at="2026-09-25T12:02:00+00:00")
                             ["reservation_status"], "SUPERSEDED")
            self.assertTrue(runtime.intent_status(smaller_hash,
                                                  at="2026-09-25T12:02:00+00:00")
                            ["locally_pending"])
            self.assertTrue(runtime.verify_receipt(larger["receipt_hash"]))
            self.assertTrue(runtime.verify_receipt(smaller_hash))

    def test_nested_graph_has_three_verified_exits(self):
        program, world = self._graph_case()
        original = program["instructions"]
        program["instructions"] = [
            original[0],
            {"op": "OBSERVE", "path": "risk.volatility", "as": "volatility",
             "max_age_ms": 3600000},
            {"op": "BRANCH", "register": "coverage", "comparison": "GTE",
             "value": "1.5", "unit": "ratio", "if_true": 3, "if_false": 18},
            {"op": "BRANCH", "register": "volatility", "comparison": "LTE",
             "value": "0.2", "unit": "ratio", "if_true": 4, "if_false": 11},
            *original[2:9], *original[9:16],
            {"op": "HOLD", "reason": "COVERAGE_BELOW_POLICY"},
        ]
        world["facts"]["risk.volatility"] = {
            "value": "0.1", "unit": "ratio", "quality": "VALID",
            "source_id": "synthetic_volatility_fixture", "source_hash": "e" * 64,
            "observed_at": "2026-09-25T12:00:00+00:00"}
        self.assertEqual(self._receipt(program=program, world=world)["action"]["amount"],
                         "200")
        world["facts"]["risk.volatility"]["value"] = "0.3"
        self.assertEqual(self._receipt(program=program, world=world)["action"]["amount"],
                         "100")
        world["facts"]["risk.coverage_ratio"]["value"] = "1.4"
        held = self._receipt(program=program, world=world)
        self.assertEqual(held["terminal_state"], "HELD")
        self.assertIsNone(held["intent"])
        self.assertEqual([step["step"] for step in held["trace"]], [0, 1, 2, 18])

    def test_invariant_violation_aborts_without_intent(self):
        world = copy.deepcopy(self.world)
        world["balances"]["USDT"] = "100"
        result = self._receipt(world=world)
        self.assertEqual(result["terminal_state"], "ABORTED")
        self.assertIn("NONNEGATIVE_BALANCE", result["reason_code"])
        self.assertIsNone(result["intent"])

    def test_large_fixed_point_amount_does_not_round(self):
        program = copy.deepcopy(self.program)
        world = copy.deepcopy(self.world)
        amount = "1000000000000000000000000000000.000000001"
        world["balances"]["USDT"] = "1000000000000000000000000000004.000000001"
        program["sandbox"]["capital_limit"] = "1000000000000000000000000000200"
        program["sandbox"]["max_exposure"] = "1000000000000000000000000000200"
        for ins in program["instructions"]:
            if ins["op"] in {"QUOTE", "ALLOCATE"}:
                ins["amount"] = amount
        for quote in world["quotes"].values():
            quote["amount"] = amount
        result = self._receipt(program=program, world=world)
        self.assertEqual(result["terminal_state"], "AWAITING_AUTHORIZATION")
        self.assertEqual(result["intent"]["expected"]["balance_after"], "1")

    def test_unknown_or_stale_observation_escalates(self):
        world = copy.deepcopy(self.world)
        world["facts"]["risk.coverage_ratio"]["quality"] = "MISSING"
        world["facts"]["risk.coverage_ratio"]["value"] = None
        self.assertEqual(self._receipt(world=world)["terminal_state"], "ESCALATED")
        self.assertEqual(self._receipt(at="2026-09-25T14:00:00+00:00")["terminal_state"],
                         "ESCALATED")

    def test_price_oracle_checks_source_independence_and_dispersion(self):
        program = copy.deepcopy(self.program)
        program["instructions"].insert(1, {
            "op": "PRICE", "paths": ["price.a", "price.b"], "as": "median_price",
            "max_age_ms": 3600000, "max_dispersion_bps": 15})
        # PRICE belongs before ASSERT in this ISA.
        program["instructions"][0], program["instructions"][1] = (
            program["instructions"][1], program["instructions"][0])
        world = copy.deepcopy(self.world)
        for path, source, amount in (("price.a", "source_a", "1"),
                                     ("price.b", "source_b", "1.001")):
            world["facts"][path] = {"value": amount, "unit": "USDT_per_USDD",
                                    "quality": "VALID", "source_id": source,
                                    "source_hash": "e" * 64,
                                    "observed_at": "2026-09-25T12:00:00+00:00"}
        self.assertEqual(self._receipt(program=program, world=world)["terminal_state"],
                         "AWAITING_AUTHORIZATION")
        world["facts"]["price.b"]["source_id"] = "source_a"
        self.assertEqual(self._receipt(program=program, world=world)["reason_code"],
                         "PRICE_SOURCES_NOT_INDEPENDENT")
        world["facts"]["price.b"]["source_id"] = "source_b"
        world["facts"]["price.b"]["value"] = "1.1"
        self.assertEqual(self._receipt(program=program, world=world)["reason_code"],
                         "PRICE_DISPERSION")

    def test_static_analysis_rejects_unsupported_and_ambiguous_programs(self):
        program = copy.deepcopy(self.program)
        program["instructions"][4]["op"] = "HEDGE"
        with self.assertRaisesRegex(MachineError, "reserved opcode"):
            compile_program(program)
        program = copy.deepcopy(self.program)
        program["instructions"][4]["amount"] = "201"
        with self.assertRaisesRegex(MachineError, "quoted amount"):
            compile_program(program)
        program = copy.deepcopy(self.program)
        program["sandbox"]["capital_limit"] = 500.0
        with self.assertRaises(MachineError):
            compile_program(program)
        malformed = [
            (0, "op", []),
            (0, "as", []),
            (1, "register", []),
            (4, "route_register", {}),
        ]
        for index, field, value in malformed:
            program = copy.deepcopy(self.program)
            program["instructions"][index][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(MachineError):
                compile_program(program)

    def test_runtime_semantic_delta_idempotence_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            self.assertFalse(runtime.install_state(self.world)["idempotent"])
            self.assertTrue(runtime.install_state(self.world)["idempotent"])
            self.assertEqual(runtime.register_program(self.program)["version"], 1)
            initial = runtime.evaluate(self.program["program_id"], at=AT)
            self.assertTrue(runtime.verify_receipt(initial["receipt_hash"]))
            delta = {"schema_version": "econ-state-delta-1", "owner_id": self.world["owner_id"],
                     "network": self.world["network"],
                     "sequence": 1, "previous_root": state_root(self.world),
                     "as_of": "2026-09-25T12:02:00+00:00",
                     "evidence_hash": "a" * 64,
                     "quote_updates": {},
                     "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}},
                     "fact_updates": {"risk.coverage_ratio": {
                         **self.world["facts"]["risk.coverage_ratio"],
                         "observed_at": "2026-09-25T12:02:00+00:00",
                         "source_hash": "f" * 64}}}
            unchanged = runtime.ingest(delta, event_id="event-unchanged-1")
            self.assertEqual((unchanged["semantic_changes"], unchanged["kernel_calls"],
                              unchanged["llm_calls"]), ([], 0, 0))
            self.assertTrue(runtime.ingest(delta, event_id="event-unchanged-1")["idempotent"])
            with self.assertRaisesRegex(MachineError, "different input"):
                runtime.ingest({**delta, "sequence": 2}, event_id="event-unchanged-1")
            next_state = runtime.latest_state()
            changed = {**delta, "sequence": 2, "previous_root": state_root(next_state),
                       "as_of": "2026-09-25T12:03:00+00:00",
                       "fact_updates": {"risk.coverage_ratio": {
                           **next_state["facts"]["risk.coverage_ratio"],
                           "value": "1.4", "observed_at": "2026-09-25T12:03:00+00:00"}}}
            event = runtime.ingest(changed, event_id="event-changed-2")
            self.assertEqual((event["affected_programs"], event["kernel_calls"]),
                             ([self.program["program_id"]], 1))
            self.assertTrue(runtime.verify_receipt(event["receipt_hashes"][0]))
            self.assertEqual(runtime.status()["receipts"], 2)

    def test_program_revision_switches_active_version(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            first = runtime.register_program(self.program)
            self.assertTrue(runtime.register_program(self.program)["idempotent"])
            revision = copy.deepcopy(self.program)
            revision["sandbox"]["max_total_cost"] = "4"
            second = runtime.register_program(revision)
            self.assertEqual((first["version"], second["version"]), (1, 2))
            self.assertEqual(runtime.status()["active_programs"][0]["version"], 2)

    def test_pause_revokes_unsent_intent_and_blocks_every_local_wake_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "machine.sqlite3"
            runtime = MachineRuntime(path)
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            prepared = runtime.evaluate(self.program["program_id"], at=AT)
            receipt_hash = prepared["receipt_hash"]
            self.assertTrue(runtime.intent_status(receipt_hash, at=AT)["locally_pending"])
            paused = runtime.pause_program(self.program["program_id"],
                                           reason="MANUAL_RISK_STOP")
            self.assertEqual(paused["revoked_reservations"], [receipt_hash])
            self.assertEqual(paused["state"], "PAUSED")
            self.assertEqual(runtime.status()["active_reservations"], 0)
            self.assertTrue(runtime.status()["journal_integrity"])
            self.assertTrue(runtime.verify_receipt(receipt_hash))  # History survives revocation.
            snapshot = runtime.intent_status(receipt_hash, at=AT)
            self.assertEqual(snapshot["reservation_status"], "REVOKED")
            self.assertFalse(snapshot["locally_pending"])
            self.assertTrue(snapshot["historical_receipt_verified"])
            event_count = runtime.status()["events"]
            self.assertTrue(runtime.pause_program(self.program["program_id"],
                                                  reason="MANUAL_RISK_STOP")["idempotent"])
            self.assertEqual(runtime.status()["events"], event_count)
            with self.assertRaisesRegex(MachineError, "paused"):
                runtime.evaluate(self.program["program_id"], at=AT)
            runtime.resume_program(self.program["program_id"], reason="REVIEWED")
            with self.assertRaisesRegex(MachineError, "retired intent"):
                runtime.evaluate(self.program["program_id"], at=AT)
            runtime.pause_program(self.program["program_id"], reason="MANUAL_RISK_STOP")
            self.assertEqual(runtime.tick(at="2026-09-25T13:01:00+00:00")["kernel_calls"], 0)
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": self.world["owner_id"], "network": self.world["network"],
                     "sequence": 1, "previous_root": state_root(self.world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {"risk.coverage_ratio": {
                         **self.world["facts"]["risk.coverage_ratio"],
                         "value": "1.7", "observed_at": "2026-09-25T12:02:00+00:00"}},
                     "quote_updates": {},
                     "account_updates": {"balances": {}, "exposures": {}, "daily_losses": {}}}
            skipped = runtime.ingest(delta, event_id="paused-risk-change-1")
            self.assertEqual((skipped["affected_programs"], skipped["kernel_calls"]),
                             ([], 0))
            self.assertEqual(runtime.latest_state()["sequence"], 1)
            self.assertEqual(MachineRuntime(path).status()["active_programs"][0]["state"],
                             "PAUSED")
            runtime.resume_program(self.program["program_id"], reason="REVIEWED")
            fresh = runtime.evaluate(self.program["program_id"],
                                     at="2026-09-25T12:03:00+00:00")
            self.assertEqual(fresh["terminal_state"], "AWAITING_AUTHORIZATION")
            self.assertNotEqual(fresh["receipt_hash"], receipt_hash)
            self.assertTrue(runtime.intent_status(fresh["receipt_hash"],
                                                  at="2026-09-25T12:03:00+00:00")
                            ["locally_pending"])
            self.assertFalse(runtime.intent_status(receipt_hash,
                                                   at="2026-09-25T12:03:00+00:00")
                             ["locally_pending"])

    def test_paused_program_revision_stays_paused_until_explicit_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            runtime.register_program(self.program)
            runtime.pause_program(self.program["program_id"], reason="MANUAL_RISK_STOP")
            revision = copy.deepcopy(self.program)
            revision["sandbox"]["max_total_cost"] = "9"
            revised = runtime.register_program(revision)
            self.assertEqual((revised["version"], revised["state"]), (2, "PAUSED"))
            self.assertEqual(runtime.status()["active_programs"][0]["state"], "PAUSED")
            self.assertEqual(runtime.register_program(revision)["state"], "PAUSED")
            with self.assertRaisesRegex(MachineError, "paused"):
                runtime.evaluate(revision["program_id"], at=AT)
            runtime.resume_program(revision["program_id"], reason="REVIEWED")
            self.assertEqual(runtime.status()["active_programs"][0]["state"], "ACTIVE")
            self.assertTrue(runtime.status()["journal_integrity"])

    def test_resume_fails_closed_if_control_journal_is_tampered(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "machine.sqlite3"
            runtime = MachineRuntime(path)
            runtime.register_program(self.program)
            runtime.pause_program(self.program["program_id"], reason="MANUAL_RISK_STOP")
            with sqlite3.connect(path) as db:
                db.execute("UPDATE events SET event_json='{}' WHERE ordinal=1")
            with self.assertRaisesRegex(MachineError, "journal integrity failed"):
                runtime.resume_program(self.program["program_id"], reason="REVIEWED")
            self.assertEqual(runtime.status()["active_programs"][0]["state"], "PAUSED")

    def test_control_event_namespace_cannot_be_claimed_by_state_input(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            with self.assertRaisesRegex(MachineError, "reserved event id prefix"):
                runtime.ingest({}, event_id="program-control:123")

    def test_capital_reservation_prevents_concurrent_overallocation(self):
        with tempfile.TemporaryDirectory() as temp:
            world = copy.deepcopy(self.world)
            world["balances"]["USDT"] = "350"
            first_program = copy.deepcopy(self.program)
            first_program["sandbox"]["capital_limit"] = "1000"
            first_program["sandbox"]["max_exposure"] = "1000"
            second_program = copy.deepcopy(first_program)
            second_program["program_id"] = "treasury-demo-002"
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            runtime.install_state(world)
            runtime.register_program(first_program)
            runtime.register_program(second_program)
            first = runtime.evaluate(first_program["program_id"], at=AT)
            second = runtime.evaluate(second_program["program_id"], at=AT)
            self.assertEqual(first["terminal_state"], "AWAITING_AUTHORIZATION")
            self.assertEqual(second["terminal_state"], "ABORTED")
            self.assertIn("NONNEGATIVE_BALANCE", second["reason_code"])
            self.assertEqual(len(second["pending_reservations"]), 1)
            self.assertEqual(runtime.status()["active_reservations"], 1)
            self.assertTrue(runtime.verify_receipt(second["receipt_hash"]))
            self.assertEqual(runtime.evaluate(first_program["program_id"], at=AT)
                             ["receipt_hash"], first["receipt_hash"])
            self.assertEqual(runtime.status()["active_reservations"], 1)
            first_program["sandbox"]["max_total_cost"] = "9"
            runtime.register_program(first_program)  # Invalidates its pending intent.
            self.assertEqual(runtime.status()["active_reservations"], 0)
            self.assertEqual(runtime.evaluate(second_program["program_id"], at=AT)
                             ["terminal_state"], "AWAITING_AUTHORIZATION")

    def test_execution_lock_survives_expiry_pause_and_program_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            world = copy.deepcopy(self.world)
            world["balances"]["USDT"] = "350"
            first_program = copy.deepcopy(self.program)
            first_program["sandbox"]["capital_limit"] = "1000"
            first_program["sandbox"]["max_exposure"] = "1000"
            second_program = copy.deepcopy(first_program)
            second_program["program_id"] = "treasury-demo-002"
            runtime = MachineRuntime(Path(temp) / "locked.sqlite3")
            runtime.install_state(world)
            runtime.register_program(first_program)
            runtime.register_program(second_program)
            first = runtime.evaluate(first_program["program_id"], at=AT)
            lock = runtime.begin_execution(first["receipt_hash"], at=AT)
            self.assertEqual(lock["status"], "EXECUTION_LOCKED")
            self.assertEqual(lock["execution_authority"], "NONE")
            self.assertFalse(lock["idempotent"])
            self.assertEqual(runtime.begin_execution(first["receipt_hash"], at=AT)
                             ["event_hash"], lock["event_hash"])
            self.assertEqual(runtime.status()["capital_encumbered_reservations"], 1)
            self.assertEqual(runtime.status()["execution_locked_reservations"], 1)
            self.assertEqual(runtime.status()["active_reservations"], 1)
            self.assertEqual(runtime.status()["unsent_reservations"], 0)
            with self.assertRaisesRegex(MachineError, "unresolved execution lock"):
                runtime.evaluate(first_program["program_id"], at=AT)
            second = runtime.evaluate(second_program["program_id"], at=AT)
            self.assertEqual(second["terminal_state"], "ABORTED")
            self.assertEqual(second["pending_reservations"][0]["status"],
                             "EXECUTION_LOCKED")
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": world["owner_id"], "network": world["network"],
                     "sequence": 1, "previous_root": state_root(world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {}, "quote_updates": {},
                     "account_updates": {"balances": {"USDT": "340"},
                                         "exposures": {}, "daily_losses": {}}}
            event = runtime.ingest(delta, event_id="balance-after-lock-1")
            self.assertEqual(event["execution_locked_programs"],
                             [first_program["program_id"]])
            self.assertEqual(event["kernel_calls"], 1)
            first_program["sandbox"]["max_total_cost"] = "9"
            runtime.register_program(first_program)
            runtime.pause_program(first_program["program_id"], reason="OPERATOR_HALT")
            self.assertEqual(runtime.tick(at="2026-09-25T13:01:00+00:00")["kernel_calls"], 0)
            status = runtime.intent_status(first["receipt_hash"],
                                           at="2026-09-25T13:01:00+00:00")
            self.assertEqual(status["reservation_status"], "EXECUTION_LOCKED")
            self.assertTrue(status["capital_locked"])
            self.assertFalse(status["locally_pending"])
            self.assertEqual(runtime.status()["execution_locked_reservations"], 1)
            self.assertTrue(runtime.verify_receipt(first["receipt_hash"]))

    def test_execution_lock_projection_tampering_and_retired_intent_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "locked.sqlite3")
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            receipt = runtime.evaluate(self.program["program_id"], at=AT)
            with self.assertRaisesRegex(MachineError, "outside intent lifetime"):
                runtime.begin_execution(receipt["receipt_hash"],
                                        at="2026-09-25T13:00:00+00:00")
            runtime.begin_execution(receipt["receipt_hash"], at=AT)
            with sqlite3.connect(runtime.db_path) as db:
                db.execute("UPDATE reservations SET status='ACTIVE' WHERE reservation_id=?",
                           (receipt["receipt_hash"],))
            with self.assertRaisesRegex(MachineError, "projection differs from journal"):
                runtime.status()
            with self.assertRaisesRegex(MachineError, "projection differs from journal"):
                runtime.register_program(self.program)

    def test_execution_lock_is_serialized_and_cannot_start_after_state_change(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "serialized.sqlite3")
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            receipt = runtime.evaluate(self.program["program_id"], at=AT)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda _: runtime.begin_execution(receipt["receipt_hash"], at=AT),
                    range(2)))
            self.assertEqual(sorted(item["idempotent"] for item in results), [False, True])
            self.assertEqual(results[0]["event_hash"], results[1]["event_hash"])
            with sqlite3.connect(runtime.db_path) as db:
                count = db.execute("SELECT count(*) FROM events WHERE kind='EXECUTION_LOCK'").fetchone()[0]
            self.assertEqual(count, 1)
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "retired.sqlite3")
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            receipt = runtime.evaluate(self.program["program_id"], at=AT)
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": self.world["owner_id"], "network": self.world["network"],
                     "sequence": 1, "previous_root": state_root(self.world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {}, "quote_updates": {},
                     "account_updates": {"balances": {"USDT": "100"},
                                         "exposures": {}, "daily_losses": {}}}
            runtime.ingest(delta, event_id="balance-before-lock-1")
            with self.assertRaisesRegex(MachineError, "retired reservation"):
                runtime.begin_execution(receipt["receipt_hash"],
                                        at="2026-09-25T12:02:00+00:00")

    def test_execution_lock_cli_never_returns_signing_or_submission_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "operator.sqlite3"
            runtime = MachineRuntime(database)
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            receipt = runtime.evaluate(self.program["program_id"], at=AT)
            output = io.StringIO()
            with patch.object(sys, "argv", ["economic-machine", "begin-execution",
                                            "--db", str(database), "--receipt-hash",
                                            receipt["receipt_hash"], "--at", AT]):
                with contextlib.redirect_stdout(output):
                    main()
            locked = json.loads(output.getvalue())
            self.assertEqual(locked["status"], "EXECUTION_LOCKED")
            self.assertEqual(locked["execution_authority"], "NONE")
            self.assertNotIn("signed_transaction", locked)
            self.assertNotIn("txid", locked)
            self.assertTrue(runtime.intent_status(receipt["receipt_hash"], at=AT)
                            ["capital_locked"])

    def test_expired_execution_lock_still_counts_against_kernel_capital(self):
        compiled = compile_program(self.program)
        reservation = {"reservation_id": "a" * 64,
                       "program_id": "other-program", "owner_id": self.world["owner_id"],
                       "agent_id": "ALPHA", "network": self.world["network"],
                       "asset": "USDT", "amount": "900", "cost": "0",
                       "expires_at": "2026-09-25T12:00:00+00:00"}
        expired = EconomicKernel().evaluate(compiled, self.world, at=AT,
                                            pending_reservations=[reservation])
        self.assertEqual(expired["terminal_state"], "AWAITING_AUTHORIZATION")
        locked = EconomicKernel().evaluate(
            compiled, self.world, at=AT,
            pending_reservations=[{**reservation, "status": "EXECUTION_LOCKED"}])
        self.assertEqual(locked["terminal_state"], "ABORTED")
        self.assertIn("NONNEGATIVE_BALANCE", locked["reason_code"])

    def test_account_delta_rechecks_capital_and_invalidates_pending_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            first = runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(runtime.status()["active_reservations"], 1)
            delta = {"schema_version": "econ-state-delta-1",
                     "owner_id": self.world["owner_id"], "network": self.world["network"],
                     "sequence": 1, "previous_root": state_root(self.world),
                     "as_of": "2026-09-25T12:02:00+00:00", "evidence_hash": "f" * 64,
                     "fact_updates": {}, "quote_updates": {},
                     "account_updates": {"balances": {"USDT": "100"},
                                         "exposures": {}, "daily_losses": {}}}
            event = runtime.ingest(delta, event_id="account-change-1")
            self.assertEqual(event["semantic_changes"], ["balance:USDT"])
            self.assertEqual(event["kernel_calls"], 1)
            self.assertEqual(runtime.status()["active_reservations"], 0)
            self.assertTrue(runtime.verify_receipt(first["receipt_hash"]))
            self.assertTrue(runtime.verify_receipt(event["receipt_hashes"][0]))

    def test_concurrent_programs_serialize_capital_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            world = copy.deepcopy(self.world)
            world["balances"]["USDT"] = "350"
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            runtime.install_state(world)
            names = []
            for suffix in ("A", "B"):
                program = copy.deepcopy(self.program)
                program["program_id"] = "concurrent-" + suffix
                program["sandbox"]["capital_limit"] = "1000"
                program["sandbox"]["max_exposure"] = "1000"
                runtime.register_program(program)
                names.append(program["program_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(lambda name: runtime.evaluate(name, at=AT), names))
            self.assertEqual(sorted(receipt["terminal_state"] for receipt in receipts),
                             ["ABORTED", "AWAITING_AUTHORIZATION"])
            self.assertEqual(runtime.status()["active_reservations"], 1)
            self.assertTrue(all(runtime.verify_receipt(row["receipt_hash"]) for row in receipts))

    def test_journal_tamper_is_detected_by_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "machine.sqlite3"
            runtime = MachineRuntime(path)
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            receipt = runtime.evaluate(self.program["program_id"], at=AT)
            self.assertTrue(runtime.status()["journal_integrity"])
            with sqlite3.connect(path) as db:
                db.execute("UPDATE events SET event_json='{}' WHERE ordinal=1")
            self.assertFalse(runtime.status()["journal_integrity"])
            self.assertFalse(runtime.verify_receipt(receipt["receipt_hash"]))

    def test_clock_tick_expires_unsent_intent_without_polling_kernel(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MachineRuntime(Path(temp) / "machine.sqlite3")
            runtime.install_state(self.world)
            runtime.register_program(self.program)
            runtime.evaluate(self.program["program_id"], at=AT)
            self.assertEqual(runtime.tick(at="2026-09-25T12:30:00+00:00")
                             ["kernel_calls"], 0)
            due = runtime.tick(at="2026-09-25T13:01:00+00:00")
            self.assertEqual((len(due["due_programs"]), due["kernel_calls"],
                              due["llm_calls"]), (1, 1, 0))
            self.assertEqual(runtime.status()["active_reservations"], 0)
            self.assertTrue(runtime.verify_receipt(due["receipt_hashes"][0]))
            self.assertEqual(runtime.tick(at="2026-09-25T13:02:00+00:00")
                             ["kernel_calls"], 0)

    def test_settlement_bus_requires_verification_and_postcondition(self):
        receipt = self._receipt()
        intent = receipt["intent"]
        authorization = {"owner_id": "demo-owner", "intent_hash": intent["intent_hash"],
                         "signed_at": AT, "signature_ref": "fixture_signature"}
        proof = {"network": intent["network"], "intent_hash": intent["intent_hash"],
                 "txid": "fixture_tx", "submitted_at": AT,
                 "finalized_at": "2026-09-25T12:02:00+00:00", "block_height": 1,
                 "actual_post_state": {key: intent["expected"][key] for key in
                                       ("balance_after", "exposure_after", "daily_loss_after")},
                 "raw_receipt_hash": "d" * 64}
        with self.assertRaisesRegex(MachineError, "verifier required"):
            settle(receipt, authorization, proof, None)
        self.assertEqual(settle(receipt, authorization, proof, FixtureVerifier())
                         ["postcondition"], "SETTLED")
        proof["actual_post_state"]["balance_after"] = "796"
        self.assertEqual(settle(receipt, authorization, proof, FixtureVerifier())
                         ["postcondition"], "POSTCONDITION_FAILED")
        bad_authorization = {**authorization, "signature_ref": "invalid"}
        with self.assertRaisesRegex(MachineError, "not verified"):
            settle(receipt, bad_authorization, proof, FixtureVerifier())


if __name__ == "__main__":
    unittest.main()
