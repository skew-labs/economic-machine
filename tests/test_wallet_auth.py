import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from test_operations import profile

from machine_commerce.api import create_app
from machine_commerce.operations import Settings, password_hash
from machine_commerce.store import Store
from machine_commerce.wallet_auth import WalletAuth


class WalletLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.operator_hash = password_hash("fixture-operator-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1790900000
        self.origin = "https://console.example"
        self.app = create_app(Path(self.temp.name) / "wallet.db", lambda: self.now,
                              settings=Settings("production", self.origin, {"operator": self.operator_hash}, profile()))
        self.client = TestClient(self.app, base_url=self.origin)
        self.wallet = Account.create()
        self.headers = {"Origin": self.origin}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def challenge(self, client=None, wallet=None, chain=421614):
        return (client or self.client).post("/api/auth/challenge", headers=self.headers,
            json={"address": (wallet or self.wallet).address, "chain_id": chain})

    def proof(self, challenge, wallet=None, message=None):
        data = challenge.json()
        signature = (wallet or self.wallet).sign_message(encode_defunct(text=message or data["message"]))
        return {"challenge_id": data["challenge_id"], "signature": "0x" + signature.signature.hex()}

    def verify(self, proof, client=None):
        return (client or self.client).post("/api/auth/verify", headers=self.headers, json=proof)

    def login(self, wallet=None, client=None, chain=421614):
        return self.verify(self.proof(self.challenge(client, wallet, chain), wallet), client)

    def test_signed_wallet_opens_zero_capital_owner_workspace_and_scoped_key(self):
        challenge = self.challenge()
        self.assertIn("console.example wants you to sign in", challenge.json()["message"])
        self.assertIn("URI: https://console.example/commerce/console", challenge.json()["message"])
        self.assertIn("This does not authorize payments.", challenge.json()["message"])
        response = self.verify(self.proof(challenge))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshot"]["balance"], "0")
        self.assertEqual(response.json()["identity"]["address"], self.wallet.address)
        self.assertNotIn("api_token", response.json())
        self.assertTrue(all(v in response.headers["set-cookie"].lower() for v in ["secure", "httponly", "samesite=strict"]))
        key = self.client.post("/api/keys", headers=self.headers, json={"name": "Agent", "scopes": ["read"],
                              "policy_id": None, "ttl_seconds": 3600})
        self.assertEqual(key.status_code, 200)
        bearer = {"Authorization": "Bearer " + key.json()["secret"]}
        self.assertEqual(self.client.get("/api/workspace", headers=bearer).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers={**bearer, **self.headers}, json={}).status_code, 403)
        self.assertEqual(self.client.get("/api/auth/session", headers=bearer).status_code, 403)
        self.assertEqual(self.client.post("/api/policies", headers=self.headers, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/sessions", headers=self.headers, json={}).json()["resumed"], True)

    def test_claimed_address_alone_never_authenticates(self):
        self.challenge()
        self.assertEqual(self.client.get("/api/workspace").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/verify", headers=self.headers,
            json={"address": self.wallet.address}).status_code, 409)

    def test_signature_for_other_address_is_rejected_without_consuming_valid_nonce(self):
        challenge = self.challenge()
        self.assertEqual(self.verify(self.proof(challenge, Account.create())).status_code, 401)
        self.assertEqual(self.verify(self.proof(challenge)).status_code, 200)

    def test_signature_is_bound_to_server_domain_uri_chain_and_statement(self):
        for old, new in [("console.example", "evil.example"), ("Chain ID: 421614", "Chain ID: 1"),
                         ("/commerce/console", "/other"), ("does not authorize", "authorizes")]:
            challenge = self.challenge()
            self.assertEqual(self.verify(self.proof(challenge, message=challenge.json()["message"].replace(old, new))).status_code, 401)

    def test_nonce_cannot_be_replayed_after_durable_restart(self):
        challenge = self.challenge()
        binding = self.client.cookies.get("machine_login")
        proof = self.proof(challenge)
        self.assertEqual(self.verify(proof).status_code, 200)
        reopened = WalletAuth(Store(self.app.state.store.path, lambda: self.now), self.origin)
        with self.assertRaises(PermissionError):
            reopened.verify(proof, binding)

    def test_challenge_expires_at_exact_boundary(self):
        challenge = self.challenge()
        self.now = challenge.json()["expires_at"]
        self.assertEqual(self.verify(self.proof(challenge)).status_code, 401)

    def test_signature_stolen_from_another_browser_is_not_a_login(self):
        proof = self.proof(self.challenge())
        with TestClient(self.app, base_url=self.origin) as other:
            self.assertEqual(self.verify(proof, other).status_code, 401)
        self.assertEqual(self.verify(proof).status_code, 200)

    def test_unknown_nonce_and_malformed_signature_fail_closed(self):
        self.challenge()
        for proof in [{"challenge_id": "f" * 32, "signature": "0x" + "11" * 65},
                      {"challenge_id": "f" * 32, "signature": None},
                      {"challenge_id": "../wallet", "signature": "0x" + "11" * 65}]:
            self.assertEqual(self.verify(proof).status_code, 401)

    def test_invalid_curve_signature_returns_auth_error_and_leaves_challenge_usable(self):
        challenge = self.challenge()
        self.assertEqual(self.verify({"challenge_id": challenge.json()["challenge_id"],
                                     "signature": "0x" + "00" * 65}).status_code, 401)
        self.assertEqual(self.verify(self.proof(challenge)).status_code, 200)

    def test_concurrent_verification_consumes_nonce_exactly_once(self):
        challenge = self.challenge()
        binding = self.client.cookies.get("machine_login")
        proof = self.proof(challenge)
        def run(_):
            try:
                self.app.state.wallet_auth.verify(proof, binding)
                return "signed-in"
            except PermissionError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(run, [1, 2])), ["rejected", "signed-in"])

    def test_relogin_rotates_cookie_preserves_workspace_keys_and_balance(self):
        first = self.login().json()["snapshot"]["buyer_id"]
        old_token = self.client.cookies.get("machine_buyer")
        key = self.client.post("/api/keys", headers=self.headers, json={"name": "Continuity", "scopes": ["read"],
                               "policy_id": None, "ttl_seconds": 3600}).json()
        second = self.login(chain=1).json()
        self.assertEqual(first, second["snapshot"]["buyer_id"])
        self.assertEqual(second["snapshot"]["balance"], "0")
        self.assertEqual(second["identity"]["chain_id"], 1)
        self.assertEqual(len(self.client.get("/api/keys").json()["keys"]), 1)
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + old_token}).status_code, 401)
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + key["secret"]}).status_code, 200)

    def test_wallets_are_isolated_and_logout_revokes_only_owner_cookie(self):
        first = self.login().json()["snapshot"]["buyer_id"]
        key = self.client.post("/api/keys", headers=self.headers, json={"name": "Persist", "scopes": ["read"],
                               "policy_id": None, "ttl_seconds": 3600}).json()
        old = self.client.cookies.get("machine_buyer")
        self.assertEqual(self.client.post("/api/auth/logout", headers=self.headers, json={}).status_code, 200)
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + old}).status_code, 401)
        self.assertEqual(self.client.get("/api/workspace", headers={"Authorization": "Bearer " + key["secret"]}).status_code, 200)
        second = self.login(Account.create()).json()["snapshot"]["buyer_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.get("/api/keys").json()["keys"], [])

    def test_auth_mutations_require_exact_origin_even_when_header_missing(self):
        for origin in [None, "https://evil.example", "http://console.example"]:
            headers = {"Origin": origin} if origin else {}
            for path in ["challenge", "verify", "logout"]:
                self.assertEqual(self.client.post("/api/auth/" + path, headers=headers, json={}).status_code, 403)

    def test_input_chain_and_address_validation_and_durable_rate_limit(self):
        for address, chain in [("0x" + "1" * 39, 1), (self.wallet.address, True), (self.wallet.address, 56)]:
            self.assertEqual(self.client.post("/api/auth/challenge", headers=self.headers,
                json={"address": address, "chain_id": chain}).status_code, 409)
        for _ in range(7):
            self.assertEqual(self.challenge().status_code, 200)
        self.assertEqual(self.challenge().status_code, 429)
        self.now += 60
        self.assertEqual(self.challenge().status_code, 200)
