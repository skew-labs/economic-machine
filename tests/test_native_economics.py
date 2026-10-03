"""Actual remote C++/Python/API boundaries and financial counterfactuals."""

import hashlib
import json
import os
import random
import tempfile
import unittest
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from economic_machine.journal import verify_journal
from economic_machine.values import MachineError
from machine_engine.api import create_engine_app
from machine_engine.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
SCALE = 1000000


def frame():
    return {"sequence": 1, "expected_sequence": 1, "observed_ns": 100, "now_ns": 200,
            "max_age_ns": 1000, "deadline_ns": 1000, "policy_version": 1, "expected_policy_version": 1}


def position():
    return {"instrument": 1, "signed_quantity": 10 * SCALE, "entry_price": 100 * SCALE,
            "mark_price": 100 * SCALE, "collateral": 100 * SCALE, "accrued_funding": 0,
            "unpaid_fees": 0, "initial_margin_rate": 100000, "maintenance_margin_rate": 50000}


class NativeEconomics(unittest.TestCase):
    def setUp(self):
        report = json.loads((ROOT / "artifacts/atlas-release/economics-build.json").read_text())
        library = Path(report["immutable_library"])
        if not library.is_file():
            self.fail("Remote native build required; a skipped native test is not release evidence")
        self.assertEqual(hashlib.sha256(library.read_bytes()).hexdigest(), report["library_sha256"])
        self.env = patch.dict(os.environ, {"ENGINE_ECONOMICS_LIBRARY": str(library),
                                           "ENGINE_ECONOMICS_SHA256": report["library_sha256"]})
        self.env.start()
        self.addCleanup(self.env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Workspace(Path(temporary.name) / "work.sqlite3", clock=lambda: 1800000000)

    def evaluate(self, operation, body):
        return self.work.economics.evaluate({"operation": operation, "input": body})

    def test_every_domain_abi_size_is_checked_when_loaded(self):
        status = self.work.economics.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(len(status["operations"]), 20)
        self.assertFalse(status["general_intelligence"])
        self.assertEqual(status["language_model_calls"], 0)
        self.assertEqual(self.work.overview()["economics"], status)

    def test_recovery_changes_when_one_financial_condition_changes(self):
        body = {"position": position(), "policy": {"minimum_equity": 10 * SCALE,
                "target_margin_headroom": 70 * SCALE, "maximum_leverage": 6 * SCALE,
                "maximum_close_quantity": 10 * SCALE, "maximum_total_cost": 10 * SCALE,
                "max_slippage_bps": 100, "maximum_close_fraction": SCALE,
                "maximum_lots_examined": 20, "deadline_ns": 900},
                "venues": [{"id": 1, "executable_price": 100 * SCALE, "available_quantity": 10 * SCALE,
                    "quantity_step": SCALE, "fee_rate": 1000, "fixed_cost": 0,
                    "min_notional": SCALE, "allowed": True, "envelope": frame()}], "count": 1, "state": frame()}
        first = self.evaluate("DERIVATIVE_RECOVERY", body)
        self.assertEqual(first["action"], "REDUCE")
        self.assertEqual(first["result"]["close_quantity"], 5 * SCALE)
        body["position"]["collateral"] = 200 * SCALE
        second = self.evaluate("DERIVATIVE_RECOVERY", body)
        self.assertEqual(second["action"], "HOLD")
        self.assertEqual(second["result"]["close_quantity"], 0)
        self.assertEqual(first["execution_authority"], "NONE")
        self.assertEqual(second["execution_authority"], "NONE")

    def test_invalid_future_and_exhausted_search_abstain(self):
        body = {"terms": {"current_expected_income": SCALE, "proposed_expected_income": 4 * SCALE,
            "trading_cost": 0, "exit_cost": 0, "settlement_cost": 0, "model_uncertainty_buffer": 0,
            "minimum_improvement": 0, "last_rebalance_ns": 0, "cooldown_ns": 0}, "state": frame()}
        body["state"]["observed_ns"] = 201
        value = self.evaluate("REBALANCE_DECISION", body)
        self.assertFalse(value["computed"])
        self.assertIsNone(value["result"])
        self.assertEqual(value["reason"], "FUTURE")

    def test_ctypes_rejects_float_bool_as_number_and_unknown_keys(self):
        base = {"signed_quantity": SCALE, "settlement_price": 100 * SCALE, "funding_rate": 1000}
        for value in (1.5, True, str(SCALE), 2**63):
            with self.assertRaises(MachineError):
                self.evaluate("FUNDING_CASHFLOW", base | {"signed_quantity": value})
        with self.assertRaises(MachineError):
            self.evaluate("FUNDING_CASHFLOW", base | {"owner_signature": "not-a-capability"})

    def test_sign_and_rounding_match_independent_decimal_reference(self):
        randomizer = random.Random(98003)
        for _ in range(100):
            quantity = randomizer.randint(-100000000, 100000000)
            price = randomizer.randint(1, 1000000000)
            rate = randomizer.randint(-10000, 10000)
            actual = self.evaluate("FUNDING_CASHFLOW", {
                "signed_quantity": quantity, "settlement_price": price, "funding_rate": rate})
            notional = int((Decimal(abs(quantity)) * price / SCALE).to_integral_value(rounding=ROUND_CEILING))
            signed_rate = -rate if quantity < 0 else rate
            payment = int((Decimal(notional) * signed_rate / SCALE).to_integral_value(rounding=ROUND_CEILING))
            self.assertTrue(actual["computed"])
            self.assertEqual(actual["result"]["signed_payment"], payment)

    def test_journal_persists_actual_native_result_without_creating_order(self):
        value = self.evaluate("DERIVATIVE_RISK", position())
        self.assertTrue(value["computed"])
        with self.work.runtime.connect() as db:
            self.assertTrue(verify_journal(db))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM engine_trade_orders").fetchone()[0], 0)
            rows = db.execute("SELECT * FROM events").fetchall()
            self.assertTrue(rows)
        self.assertEqual(value["input_assurance"], "CALLER_SUPPLIED_ASSUMPTIONS_NOT_AUTHENTICATED_MARKET_STATE")

    def test_same_http_engine_reads_catalogue_and_calls_actual_cpp(self):
        app = create_engine_app(self.work.runtime.db_path, workspace=self.work,
            admin_token="disposable-economic-test-owner-token")
        client = TestClient(app)
        self.assertEqual(client.get("/api/engine/economics").status_code, 401)
        headers = {"Authorization": "Bearer disposable-economic-test-owner-token"}
        catalogue = client.get("/api/engine/economics", headers=headers)
        self.assertEqual(catalogue.status_code, 200)
        self.assertIn("DERIVATIVE_RECOVERY", catalogue.json()["schemas"])
        response = client.post("/api/engine/economics/evaluate", headers=headers,
            json={"operation": "DERIVATIVE_RISK", "input": position()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["notional"], 1000 * SCALE)

    def test_named_agent_calls_same_native_library_with_same_receipt(self):
        connection = self.work.connect({"name": "Owned data", "profile": "json-data",
            "config": {"url": "https://example.com/data", "api_key_env": "OWNED_DATA_KEY"}})
        policy = self.work.control.policy({"name": "DecisionPolicy", "connection_ids": [connection["id"]],
            "venue_policy_ids": [], "allowed_operations": ["ECONOMIC_DECISION"], "turnover_limit_usdt": "0",
            "max_order_usdt": "0", "max_active_runs": 1, "expires_at": 1800003600})
        agent = self.work.control.register({"name": "VAULT", "role": "Treasury", "policy_id": policy["id"],
            "connection_ids": [connection["id"]], "operations": ["ECONOMIC_DECISION"]})
        request = {"request_id": "native-economic-run-one", "operation": "ECONOMIC_DECISION",
                   "connection_id": connection["id"], "payload": {"operation": "DERIVATIVE_RISK", "input": position()}}
        result = self.work.control.run(agent["id"], request)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["result"]["result"]["notional"], 1000 * SCALE)
        replay = self.work.control.run(agent["id"], request)
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(replay["result"]["receipt_sha256"], result["result"]["receipt_sha256"])
        self.assertEqual(result["held_usdt"], "0")

    def test_native_library_checksum_fail_closed(self):
        from machine_engine.economics import EconomicLibrary
        report = json.loads((ROOT / "artifacts/atlas-release/economics-build.json").read_text())
        with self.assertRaisesRegex(MachineError, "ECONOMIC_LIBRARY_HASH_MISMATCH"):
            EconomicLibrary(self.work, report["immutable_library"], "0" * 64)

    def test_compiled_economic_program_runs_through_same_workspace(self):
        from machine_engine.economic_examples import strategy_example
        example = strategy_example()
        result = self.work.economics.evaluate(example)
        self.assertTrue(result["computed"])
        self.assertEqual(result["action"], "REDUCE")
        self.assertEqual(result["result"]["amount"], 5000000)
        example["input"]["frame"]["dependencies"]["facts"][0]["value"] = 200000000
        self.assertEqual(self.work.economics.evaluate(example)["action"], "HOLD")
        example["input"]["program"]["instructions"][3]["true_target"] = 2
        invalid = self.work.economics.evaluate(example)
        self.assertFalse(invalid["computed"])
        self.assertEqual(invalid["reason"], "INVALID")
        self.assertIsNone(invalid["result"])

    def test_state_frame_is_typed_and_expires_in_native_boundary(self):
        from machine_engine.economic_examples import strategy_example
        fact = strategy_example()["input"]["frame"]["dependencies"]["facts"][0]
        request = {"deltas": [{"event": 1, "fingerprint": 101, "operation": 0, "fact": fact}],
            "dependencies": [{"key": fact["key"], "unit": fact["unit"], "asset": fact["asset"], "quote_asset": 0}],
            "delta_count": 1, "dependency_count": 1, "now_ns": 200, "maximum_skew_ns": 0}
        result = self.evaluate("STATE_FRAME", request)
        self.assertEqual(result["result"]["facts"][0]["value"], fact["value"])
        request["now_ns"] = 600
        self.assertEqual(self.evaluate("STATE_FRAME", request)["reason"], "STALE")
        request["now_ns"] = 200
        request["dependencies"][0]["asset"] = 99
        self.assertEqual(self.evaluate("STATE_FRAME", request)["reason"], "DOMAIN")

    def test_public_program_has_no_workspace_or_payment_authority(self):
        from machine_commerce.portal import create_portal
        from machine_engine.economic_examples import strategy_example
        with TestClient(create_portal()) as client:
            request = client.get("/demo/economics/example").json()["example"]
            self.assertEqual(request, strategy_example())
            response = client.post("/demo/economics/evaluate", json=request)
            self.assertEqual(response.status_code, 200)
            receipt = response.json()
            self.assertEqual(receipt["action"], "REDUCE")
            self.assertEqual(receipt["demonstration"], "SYNTHETIC_ASSUMPTIONS_NOT_CUSTOMER_ACCOUNT")
            from economic_machine.values import digest
            self.assertEqual(receipt.pop("receipt_sha256"), digest(receipt))
            self.assertEqual(client.post("/demo/economics/evaluate", json={
                "operation": "STATE_TRANSITION", "input": {}}).status_code, 400)
            self.assertEqual(client.post("/demo/economics/evaluate", content=b"x" * 32001).status_code, 413)

    def test_native_transaction_compiler_requires_funding_and_terminal_target(self):
        from machine_engine.economic_examples import execution_example
        example = execution_example()
        result = self.work.economics.evaluate(example)
        self.assertTrue(result["computed"])
        self.assertEqual(result["result"]["order"][:2], [1, 0])
        self.assertEqual(result["result"]["assets_after"][0]["available"], 60000000)
        self.assertEqual(result["execution_authority"], "NONE")
        example["input"]["steps"][0]["dependencies"] = 0
        example["input"]["steps"][0]["id"] = 1
        self.assertEqual(self.work.economics.evaluate(example)["reason"], "ILLIQUID")
        example = execution_example()
        example["input"]["targets"][1]["minimum_reserved"] = 21000000
        example["input"]["targets"][1]["maximum_reserved"] = 21000000
        self.assertEqual(self.work.economics.evaluate(example)["reason"], "INFEASIBLE")

    def test_browser_preserves_full_width_integer_receipts(self):
        value = 2**63 - 1
        result = self.evaluate("FUNDING_CASHFLOW", {"signed_quantity": {"integer": str(value)},
            "settlement_price": SCALE, "funding_rate": 0})
        self.assertTrue(result["computed"])
        self.assertEqual(result["result"]["notional"], {"integer": str(value)})
        for literal in ("01", "-0", "1.5", "1e6", str(2**63)):
            with self.subTest(literal=literal), self.assertRaises(MachineError):
                self.evaluate("FUNDING_CASHFLOW", {"signed_quantity": {"integer": literal},
                    "settlement_price": SCALE, "funding_rate": 0})

    def test_hosted_native_routes_keep_key_and_agent_scope_boundaries(self):
        from machine_commerce.access import Access, Principal
        reader = Principal("owner-workspace", "read-key", frozenset({"engine:read"}))
        writer = Principal("owner-workspace", "write-key", frozenset({"engine:write"}))
        agent = Principal("owner-workspace", "agent-key", frozenset({"agents:run"}), engine_agent_id="vault")
        Access.authorize(reader, "GET", "/api/engine/economics")
        Access.authorize(writer, "POST", "/api/engine/economics/evaluate")
        Access.authorize(agent, "POST", "/api/engine/agents/vault/runs")
        for principal, method, path in (
                (reader, "POST", "/api/engine/economics/evaluate"),
                (agent, "POST", "/api/engine/economics/evaluate"),
                (agent, "POST", "/api/engine/agents/other/runs"),
                (writer, "POST", "/api/engine/trade/orders/order/approve"),
                (writer, "POST", "/api/engine/control-policies")):
            with self.subTest(path=path, key=principal.key_id), self.assertRaises(PermissionError):
                Access.authorize(principal, method, path)


if __name__ == "__main__":
    unittest.main()
