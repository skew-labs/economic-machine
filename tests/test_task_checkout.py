"""New PR2 payment boundaries; fixtures are NOT real PayPal transactions."""

import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from test_engine_workspace import OWNER_TOKEN
from test_tasks import brief

from economic_machine.journal import verify_journal
from economic_machine.values import MachineError, canonical
from machine_commerce.api import create_app
from machine_engine.api import create_engine_app
from machine_engine.paypal import PayPalSandbox, bound_order
from machine_engine.task_checkout import clean_csv
from machine_engine.workspace import Workspace

CONFIG = {
    "merchant_id": "TESTMERCHANT123",
    "client_id_env": "SKEW_PAYPAL_CLIENT_ID",
    "client_secret_env": "SKEW_PAYPAL_CLIENT_SECRET",
    "public_origin": "https://machine.example.com",
    "services": [
        {
            "sku": "business-csv-cleanup",
            "version": 1,
            "kind": "data_cleanup",
            "worker": "csv_cleanup_v1",
            "price_cents": 100,
            "name": "Business CSV cleanup",
        }
    ],
}


class FakePayPal(PayPalSandbox):
    def __init__(self):
        super().__init__(copy.deepcopy(CONFIG))
        self.creates, self.captures, self.reads = [], [], []
        self.order = None
        self.create_lost = self.capture_lost = self.read_lost = False
        self.finish_capture = True

    def credentials_available(self):
        return True

    def create(self, plan, request_id):
        self.creates.append(request_id)
        self.order = {
            "id": "TESTORDER12345",
            "intent": "CAPTURE",
            "status": "CREATED",
            "purchase_units": [
                {
                    "reference_id": plan["id"],
                    "custom_id": plan["hash"],
                    "payee": {"merchant_id": self.merchant},
                    "amount": {"currency_code": "USD", "value": plan["amount"]},
                }
            ],
        }
        if self.create_lost:
            raise RuntimeError("lost network response")
        return copy.deepcopy(self.order)

    def read(self, oid):
        self.reads.append(oid)
        if self.read_lost:
            raise RuntimeError("lost read response")
        return copy.deepcopy(self.order)

    def capture(self, oid, request_id):
        self.captures.append(request_id)
        if self.finish_capture:
            self.order["status"] = "COMPLETED"
            self.order["purchase_units"][0]["payments"] = {
                "captures": [
                    {
                        "id": "TESTCAPTURE12345",
                        "status": "COMPLETED",
                        "final_capture": True,
                        "amount": self.order["purchase_units"][0]["amount"],
                    }
                ]
            }
        if self.capture_lost:
            raise RuntimeError("lost capture response")
        return copy.deepcopy(self.order)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "work.db"
        self.at = 1790900000
        self.paypal = FakePayPal()
        self.work = Workspace(self.path, clock=lambda: self.at, task_provider=self.paypal)
        self.task = self.work.tasks.create(
            brief(
                kind="data_cleanup",
                constraints={"required_fields": ["name", "email"], "output_format": "json"},
            )
        )
        self.raw = {
            "request_id": "purchase-one",
            "expected_revision": 1,
            "sku": "business-csv-cleanup",
            "input": {"csv": "name,email\n  Alice , alice@example.test \nBob,bob@example.test\n"},
        }
        self.purchase = self.work.task_checkout.plan(self.task["id"], self.raw)
        self.pid = self.purchase["id"]
        self.approval = {"plan_hash": self.purchase["plan"]["hash"]}

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self):
        return self.work.task_checkout.approve(self.pid, self.approval)

    def pay(self):
        self.approve()
        self.paypal.order["status"] = "APPROVED"
        return self.work.task_checkout.capture(self.pid, self.approval)

    def test_complete_purchase_entitlement_download_and_journal(self):
        self.assertEqual(self.pay()["status"], "PAID")
        result = self.work.task_checkout.fulfill(self.pid)
        self.assertEqual(result["status"], "DELIVERED")
        delivery = self.work.task_checkout.delivery(self.pid)
        self.assertEqual(json.loads(delivery["content"])[0], {"name": "Alice", "email": "alice@example.test"})
        self.assertFalse(delivery["data_truth_verified"])
        self.assertEqual(len(self.paypal.captures), 1)
        self.assertGreaterEqual(len(self.paypal.reads), 2)
        with self.work.runtime.connect() as db:
            self.assertTrue(verify_journal(db))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_replay_plan_approval_capture_and_fulfillment(self):
        self.assertEqual(self.work.task_checkout.plan(self.task["id"], self.raw), self.purchase)
        self.pay()
        for _ in range(3):
            self.approve()
            self.work.task_checkout.capture(self.pid, self.approval)
            self.work.task_checkout.fulfill(self.pid)
        self.assertEqual(len(self.paypal.creates), 1)
        self.assertEqual(len(self.paypal.captures), 1)

    def test_request_id_reuse_with_different_input_rejected(self):
        raw = self.raw | {"input": {"csv": "name,email\nOther,other@test\n"}}
        with self.assertRaisesRegex(MachineError, "CONFLICT"):
            self.work.task_checkout.plan(self.task["id"], raw)

    def test_second_plan_cannot_spend_same_task_budget(self):
        with self.assertRaisesRegex(MachineError, "ALREADY_HAS_PURCHASE"):
            self.work.task_checkout.plan(self.task["id"], self.raw | {"request_id": "another"})

    def test_price_above_budget_rejected(self):
        task = self.work.tasks.create(
            brief(
                "small",
                kind="data_cleanup",
                budget={"currency": "USD", "maximum": "0.50"},
                constraints={"required_fields": ["name", "email"], "output_format": "json"},
            )
        )
        with self.assertRaisesRegex(MachineError, "EXCEEDS_TASK_BUDGET"):
            self.work.task_checkout.plan(task["id"], self.raw | {"request_id": "small-purchase"})

    def test_no_delivery_before_completed_capture(self):
        self.approve()
        self.paypal.order["status"] = "APPROVED"
        self.assertFalse(self.work.task_checkout.reconcile(self.pid)["entitled"])
        with self.assertRaisesRegex(MachineError, "VERIFIED_PAYMENT"):
            self.work.task_checkout.fulfill(self.pid)
        with self.assertRaisesRegex(MachineError, "ENTITLEMENT"):
            self.work.task_checkout.delivery(self.pid)

    def test_redirect_and_created_order_are_not_approval(self):
        self.approve()
        with self.assertRaisesRegex(MachineError, "BUYER_APPROVAL"):
            self.work.task_checkout.capture(self.pid, self.approval)
        self.assertEqual(self.paypal.captures, [])

    def test_missing_capture_response_reconciles_without_second_charge(self):
        self.paypal.capture_lost = True
        self.assertEqual(self.pay()["status"], "PAID")
        self.work.task_checkout.capture(self.pid, self.approval)
        self.assertEqual(len(self.paypal.captures), 1)

    def test_unknown_capture_holds_task_and_does_not_retry(self):
        self.paypal.finish_capture = False
        self.paypal.capture_lost = True
        self.assertEqual(self.pay()["status"], "CAPTURE_UNKNOWN")
        self.work.task_checkout.capture(self.pid, self.approval)
        with self.assertRaisesRegex(MachineError, "IN_FLIGHT_OR_PAID"):
            self.work.tasks.cancel(self.task["id"], {"request_id": "cancel", "expected_revision": 1})
        self.assertEqual(len(self.paypal.captures), 1)
        self.assertFalse(self.work.task_checkout.get(self.pid)["entitled"])

    def test_unknown_create_holds_task_and_does_not_retry(self):
        self.paypal.create_lost = True
        self.assertEqual(self.approve()["status"], "CREATE_UNKNOWN")
        self.approve()
        self.assertEqual(len(self.paypal.creates), 1)

    def test_restart_after_capture_intent_uses_get_only(self):
        self.approve()
        self.paypal.capture(self.paypal.order["id"], "simulated-pre-crash-key")
        with self.work.runtime.connect() as db:
            db.execute("UPDATE engine_task_purchases SET status='CAPTURING' WHERE id=?", (self.pid,))
        restarted = Workspace(self.path, clock=lambda: self.at, task_provider=self.paypal)
        self.assertEqual(restarted.task_checkout.reconcile(self.pid)["status"], "PAID")
        self.assertEqual(len(self.paypal.captures), 1)

    def test_concurrent_approval_creates_one_order(self):
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: self.approve(), range(2)))
        self.assertEqual(len(self.paypal.creates), 1)

    def test_concurrent_capture_creates_one_capture(self):
        self.approve()
        self.paypal.order["status"] = "APPROVED"
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: self.work.task_checkout.capture(self.pid, self.approval), range(2)))
        self.assertEqual(len(self.paypal.captures), 1)

    def test_concurrent_fulfillment_has_one_result_event(self):
        self.pay()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: self.work.task_checkout.fulfill(self.pid), range(2)))
        with self.work.runtime.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM events WHERE kind='TASK_PURCHASE_DELIVERED'").fetchone()[0],
                1,
            )

    def test_expired_plan_and_wrong_hash_rejected(self):
        with self.assertRaisesRegex(MachineError, "INVALID"):
            self.work.task_checkout.approve(self.pid, {"plan_hash": "wrong"})
        self.at += 901
        with self.assertRaisesRegex(MachineError, "INVALID"):
            self.approve()
        self.assertEqual(self.paypal.creates, [])

    def test_service_repricing_requires_new_approval(self):
        self.paypal.services["business-csv-cleanup"]["price_cents"] = 200
        with self.assertRaisesRegex(MachineError, "INVALID"):
            self.approve()

    def test_edit_unsent_revokes_plan_and_paid_task_cannot_be_edited(self):
        self.work.tasks.revise(
            self.task["id"],
            {
                "request_id": "revise",
                "expected_revision": 1,
                "draft": {
                    k: v
                    for k, v in brief(
                        kind="data_cleanup",
                        constraints={"required_fields": ["name", "email"], "output_format": "json"},
                    ).items()
                    if k != "request_id"
                },
            },
        )
        self.assertEqual(self.work.task_checkout.get(self.pid)["status"], "REVOKED")
        self.assertEqual(self.approve()["status"], "REVOKED")
        purchase = self.work.task_checkout.plan(
            self.task["id"], self.raw | {"request_id": "revised", "expected_revision": 2}
        )
        self.pid, self.approval = purchase["id"], {"plan_hash": purchase["plan"]["hash"]}
        self.pay()
        with self.assertRaisesRegex(MachineError, "IN_FLIGHT_OR_PAID"):
            self.work.tasks.cancel(self.task["id"], {"request_id": "cancel-paid", "expected_revision": 2})

    def test_public_metadata_does_not_expose_input(self):
        text = json.dumps(self.work.task_checkout.status())
        self.assertNotIn("alice@example.test", text)
        self.assertNotIn("client_secret", text)
        self.assertNotIn("create_key", text)

    def test_plan_and_delivery_hash_tampering_rejected(self):
        self.pay()
        self.work.task_checkout.fulfill(self.pid)
        with self.work.runtime.connect() as db:
            db.execute("UPDATE engine_task_purchases SET result='{}' WHERE id=?", (self.pid,))
        with self.assertRaisesRegex(MachineError, "INTEGRITY"):
            self.work.task_checkout.delivery(self.pid)
        with self.work.runtime.connect() as db:
            plan = json.loads(
                db.execute("SELECT plan FROM engine_task_purchases WHERE id=?", (self.pid,)).fetchone()[0]
            )
            plan["amount"] = "0.01"
            db.execute(
                "UPDATE engine_task_purchases SET plan=? WHERE id=?", (canonical(plan).decode(), self.pid)
            )
        with self.assertRaisesRegex(MachineError, "INTEGRITY"):
            self.work.task_checkout.get(self.pid)

    def test_wrong_currency_payee_price_reference_and_partial_captures_rejected(self):
        self.pay()
        with self.work.runtime.connect() as db:
            plan = self.work.task_checkout._plan(db.execute("SELECT * FROM engine_task_purchases").fetchone())
        changes = [
            lambda o: o["purchase_units"][0]["payee"].update(merchant_id="OTHERSELLER123"),
            lambda o: o["purchase_units"][0]["amount"].update(value="0.01"),
            lambda o: o["purchase_units"][0]["amount"].update(currency_code="EUR"),
            lambda o: o["purchase_units"][0].update(custom_id="other-plan"),
            lambda o: o["purchase_units"][0]["payments"]["captures"][0].update(final_capture=False),
            lambda o: o["purchase_units"][0]["payments"]["captures"][0].update(status="PENDING"),
            lambda o: o["purchase_units"].append(copy.deepcopy(o["purchase_units"][0])),
            lambda o: o["purchase_units"][0]["payments"].update(refunds=[{"id": "REFUND12345"}]),
        ]
        for change in changes:
            with self.subTest(change=change):
                order = copy.deepcopy(self.paypal.order)
                change(order)
                with self.assertRaises(MachineError):
                    bound_order(order, plan, self.paypal.merchant, self.paypal.order["id"], paid=True)

    def test_unknown_wrong_payment_cannot_grant_entitlement(self):
        self.paypal.finish_capture = False
        self.pay()
        self.paypal.capture(self.paypal.order["id"], "external")
        self.paypal.order["purchase_units"][0]["payee"]["merchant_id"] = "OTHERSELLER123"
        self.assertFalse(self.work.task_checkout.reconcile(self.pid)["entitled"])

    def test_cross_workspace_cannot_read_or_capture_purchase(self):
        other = Workspace(Path(self.tmp.name) / "other.db", task_provider=self.paypal)
        with self.assertRaisesRegex(MachineError, "NOT_FOUND"):
            other.task_checkout.get(self.pid)
        with self.assertRaisesRegex(MachineError, "NOT_FOUND"):
            other.task_checkout.capture(self.pid, self.approval)

    def test_owner_routes_complete_and_download_no_store(self):
        app = create_engine_app(self.path, workspace=self.work, admin_token=OWNER_TOKEN)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + OWNER_TOKEN
            path = "/api/engine/task-purchases/" + self.pid
            self.assertEqual(client.post(path + "/approve", json=self.approval).status_code, 200)
            self.paypal.order["status"] = "APPROVED"
            self.assertEqual(client.post(path + "/capture", json=self.approval).json()["status"], "PAID")
            self.assertEqual(client.post(path + "/fulfill", json={}).json()["status"], "DELIVERED")
            response = client.get(path + "/delivery")
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertIn("alice@example.test", response.text)

    def test_no_provider_configuration_never_exposes_purchase(self):
        with patch.dict("os.environ", {}, clear=True):
            other = Workspace(Path(self.tmp.name) / "unconfigured.db")
            self.assertFalse(other.task_checkout.status()["configured"])
            with self.assertRaisesRegex(MachineError, "NOT_CONFIGURED"):
                other.task_checkout.plan("missing-task", self.raw)


class WorkerAndConfigTests(unittest.TestCase):
    def test_api_console_serves_all_declared_scripts(self):
        import re

        with (
            tempfile.TemporaryDirectory() as tmp,
            TestClient(create_app(Path(tmp) / "commerce.db")) as client,
        ):
            html = client.get("/").text
            for src in re.findall(r'<script src="([^"]+)"', html):
                self.assertEqual(client.get(src).status_code, 200, src)

    def test_csv_export_blocks_formula_execution(self):
        result = clean_csv({"csv": "name,email\n=IMPORTXML(1),@evil\n"}, ["name", "email"], "csv")
        self.assertIn("'=IMPORTXML(1)", result["content"])
        self.assertIn("'@evil", result["content"])

    def test_csv_shape_and_size_limits(self):
        for source in [
            "wrong,email\nA,a",
            "name,email\nA",
            "name,email\nA,a,extra",
            "name,email",
            "\x00",
            "x" * 20001,
        ]:
            with self.subTest(source=source[:30]), self.assertRaises(MachineError):
                clean_csv({"csv": source}, ["name", "email"], "json")

    def test_live_environment_and_unimplemented_service_rejected(self):
        for config in [
            CONFIG | {"public_origin": "http://local"},
            CONFIG | {"client_secret_env": "secret-in-config"},
            CONFIG | {"services": [CONFIG["services"][0] | {"worker": "research_placeholder"}]},
            CONFIG | {"environment": "LIVE"},
        ]:
            with self.subTest(config=config), self.assertRaises(MachineError):
                PayPalSandbox(config)

    def test_oauth_create_capture_read_transport_contract_and_stable_keys(self):
        provider = PayPalSandbox(copy.deepcopy(CONFIG))
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path == "/v1/oauth2/token":
                self.assertTrue(request.headers["authorization"].startswith("Basic "))
                self.assertIn(b"grant_type=client_credentials", request.content)
                return httpx.Response(200, json={"access_token": "fixture-oauth-token"})
            self.assertEqual(request.headers["authorization"], "Bearer fixture-oauth-token")
            return httpx.Response(200, json={"id": "TESTORDER12345"})

        with (
            patch.dict(
                os.environ,
                {"SKEW_PAYPAL_CLIENT_ID": "fixture-client", "SKEW_PAYPAL_CLIENT_SECRET": "fixture-secret"},
            ),
            patch(
                "machine_engine.paypal.PinnedTransport", side_effect=lambda urls: httpx.MockTransport(handler)
            ),
        ):
            provider.create({"id": "purchase-test", "hash": "a" * 64, "amount": "1.00"}, "create-uuid")
            provider.capture("TESTORDER12345", "capture-uuid")
            provider.read("TESTORDER12345")
        operations = [r for r in calls if r.url.path != "/v1/oauth2/token"]
        self.assertEqual([r.method for r in operations], ["POST", "POST", "GET"])
        self.assertEqual(operations[0].headers["paypal-request-id"], "create-uuid")
        self.assertEqual(operations[1].headers["paypal-request-id"], "capture-uuid")
        body = json.loads(operations[0].content)
        self.assertEqual(body["purchase_units"][0]["payee"]["merchant_id"], CONFIG["merchant_id"])
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "1.00"})
        self.assertTrue(all(r.url.host == "api-m.sandbox.paypal.com" for r in calls))

    def test_transport_rejects_redirect_and_does_not_leak_secret(self):
        provider = PayPalSandbox(copy.deepcopy(CONFIG))
        with (
            patch.dict(
                os.environ,
                {"SKEW_PAYPAL_CLIENT_ID": "fixture-client", "SKEW_PAYPAL_CLIENT_SECRET": "fixture-secret"},
            ),
            patch(
                "machine_engine.paypal.PinnedTransport",
                side_effect=lambda urls: httpx.MockTransport(
                    lambda r: httpx.Response(302, headers={"location": "https://evil.test"})
                ),
            ),
        ):
            with self.assertRaisesRegex(MachineError, "NOT_CONFIRMED") as error:
                provider.read("TESTORDER12345")
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_private_config_only_and_missing_credentials_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paypal.json"
            path.write_text(json.dumps(CONFIG))
            path.chmod(0o644)
            with patch.dict(os.environ, {"ENGINE_PAYPAL_SANDBOX_FILE": str(path)}, clear=True):
                with self.assertRaisesRegex(MachineError, "PRIVATE_CONFIG"):
                    PayPalSandbox.configured()
                path.chmod(0o600)
                provider = PayPalSandbox.configured()
                self.assertFalse(provider.credentials_available())
                workspace = Workspace(Path(tmp) / "work.db", task_provider=provider)
                self.assertFalse(workspace.task_checkout.status()["configured"])

    def test_hosted_agent_cannot_approve_capture_or_download(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("machine_engine.paypal.PayPalSandbox.configured", return_value=FakePayPal()),
        ):
            app = create_app(Path(tmp) / "commerce.db")
            owner, agent = TestClient(app), TestClient(app)
            owner.post("/api/sessions", json={})
            key = owner.post(
                "/api/keys",
                json={
                    "name": "Agent",
                    "scopes": ["engine:read", "engine:write"],
                    "policy_id": None,
                    "ttl_seconds": 3600,
                },
            ).json()
            agent.headers["Authorization"] = "Bearer " + key["secret"]
            self.assertEqual(agent.get("/api/engine/task-checkout").status_code, 200)
            for action in ["approve", "capture", "reconcile", "fulfill"]:
                self.assertEqual(
                    agent.post("/api/engine/task-purchases/unknown/" + action, json={}).status_code, 403
                )
            self.assertEqual(agent.get("/api/engine/task-purchases/unknown/result").status_code, 403)
            self.assertEqual(agent.get("/api/engine/task-purchases/unknown/delivery").status_code, 403)
            owner.close()
            agent.close()


if __name__ == "__main__":
    unittest.main()
