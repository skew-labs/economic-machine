"""Agent credentials must never enlarge console or capital authority."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from test_commerce import FakeRPC
from test_market import demand, supply

from machine_commerce.api import create_app
from machine_commerce.domain import DEFAULT_REQUESTS
from machine_commerce.providers import Workers


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.path = Path(self.temp.name) / "access.db"
        self.app = create_app(self.path, lambda: self.now, Workers(lambda: self.now, FakeRPC(self.now)))
        self.owner = TestClient(self.app)
        self.session = self.owner.post("/api/sessions", json={}).json()
        self.agent = TestClient(self.app)
        self.policy = self.new_policy()

    def tearDown(self):
        self.agent.close()
        self.owner.close()
        self.temp.cleanup()

    def new_policy(self, budget="1", ttl=3600, max_order="0.25"):
        response = self.owner.post("/api/policies", json={"budget": budget, "max_order": max_order,
            "allowed_offers": ["csv-normalize", "arbitrum-state"], "ttl_seconds": ttl})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def key(self, scopes=None, pid=None, ttl=3600):
        scopes = ["read"] if scopes is None else scopes
        response = self.owner.post("/api/keys", json={"name": "Research agent", "scopes": scopes,
            "policy_id": pid, "ttl_seconds": ttl})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.agent.headers["Authorization"] = "Bearer " + body["secret"]
        return body

    def reserve(self, policy=None, key="agent-order"):
        return self.agent.post("/api/orders", json={"policy_id": (policy or self.policy)["id"],
            "offer_id": "csv-normalize", "request": DEFAULT_REQUESTS["csv-normalize"], "idempotency_key": key})

    def test_secret_is_returned_once_and_only_hash_is_stored(self):
        created = self.key()
        self.assertTrue(created["shown_once"])
        listed = self.owner.get("/api/keys").json()
        self.assertNotIn(created["secret"], str(listed))
        with self.app.state.store.connect() as db:
            row = db.execute("SELECT * FROM api_keys").fetchone()
            self.assertEqual(row["token_hash"], hashlib.sha256(created["secret"].encode()).hexdigest())
            self.assertNotIn(created["secret"], str(tuple(row)))
            journal = [r[0] for r in db.execute("SELECT event_json FROM events")]
            self.assertNotIn(created["secret"], str(journal))
        self.assertEqual(listed["keys"][0]["status"], "active")

    def test_read_key_cannot_spend_administer_or_escalate(self):
        self.key()
        self.assertEqual(self.agent.get("/api/workspace").status_code, 200)
        self.assertEqual(self.reserve().status_code, 403)
        for path in ["/api/policies", "/api/keys", "/api/demands", "/api/supplies"]:
            self.assertEqual(self.agent.post(path, json={}).status_code, 403)
        self.assertEqual(self.agent.get("/api/keys").status_code, 403)
        self.assertEqual(self.owner.get("/api/workspace").json()["reserved"], "0")

    def test_invalid_bearer_never_falls_back_to_owner_cookie(self):
        for credential in ["Basic invalid", "Bearer em_test_invalid", "Bearer "]:
            self.assertEqual(self.owner.get("/api/keys", headers={"Authorization": credential}).status_code, 401)

    def test_scoped_agent_executes_real_worker_and_receipt(self):
        self.key(["read", "orders:write"], self.policy["id"])
        order = self.reserve().json()
        result = self.agent.post(f'/api/orders/{order["id"]}/run', json={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "SETTLED")
        self.assertEqual(self.agent.get(f'/api/orders/{order["id"]}/artifact').json()["row_count"], 2)
        receipt = self.agent.get(f'/api/orders/{order["id"]}/receipt').json()
        self.assertEqual(receipt["settlement"], "SANDBOX_LEDGER")
        self.assertEqual(self.owner.get("/api/workspace").json()["spent"], "0.12")

    def test_policy_binding_blocks_creating_running_and_cancelling_other_orders(self):
        self.key(["orders:write"], self.policy["id"])
        other = self.new_policy()
        self.assertEqual(self.reserve(other).status_code, 403)
        response = self.owner.post("/api/orders", json={"policy_id": other["id"], "offer_id": "csv-normalize",
            "request": DEFAULT_REQUESTS["csv-normalize"], "idempotency_key": "owner-order"})
        oid = response.json()["id"]
        for action in ["run", "cancel"]:
            self.assertEqual(self.agent.post(f"/api/orders/{oid}/{action}", json={}).status_code, 403)
        self.assertEqual(self.app.state.store.order(self.session["snapshot"]["buyer_id"], oid)["status"], "RESERVED")

    def test_keys_share_policy_cap_and_idempotent_reservation(self):
        limited = self.new_policy("0.2", max_order="0.2")
        self.key(["orders:write"], limited["id"])
        first = self.reserve(limited, "same-key").json()
        self.assertEqual(self.reserve(limited, "same-key").json()["id"], first["id"])
        self.key(["orders:write"], limited["id"])
        self.assertEqual(self.reserve(limited, "different-key").status_code, 409)
        self.assertEqual(self.owner.get("/api/workspace").json()["reserved"], "0.12")

    def test_revocation_is_immediate_and_idempotent_without_reversing_orders(self):
        created = self.key(["read", "orders:write"], self.policy["id"])
        oid = self.reserve().json()["id"]
        path = f'/api/keys/{created["key"]["id"]}/revoke'
        self.assertEqual(self.agent.post(path, json={}).status_code, 403)
        first = self.owner.post(path, json={}).json()
        self.assertEqual(first["status"], "revoked")
        self.assertEqual(self.owner.post(path, json={}).json(), first)
        self.assertEqual(self.agent.get("/api/workspace").status_code, 401)
        self.assertEqual(self.agent.post(f"/api/orders/{oid}/run", json={}).status_code, 401)
        self.assertEqual(self.owner.get(f"/api/orders/{oid}").json()["status"], "RESERVED")
        self.assertEqual(self.owner.post(f"/api/orders/{oid}/cancel", json={}).json()["status"], "REFUNDED")

    def test_expiry_is_bounded_by_owner_and_policy_and_records_usage(self):
        policy = self.new_policy(ttl=60)
        created = self.key(["read", "orders:write"], policy["id"], ttl=86400)
        self.assertEqual(created["key"]["expires"], self.now + 60)
        self.assertEqual(self.agent.get("/api/workspace").status_code, 200)
        self.assertEqual(self.owner.get("/api/keys").json()["keys"][0]["last_used"], self.now)
        self.now += 60
        self.assertEqual(self.agent.get("/api/workspace").status_code, 401)
        self.assertEqual(self.owner.get("/api/keys").json()["keys"][0]["status"], "expired")
        later = self.key(ttl=86400)
        self.assertEqual(later["key"]["expires"], 1790986400)

    def test_key_and_policy_cannot_cross_workspaces(self):
        created = self.key()
        other = TestClient(self.app)
        other.post("/api/sessions", json={})
        response = other.post("/api/keys", json={"name": "foreign", "scopes": ["orders:write"],
            "policy_id": self.policy["id"], "ttl_seconds": 60})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(other.post(f'/api/keys/{created["key"]["id"]}/revoke', json={}).status_code, 409)
        self.assertEqual(other.get("/api/keys").json()["keys"], [])
        other.close()

    def test_malformed_key_permissions_and_lifetimes_are_rejected(self):
        valid = {"name": "agent", "scopes": ["read"], "policy_id": None, "ttl_seconds": 60}
        for change in [{"name": "\n"}, {"name": []}, {"scopes": []}, {"scopes": ["owner"]},
                       {"scopes": ["read", "read"]}, {"scopes": [[]]}, {"ttl_seconds": True},
                       {"ttl_seconds": 59}, {"ttl_seconds": 86401}, {"scopes": ["orders:write"]},
                       {"policy_id": self.policy["id"]}]:
            self.assertEqual(self.owner.post("/api/keys", json={**valid, **change}).status_code, 409)
        self.assertEqual(self.owner.get("/api/keys").json()["keys"], [])

    def test_demand_and_supply_scopes_admit_only_their_own_routes(self):
        self.key(["demands:write"])
        self.assertEqual(self.agent.post("/api/demands", json=demand()).status_code, 200)
        self.assertEqual(self.agent.post("/api/supplies", json=supply(self.now)).status_code, 403)
        self.key(["supplies:write"])
        self.assertEqual(self.agent.post("/api/supplies", json=supply(self.now)).status_code, 200)
        self.assertEqual(self.agent.post("/api/demands", json=demand()).status_code, 403)
        self.assertEqual(self.agent.post("/api/matches/unknown/payment-request", json={}).status_code, 403)

    def test_persistence_and_restart_preserve_hashed_key_authority(self):
        created = self.key()
        restarted = create_app(self.path, lambda: self.now, Workers(lambda: self.now, FakeRPC(self.now)))
        client = TestClient(restarted)
        response = client.get("/api/workspace", headers={"Authorization": "Bearer " + created["secret"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["buyer_id"], self.session["snapshot"]["buyer_id"])
        self.assertTrue(response.json()["journal_integrity"])
        client.close()

    def test_payment_request_permission_never_grants_live_payment_authority(self):
        seller = TestClient(self.app)
        seller.post("/api/sessions", json={})
        seller.post("/api/supplies", json=supply(self.now))
        self.owner.post("/api/demands", json=demand())
        match = self.owner.get("/api/market").json()["matches"][0]
        self.key(["payments:request"])
        payment = self.agent.post(f'/api/matches/{match["id"]}/payment-request',
            json={"terms_hash": match["terms_hash"]})
        self.assertEqual(payment.status_code, 200)
        self.assertEqual(payment.json()["status"], "BLOCKED")
        self.assertEqual(self.owner.get("/api/workspace").json()["spent"], "0")
        self.assertEqual(self.reserve().status_code, 403)
        seller.close()

    def test_corrupted_journal_rolls_back_new_key_authority(self):
        with self.app.state.store.connect() as db:
            db.execute("UPDATE events SET event_hash='corrupted' WHERE ordinal=1")
        response = self.owner.post("/api/keys", json={"name": "agent", "scopes": ["read"],
            "policy_id": None, "ttl_seconds": 60})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.owner.get("/api/keys").json()["keys"], [])


if __name__ == "__main__":
    unittest.main()
