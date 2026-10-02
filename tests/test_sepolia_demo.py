"""Exercise the real HTTP client's lifecycle used by the external test signer."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sepolia_demo import approved_match, execute, owner, renew


class SepoliaClientTests(unittest.TestCase):
    def test_external_signer_refuses_to_repeat_a_transmission_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            for phase in ["SUBMISSION_ATTEMPTED", "VERIFIED"]:
                state.write_text(json.dumps({"phase": phase}))
                with patch("sepolia_demo.STATE", state), patch("sepolia_demo.chain") as chain:
                    with self.assertRaises(RuntimeError):
                        execute()
                    chain.assert_not_called()

    def test_unsigned_renewal_refuses_an_ambiguous_or_paid_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            state.write_text(json.dumps({"phase": "SUBMISSION_ATTEMPTED"}))
            with patch("sepolia_demo.STATE", state), patch("sepolia_demo.agent") as agent:
                with self.assertRaises(RuntimeError):
                    renew()
                agent.assert_not_called()

    def test_prepared_local_state_cannot_override_a_transmitted_server_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            state.write_text(json.dumps({"phase": "PREPARED", "buyer_secret": "test-only", "payment_id": "payment-one"}))
            client = Mock()
            for status in ["UNKNOWN", "SETTLEMENT_REPORTED", "SETTLED"]:
                client.get.return_value.json.return_value = {"status": status, "tx_hash": None}
                with patch("sepolia_demo.STATE", state), patch("sepolia_demo.agent") as factory, \
                        patch("sepolia_demo.post") as post:
                    factory.return_value.__enter__.return_value = client
                    with self.assertRaises(RuntimeError):
                        renew()
                    post.assert_not_called()

    def test_prior_unsigned_offer_does_not_ambiguate_current_registered_sale(self):
        old = {"id": "old-match", "terms": {"supply_id": "old-supply"}}
        current = {"id": "new-match", "terms": {"supply_id": "new-supply"}}
        self.assertEqual(approved_match([old, current], "new-supply"), current)

    def test_missing_or_duplicate_current_sale_is_rejected(self):
        current = {"terms": {"supply_id": "new-supply"}}
        for matches in [[], [current, current]]:
            with self.assertRaises(RuntimeError):
                approved_match(matches, "new-supply")

    def test_owner_opens_once_authenticates_and_closes(self):
        calls = []
        token = "test-session-not-a-real-secret"

        def handle(request):
            calls.append(request)
            if request.url.path == "/api/sessions":
                return httpx.Response(200, json={}, headers={"Set-Cookie": "machine_buyer=" + token + "; Secure"})
            self.assertEqual(request.headers["Authorization"], "Bearer " + token)
            return httpx.Response(200, json={"status": "ok"})

        client = httpx.Client(base_url="http://127.0.0.1:4261", transport=httpx.MockTransport(handle))
        with patch("sepolia_demo.httpx.Client", return_value=client), \
                owner("buyer", {"passwords": {"buyer": "test-only-password"}}) as authenticated:
            self.assertEqual(authenticated.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(client.is_closed)

    def test_login_failure_closes_client(self):
        client = httpx.Client(base_url="http://127.0.0.1:4261", transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "invalid login"})))
        with patch("sepolia_demo.httpx.Client", return_value=client), \
                self.assertRaises(httpx.HTTPStatusError), owner("buyer", {"passwords": {"buyer": "invalid"}}):
            self.fail("failed login cannot authorize an owner")
        self.assertTrue(client.is_closed)


if __name__ == "__main__":
    unittest.main()
