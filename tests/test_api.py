import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from test_commerce import FakeRPC
from test_market import demand, supply

from machine_commerce.api import create_app
from machine_commerce.domain import DEFAULT_REQUESTS
from machine_commerce.providers import Workers


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.app = create_app(Path(self.temp.name) / "app.db", lambda: self.now,
                              Workers(lambda: self.now, FakeRPC(self.now)))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def start(self, client=None):
        client = client or self.client
        session = client.post("/api/sessions", json={}).json()
        policy = client.post("/api/policies", json={"budget": "2", "max_order": "0.25",
            "allowed_offers": ["csv-normalize", "arbitrum-state"], "ttl_seconds": 3600}).json()
        return session, policy

    def test_full_purchase_delivery_events_and_downloads(self):
        _, policy = self.start()
        created = self.client.post("/api/orders", json={"policy_id": policy["id"], "offer_id": "csv-normalize",
            "request": DEFAULT_REQUESTS["csv-normalize"], "idempotency_key": "api-test"})
        self.assertEqual(created.status_code, 200)
        oid = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/orders/{oid}/run", json={}).json()["status"], "SETTLED")
        self.assertEqual(len(self.client.get(f"/api/orders/{oid}/events").json()["events"]), 5)
        artifact = self.client.get(f"/api/orders/{oid}/artifact")
        self.assertEqual(artifact.json()["row_count"], 2)
        self.assertIn("attachment", artifact.headers["content-disposition"])
        self.assertEqual(self.client.get(f"/api/orders/{oid}/receipt").json()["settlement"], "SANDBOX_LEDGER")

    def test_api_buyer_scope_csrf_and_request_limits(self):
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        _, policy = self.start()
        denied = self.client.post("/api/orders", headers={"Origin": "https://evil.example"}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post("/api/orders", content="x" * 50001,
            headers={"Content-Type": "application/json"}).status_code, 413)
        other = TestClient(self.app)
        self.start(other)
        result = other.post("/api/orders", json={"policy_id": policy["id"], "offer_id": "csv-normalize",
            "request": DEFAULT_REQUESTS["csv-normalize"], "idempotency_key": "isolated"})
        self.assertEqual(result.status_code, 409)
        other.close()

    def test_sdk_token_and_browser_resume_keep_same_balance(self):
        session, policy = self.start()
        resumed = self.client.post("/api/sessions", json={}).json()
        self.assertTrue(resumed["resumed"])
        self.assertEqual(session["snapshot"]["buyer_id"], resumed["snapshot"]["buyer_id"])
        sdk = TestClient(self.app)
        result = sdk.get("/api/workspace", headers={"Authorization": "Bearer " + session["api_token"]})
        self.assertEqual(result.json()["policies"][0]["id"], policy["id"])
        sdk.close()

    def test_static_app_is_new_commerce_product_and_has_csp(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Machine Market", response.text)
        self.assertNotIn("WHOLLET", response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/healthz").json()["customer_signing_authority"], "NONE")

    def test_two_external_agents_negotiate_and_payment_gate_keeps_unpaid(self):
        self.start()
        seller = TestClient(self.app)
        self.start(seller)
        self.assertEqual(seller.post("/api/supplies", json=supply(self.now)).status_code, 200)
        registered = self.client.post("/api/demands", json=demand()).json()
        self.assertEqual(registered["matching"]["compatible_pairs"], 1)
        match = self.client.get("/api/market").json()["matches"][0]
        request = {"terms_hash": match["terms_hash"]}
        self.assertEqual(seller.post(f'/api/matches/{match["id"]}/payment-request', json=request).status_code, 409)
        payment = self.client.post(f'/api/matches/{match["id"]}/payment-request', json=request).json()
        self.assertEqual(payment["status"], "BLOCKED")
        self.assertEqual(payment["payment_status"], "NOT_REQUESTED")
        self.assertEqual(self.client.get("/api/workspace").json()["spent"], "0")
        seller.close()

    def test_malformed_market_fields_are_client_errors(self):
        self.start()
        for raw in [demand(purpose=[]), demand(license={}), demand(units=True)]:
            self.assertEqual(self.client.post("/api/demands", json=raw).status_code, 409)
        for field in ["offer_id", "policy_id"]:
            raw = {"policy_id": "policy-one", "offer_id": "csv-normalize", "request": DEFAULT_REQUESTS["csv-normalize"],
                   "idempotency_key": "malformed", field: []}
            self.assertEqual(self.client.post("/api/orders", json=raw).status_code, 409)


if __name__ == "__main__":
    unittest.main()
