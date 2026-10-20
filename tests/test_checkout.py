"""New checkout boundaries; fixtures never claim public payments or inventory."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_market import supply

from economic_machine.values import MachineError, canonical
from machine_commerce.api import create_app
from machine_commerce.checkout import DEFAULT_PLANS, Checkout, validate_plans
from machine_commerce.market import Market
from machine_commerce.operations import Settings
from machine_commerce.payments import Payments
from machine_commerce.store import Store


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1791040000
        self.path = Path(self.tmp.name) / "commerce.sqlite3"
        self.store = Store(self.path, lambda: self.now)
        self.buyer, self.token = self.store.create_session()
        self.other, self.other_token = self.store.create_session()
        self.seller, _ = self.store.create_session()
        self.market = Market(self.store)
        self.payer = "0x" + "11" * 20
        self.profile = {"url": "https://seller.example/data", "rpc_url": "https://rpc.example/chain",
            "network": "eip155:421614", "asset": "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d",
            "pay_to": "0x" + "22" * 20, "token_name": "USD Coin", "token_version": "2",
            "max_timeout_seconds": 60, "seller_owner": self.seller,
            "data_type": "subscription.atlas-monthly", "data_version": "monthly-v1", "finality": "finalized"}
        self.asset = self.profile["network"] + "/erc20:" + self.profile["asset"]
        self.payments = Payments(self.store, self.market, {"atlas-monthly": self.profile})
        self.checkout = Checkout(self.store, self.market, self.payments, DEFAULT_PLANS)
        self.rules = supply(self.now, name="Atlas Monthly", data_type=self.profile["data_type"],
            version="monthly-v1", unit_price="10", floor_price="10", discount_bps=0,
            min_units=1, payment_asset=self.asset)
        self.sid = self.market.register(self.seller, "supply", self.rules)["id"]
        self.raw = {"resource_id": "atlas-monthly", "supply_id": self.sid, "plan_id": "atlas-monthly",
            "units": 1, "purpose": "research", "license": "internal-use", "max_total": "10",
            "max_age_seconds": 60, "max_refresh_seconds": 60, "response_seconds": 30,
            "idempotency_key": "checkout-test"}
        self.mandate = self.payments.mandate(self.buyer, {"payer": self.payer,
            "payment_asset": self.asset, "budget": "20", "max_order": "10",
            "resources": ["atlas-monthly"], "ttl_seconds": 3600})

    def tearDown(self):
        self.tmp.cleanup()

    def test_mainnet_cutover_preserves_history_but_blocks_new_testnet_checkout(self):
        old = self.prepared()
        self.checkout.public_network = "eip155:42161"
        self.payments.public_network = "eip155:42161"
        self.assertEqual(self.checkout.catalog()["products"], [])
        with self.assertRaisesRegex(MachineError, "historical"):
            self.checkout.quote(self.buyer, self.raw | {"idempotency_key": "after-cutover"})
        self.assertEqual(self.checkout.get(self.buyer, old["id"])["payment_id"], old["payment_id"])
        self.assertEqual(self.payments.get(self.buyer, old["payment_id"])["network"], "eip155:421614")

    def test_direct_payment_preparation_cannot_bypass_cutover(self):
        quote = self.checkout.quote(self.buyer, self.raw)
        self.payments.public_network = "eip155:42161"
        with self.assertRaisesRegex(MachineError, "historical"):
            self.checkout.prepare(self.buyer, quote["id"], self.mandate["id"])

    def prepared(self, key="checkout-test"):
        quote = self.checkout.quote(self.buyer, self.raw | {"idempotency_key": key})
        return self.checkout.prepare(self.buyer, quote["id"], self.mandate["id"])

    def settled_fixture(self, order, *, grant=True, finalized=True):
        """Controlled payment observation injection, not a chain settlement test."""
        artifact = {"terms_hash": order["agreement"]["terms_hash"], "data_version": "monthly-v1", "data": {}}
        if grant:
            artifact["data"]["subscription_grant"] = {"plan_id": "atlas-monthly", "plan_hash": order["plan_hash"],
                "payer": self.payer, "duration_seconds": 30 * 86400}
        with self.store.connect() as db:
            db.execute("UPDATE payments SET status='SETTLED',delivery=?,observation=? WHERE id=?", (
                canonical({"artifact": artifact}).decode(),
                canonical({"status": "PAID", "finality": "finalized" if finalized else "latest"}).decode(), order["payment_id"]))

    def test_catalog_lists_only_approved_current_real_asset_seller(self):
        catalog = self.checkout.catalog()
        self.assertEqual(catalog["products"][0]["status"], "AVAILABLE")
        self.assertFalse(catalog["products"][0]["capacity_verified"])
        self.assertNotIn("rpc.example", json.dumps(catalog))
        self.assertNotIn(self.seller, json.dumps(catalog))
        self.now += 86401
        self.assertEqual(self.checkout.catalog()["products"][0]["status"], "NO_ACTIVE_SELLER_RULE")

    def test_published_ten_usdc_plan_without_payment_provider_is_not_purchasable(self):
        blank = Checkout(self.store, self.market, Payments(self.store, self.market), DEFAULT_PLANS)
        plan = blank.catalog()["plans"][0]
        self.assertEqual(plan["price"], "10")
        self.assertEqual(plan["currency"], "USDC")
        self.assertEqual(plan["status"], "PAYMENT_PROVIDER_NOT_CONFIGURED")
        self.assertIsNone(plan["product"])
        with self.assertRaises(MachineError): blank.quote(self.buyer, self.raw)

    def test_quote_replay_atomic_negotiation_and_changed_conditions(self):
        first = self.checkout.quote(self.buyer, self.raw)
        self.assertEqual(first["id"], self.checkout.quote(self.buyer, self.raw)["id"])
        self.assertEqual(first["agreement"]["terms"]["total_price"], "10")
        with self.assertRaises(MachineError): self.checkout.quote(self.buyer, self.raw | {"max_total": "11"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demands WHERE owner=?", (self.buyer,)).fetchone()[0], 1)
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 0)

    def test_insufficient_budget_rolls_back_demand_and_no_match_is_leaked(self):
        with self.assertRaises(MachineError): self.checkout.quote(self.buyer, self.raw | {"max_total": "9"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM demands WHERE owner=?", (self.buyer,)).fetchone()[0], 0)

    def test_expired_or_wrong_seller_and_usage_rights_cannot_quote(self):
        for change in [{"supply_id": "supply-missing"}, {"license": "commercial-use"}, {"purpose": "commercial"}, {"units": True}]:
            with self.subTest(change=change), self.assertRaises(MachineError):
                self.checkout.quote(self.buyer, self.raw | change)
        self.now += 61
        with self.assertRaises(MachineError): self.checkout.quote(self.buyer, self.raw)

    def test_subscription_price_and_sku_are_not_client_controlled(self):
        with self.assertRaises(MachineError): self.checkout.quote(self.buyer, self.raw | {"plan_id": None})
        changed = copy.deepcopy(self.checkout.plans["atlas-monthly"])
        changed["price"] = "9"
        self.checkout.plans["atlas-monthly"] = changed
        with self.assertRaises(MachineError): self.checkout.quote(self.buyer, self.raw)
        wrong = self.profile | {"asset": "0x" + "33" * 20}
        with self.assertRaises(MachineError): validate_plans(DEFAULT_PLANS, {"atlas-monthly": wrong})

    def test_concurrent_prepare_has_one_payment_and_one_shared_hold(self):
        quote = self.checkout.quote(self.buyer, self.raw)
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda _: self.checkout.prepare(self.buyer, quote["id"], self.mandate["id"]), range(3)))
        self.assertEqual(len({v["payment_id"] for v in values}), 1)
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 10_000_000)
        self.assertIsNone(self.checkout.get(self.buyer, quote["id"])["entitlement"])

    def test_bad_limit_can_be_corrected_before_any_payment_reservation(self):
        quote = self.checkout.quote(self.buyer, self.raw)
        with self.assertRaises(MachineError): self.checkout.prepare(self.buyer, quote["id"], "mandate-unknown")
        self.assertEqual(self.checkout.prepare(self.buyer, quote["id"], self.mandate["id"])["status"], "PREPARED")

    def test_crash_after_payment_reservation_recovers_its_existing_purchase_link(self):
        order = self.prepared()
        with self.store.connect() as db:
            db.execute("UPDATE commerce_checkouts SET payment_id=NULL WHERE id=?", (order["id"],))
        restarted = Checkout(self.store, self.market, self.payments, DEFAULT_PLANS)
        recovered = restarted.get(self.buyer, order["id"])
        self.assertEqual(recovered["payment_id"], order["payment_id"])
        self.assertEqual(recovered["status"], "PREPARED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 10_000_000)

    def test_changed_registry_or_cross_owner_is_denied(self):
        quote = self.checkout.quote(self.buyer, self.raw)
        with self.assertRaises(MachineError): self.checkout.get(self.other, quote["id"])
        with self.assertRaises(MachineError): self.checkout.prepare(self.other, quote["id"], self.mandate["id"])
        self.payments.profiles["atlas-monthly"]["pay_to"] = "0x" + "44" * 20
        with self.assertRaises(MachineError): self.checkout.prepare(self.buyer, quote["id"], self.mandate["id"])

    def test_subscription_needs_finality_and_exact_delivery_grant(self):
        order = self.prepared()
        for kwargs in [{"grant": False}, {"finalized": False}]:
            self.settled_fixture(order, **kwargs)
            value = self.checkout.get(self.buyer, order["id"])
            self.assertIsNone(value["entitlement"])
            self.assertEqual(value["entitlement_error"], "SUBSCRIPTION_DELIVERY_NOT_VERIFIED")
            with self.assertRaises(MachineError): self.checkout.require_access(self.buyer, "atlas-monthly")

    def test_subscription_activation_restart_replay_and_manual_renewal(self):
        first = self.prepared()
        self.settled_fixture(first)
        grant = self.checkout.get(self.buyer, first["id"])["entitlement"]
        self.assertEqual(grant["expires"] - grant["starts"], 30 * 86400)
        restarted = Checkout(self.store, self.market, self.payments, DEFAULT_PLANS)
        self.assertEqual(restarted.get(self.buyer, first["id"])["entitlement"], grant)
        self.assertEqual(restarted.require_access(self.buyer, "atlas-monthly")["id"], grant["id"])
        with self.assertRaises(MachineError): restarted.require_access(self.other, "atlas-monthly")
        second = self.prepared("renewal-test")
        self.settled_fixture(second)
        renewal = restarted.get(self.buyer, second["id"])["entitlement"]
        self.assertEqual(renewal["starts"], grant["expires"])
        self.assertEqual(renewal["status"], "SCHEDULED")
        self.assertFalse(restarted.snapshot(self.buyer)["auto_charge"])

    def test_cancel_unsigned_purchase_releases_hold_once(self):
        order = self.prepared()
        self.payments.cancel_unsigned(self.buyer, order["payment_id"])
        self.payments.cancel_unsigned(self.buyer, order["payment_id"])
        self.assertEqual(self.checkout.get(self.buyer, order["id"])["status"], "CANCELLED")
        self.assertEqual(self.payments.snapshot(self.buyer)["mandates"][0]["reserved"], 0)

    def test_http_checkout_permissions_mandate_binding_and_asset_routes(self):
        app = create_app(self.path, lambda: self.now, settings=Settings(resources={"atlas-monthly": self.profile}))
        with TestClient(app) as client:
            catalog = client.get("/api/commerce/catalog")
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual(client.get("/api/commerce/checkouts").status_code, 401)
            buyer_key = app.state.access.create(self.buyer, {"name": "buyer fixture", "scopes": ["read", "demands:write", "payments:request"],
                "policy_id": None, "payment_mandate_id": self.mandate["id"], "ttl_seconds": 3600})
            reader_key = app.state.access.create(self.buyer, {"name": "reader fixture", "scopes": ["read"], "policy_id": None, "ttl_seconds": 3600})
            buyer = {"Authorization": "Bearer " + buyer_key["secret"]}
            reader = {"Authorization": "Bearer " + reader_key["secret"]}
            self.assertEqual(client.post("/api/commerce/checkouts", json=self.raw, headers=reader).status_code, 403)
            quote = client.post("/api/commerce/checkouts", json=self.raw, headers=buyer).json()
            path = f'/api/commerce/checkouts/{quote["id"]}/prepare'
            self.assertEqual(client.post(path, json={"mandate_id": "mandate-other"}, headers=buyer).status_code, 403)
            self.assertEqual(client.post(path, json={"mandate_id": self.mandate["id"]}, headers=buyer).status_code, 200)
            self.assertEqual(client.get('/api/commerce/checkouts/' + quote['id'], headers=reader).status_code, 200)
            self.assertEqual(client.get('/api/commerce/subscriptions/atlas-monthly/delivery', headers=reader).status_code, 403)
            for path in ["/commerce.js", "/commerce.css"]:
                self.assertEqual(client.get(path).status_code, 200)

    def test_native_purchase_status_requires_auth_and_preserves_delivery_denial(self):
        app = create_app(self.path, lambda: self.now, settings=Settings(resources={"atlas-monthly": self.profile}))
        url = "/api/data/purchase-status?purchase_id=0x" + "a" * 64
        with TestClient(app) as client:
            self.assertEqual(client.get(url).status_code, 401)
            headers = {"Authorization": "Bearer " + self.token}
            with patch("machine_commerce.datapass.DataProducts.purchase_status", return_value={"status": "NOT_FINALIZED", "safe_to_retry_payment": False}) as read:
                result = client.get(url, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()["safe_to_retry_payment"])
                self.assertEqual(read.call_args.args[1], "0x" + "a" * 64)
            with patch("machine_commerce.datapass.DataProducts.purchase_status", side_effect=PermissionError("CURRENT_DATA_LICENSE_REQUIRED")):
                self.assertEqual(client.get(url, headers=headers).status_code, 403)

    def test_subscribed_delivery_checks_paid_access_and_expiry(self):
        from test_atlas import sample_report
        report = Path(self.tmp.name) / "atlas.json"
        report.write_text(json.dumps(sample_report()))
        order = self.prepared()
        with patch.dict("os.environ", {"MACHINE_ATLAS_RELEASE": str(report)}):
            app = create_app(self.path, lambda: self.now, settings=Settings(resources={"atlas-monthly": self.profile}))
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer " + self.token}
            path = "/api/commerce/subscriptions/atlas-monthly/delivery"
            self.assertEqual(client.get(path, headers=headers).status_code, 409)
            self.settled_fixture(order)
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["usage_rights"], "internal-use")
            self.assertEqual(len(response.json()["report_sha256"]), 64)
            # Extend only this ephemeral test session to isolate entitlement expiry.
            with self.store.connect() as db:
                db.execute("UPDATE sessions SET expires=? WHERE id=?", (self.now + 40 * 86400, self.buyer))
            self.now += 30 * 86400
            self.assertEqual(client.get(path, headers=headers).status_code, 409)


class CheckoutPaymentIntegration(unittest.TestCase):
    def setUp(self):
        from test_payments import PaymentTests
        PaymentTests.setUpClass()
        self.fixture = PaymentTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_purchase_through_checkout_real_ephemeral_evm_signature_payment_and_delivery(self):
        fx = self.fixture
        checkout = Checkout(fx.store, fx.market, fx.payments)
        raw = {"resource_id": "state-data", "supply_id": fx.supply["id"], "plan_id": None,
            "units": 10, "purpose": "research", "license": "internal-use", "max_total": "0.4",
            "max_age_seconds": 60, "max_refresh_seconds": 60, "response_seconds": 30, "idempotency_key": "checkout-evm"}
        quote = checkout.quote(fx.buyer, raw)
        prepared = checkout.prepare(fx.buyer, quote["id"], fx.mandate["id"])
        ready = fx.payments.challenge(fx.buyer, prepared["payment_id"])
        done = fx.payments.submit(fx.buyer, ready["id"], fx.signed(ready))
        self.assertEqual(done["status"], "SETTLED")
        record = checkout.get(fx.buyer, quote["id"])
        self.assertEqual(record["payment"]["observation"]["status"], "PAID")
        self.assertTrue(record["payment"]["delivery"]["artifact"]["data"]["observed"])
        self.assertEqual(fx.token.functions.balanceOf(fx.w3.eth.accounts[1]).call(), 400000)
        self.assertEqual(checkout.prepare(fx.buyer, quote["id"], fx.mandate["id"])["payment_id"], ready["id"])
        self.assertEqual(fx.transport.signed_calls, 1)


if __name__ == "__main__":
    unittest.main()
