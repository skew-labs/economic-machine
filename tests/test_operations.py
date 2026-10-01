import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from economic_machine.values import MachineError
from machine_commerce.api import create_app
from machine_commerce.operations import Operations, Settings, password_hash
from machine_commerce.store import Store


def profile():
    return {"test-resource": {"url": "https://seller.example/data", "rpc_url": "https://rpc.example/rpc",
        "network": "eip155:42161", "asset": "0x" + "11" * 20, "pay_to": "0x" + "22" * 20,
        "token_name": "USDC", "token_version": "2", "max_timeout_seconds": 60,
        "seller_owner": "buyer-registered", "data_type": "data.test", "data_version": "v1", "finality": "finalized"}}


class OperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.secret = "operator-test-password"
        cls.encoded = password_hash(cls.secret)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.path = Path(self.temp.name) / "operations.db"
        self.settings = Settings("production", "https://console.example", {"owner": self.encoded}, profile())
        self.app = create_app(self.path, lambda: self.now, settings=self.settings)
        self.client = TestClient(self.app, base_url="https://console.example")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        return self.client.post("/api/sessions", json={"username": "owner", "password": self.secret})

    def test_production_requires_https_origin_operator_and_resource_configuration(self):
        for settings in [Settings("production"), Settings("production", "http://console.example"),
                         Settings("production", "https://console.example", {"owner": self.encoded}),
                         Settings("production", "https://console.example", {"owner": "scrypt$invalid"}, profile()), Settings("typo")]:
            with self.assertRaises(MachineError):
                settings.validate()

    def test_anonymous_and_bad_credentials_cannot_create_owner_or_seed_test_money(self):
        self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
        self.assertEqual(self.client.post("/api/sessions", json={"username": "owner", "password": "incorrect-pass"}).status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshot"]["balance"], "0")
        self.assertNotIn("api_token", response.json())
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.post("/api/policies", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/orders", json={}).status_code, 403)
        created = self.client.post("/api/keys", json={"name": "Read agent", "scopes": ["read"],
            "policy_id": None, "ttl_seconds": 3600}).json()
        self.assertTrue(created["secret"].startswith("em_live_"))
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + created["secret"]}).status_code, 200)

    def test_production_denies_plain_http_and_foreign_origin_mutations(self):
        insecure = TestClient(self.app, base_url="http://console.example")
        self.assertEqual(insecure.post("/api/sessions", json={}).status_code, 403)
        self.assertEqual(insecure.get("/healthz").status_code, 200)
        insecure.close()
        self.login()
        for origin in ["http://console.example", "https://evil.example"]:
            self.assertEqual(self.client.post("/api/keys", json={}, headers={"Origin": origin}).status_code, 403)

    def test_development_credentials_cannot_become_production_owners(self):
        legacy_sid, legacy_token = self.app.state.store.create_session()
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + legacy_token}).status_code, 401)
        self.client.cookies.set("machine_buyer", legacy_token)
        self.assertEqual(self.client.post("/api/sessions", json={}).status_code, 409)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["snapshot"]["buyer_id"], legacy_sid)

    def test_removed_operator_cannot_continue_with_an_old_key(self):
        self.login()
        created = self.client.post("/api/keys", json={"name": "Read agent", "scopes": ["read"],
            "policy_id": None, "ttl_seconds": 3600}).json()
        self.settings.operators.pop("owner")
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + created["secret"]}).status_code, 401)

    def test_expired_cookie_login_keeps_identity_and_rotates_old_owner_credential(self):
        first = self.login().json()["snapshot"]["buyer_id"]
        old = self.client.cookies.get("machine_buyer")
        self.now += 86401
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        second = self.login().json()["snapshot"]["buyer_id"]
        self.assertEqual(first, second)
        self.assertNotEqual(old, self.client.cookies.get("machine_buyer"))
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + old}).status_code, 401)
        self.assertEqual(self.client.get("/api/workspace").json()["balance"], "0")

    def test_durable_limits_and_cookie_spoofing_cannot_bypass_login_throttle(self):
        for i in range(5):
            self.client.cookies.set("machine_buyer", f"fake-owner-cookie-{i}")
            self.assertEqual(self.client.post("/api/sessions", json={"username": "owner", "password": "incorrect-pass"}).status_code, 401)
        self.client.cookies.set("machine_buyer", "another-fake-cookie")
        self.assertEqual(self.login().status_code, 429)
        reopened = Operations(Store(self.path, lambda: self.now), self.settings)
        self.assertFalse(reopened.admit_rate("login:testclient", 5))
        self.now += 60
        self.assertEqual(self.login().status_code, 200)

    def test_streaming_input_limit_rejects_without_entire_body_buffer(self):
        def chunks():
            for _ in range(20):
                yield b"x" * 5000
        response = self.client.post("/api/sessions", content=chunks(), headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_provisioning_cli_writes_private_hash_without_printing_password(self):
        path = Path(self.temp.name) / "operators.json"
        script = Path(__file__).resolve().parents[1] / "scripts/provision_operator.py"
        with patch("sys.argv", [str(script), "--file", str(path), "--username", "owner"]), \
                patch("getpass.getpass", side_effect=[self.secret, self.secret]), patch("builtins.print") as printed:
            runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.secret, path.read_text())
        self.assertNotIn(self.secret, str(printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
