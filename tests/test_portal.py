import json
import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from machine_commerce.portal import ROOT, create_portal, recorded_workspace, run_scenario


class PortalScenarios(unittest.TestCase):
    def test_comparison_uses_six_seller_policies(self):
        result = run_scenario("standard")
        self.assertEqual(result["compatible"], 2)
        self.assertEqual(len(result["suppliers"]), 6)
        self.assertEqual(result["language_model_calls"], 0)
        self.assertFalse(result["payment_requested"])
        self.assertEqual([r["name"] for r in result["suppliers"] if r["status"] == "AGREED"], ["Orbit", "Relay"])
        self.assertEqual(result["suppliers"][0]["total_price"], "0.4")
        self.assertEqual(result["suppliers"][1]["total_price"], "0.38")

    def test_one_price_change_updates_entire_comparison(self):
        result = run_scenario("tight")
        self.assertEqual(result["compatible"], 0)
        self.assertTrue(all("PRICE_OUTSIDE_BUYER_POLICY" in r["reason_codes"] for r in result["suppliers"]))

    def test_one_freshness_change_removes_old_agreement(self):
        result = run_scenario("fresh")
        self.assertEqual(result["compatible"], 1)
        self.assertIn("DATA_NOT_FRESH", result["suppliers"][1]["reason_codes"])

    def test_shared_budget_and_replay(self):
        result = run_scenario("budget")
        self.assertEqual(result["reason"], "policy budget exhausted")
        self.assertTrue(result["same_order"])
        self.assertEqual(result["snapshot"]["reserved"], "0.12")
        self.assertEqual(len(result["snapshot"]["orders"]), 1)
        self.assertEqual(result["snapshot"]["spent"], "0")
        self.assertTrue(result["snapshot"]["journal_integrity"])

    def test_timeout_releases_only_test_ledger_once(self):
        result = run_scenario("recovery")
        self.assertEqual(result["snapshot"]["reserved"], "0")
        self.assertEqual(result["snapshot"]["balance"], "10")
        self.assertEqual(result["snapshot"]["orders"][0]["status"], "REFUNDED")
        self.assertTrue(result["same_order"])
        self.assertFalse(result["payment_requested"])
        self.assertTrue(result["snapshot"]["journal_integrity"])

    def test_requests_have_isolated_state(self):
        first = run_scenario("budget")
        second = run_scenario("budget")
        self.assertNotEqual(first["snapshot"]["buyer_id"], second["snapshot"]["buyer_id"])
        self.assertEqual(second["snapshot"]["reserved"], "0.12")


class PortalHTTP(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.site = Path(self.directory.name)
        (self.site / "index.html").write_text("<html><head></head><body>Preview</body></html>")
        self.client = TestClient(create_portal(self.site,
            proof_path=ROOT / "tests/fixtures/sepolia-workspace.json"))

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_mainnet_datapass_proof_never_returns_legacy_testnet_contract(self):
        from machine_commerce.token_market import deployment
        proof = deployment()
        with patch.dict("os.environ", {"DATAPASS_CHAIN_ID": "42161", "DATAPASS_CONTRACT": proof["contracts"]["SkewDataPass"],
                "DATAPASS_RUNTIME_SHA256": proof["runtime_sha256"]["SkewDataPass"]}):
            response = self.client.get("/demo/datapass/deployment")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["chain_id"], 42161)
            self.assertEqual(response.json()["contracts"]["SkewDataPass"], proof["contracts"]["SkewDataPass"])
        with patch.dict("os.environ", {"DATAPASS_CHAIN_ID": "42161", "DATAPASS_CONTRACT": "0x"+"1"*40,
                "DATAPASS_RUNTIME_SHA256": "0"*64}):
            self.assertEqual(self.client.get("/demo/datapass/deployment").status_code, 503)

    def test_live_scenario_and_cross_origin_without_credentials(self):
        response = self.client.post("/demo/run", json={"scenario": "standard"}, headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", response.headers)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(response.json()["compatible"], 2)

    def test_options_allows_only_public_scenario(self):
        response = self.client.options("/demo/run")
        self.assertEqual(response.headers["access-control-allow-methods"], "POST, OPTIONS")
        response = self.client.get("/demo/workspace")
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_closed_inputs(self):
        for value in [{}, {"scenario": "sign"}, {"scenario": []}, {"scenario": "budget", "wallet": "x"}, ["budget"]]:
            with self.subTest(value=value):
                self.assertEqual(self.client.post("/demo/run", json=value).status_code, 400)
        self.assertEqual(self.client.post("/demo/run", content="not-json").status_code, 400)

    def test_request_size_bound(self):
        self.assertEqual(self.client.post("/demo/run", content="x" * 2049).status_code, 413)

    def test_record_is_read_only_and_exposes_no_credentials(self):
        response = self.client.get("/demo/workspace")
        record = response.json()
        self.assertTrue(record["read_only"])
        self.assertEqual(record["keys"]["keys"], [])
        self.assertEqual(record["payments"]["payments"][0]["status"], "SETTLED")
        for word in ["private_key", "password", "token_hash", "em_live_", "signature"]:
            self.assertNotIn(word, response.text)
        self.assertEqual(self.client.post("/demo/workspace", json={}).status_code, 405)

    def test_console_routes_use_authenticated_runtime_prefix(self):
        response = self.client.get("/console")
        self.assertIn('name="machine-api-prefix" content="/commerce"', response.text)
        self.assertIn('src="/commerce/app.js?v=', response.text)
        self.assertIn('src="/commerce/wallet.js?v=', response.text)
        versions = re.findall(r'src="/commerce/(?:wallet|data|commerce)\.js\?v=([^"]+)', response.text)
        self.assertEqual(len(versions), 3)
        self.assertEqual(len(set(versions)), 1)
        self.assertNotIn('atlas-20261003', versions)
        self.assertIn('href="/commerce/console-theme.css?v=', response.text)
        self.assertIn('href="/commerce/assets/ui-icons.svg?v=', response.text)
        self.assertNotIn('href="/assets/ui-icons.svg', response.text)
        self.assertIn('id="wallet-options"', response.text)
        self.assertNotIn('id="login-password"', response.text)
        self.assertIn("img-src 'self' data:", response.headers["content-security-policy"])
        self.assertIn('base href="/commerce/"', self.client.get("/").text)
        self.assertEqual(self.client.get("/../demo-state.json").status_code, 404)

    def test_landing_assets_are_served_from_the_site_and_private_paths_stay_closed(self):
        for name in ["landing.css", "landing.js"]:
            content = "/* landing asset fixture */"
            (self.site / name).write_text(content)
            response = self.client.get("/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, content)
        self.assertEqual(self.client.get("/.openai/hosting.json").status_code, 404)
        self.assertEqual(self.client.get("/runtime/commerce.sqlite3").status_code, 404)

    def test_reject_nonverified_record(self):
        proof = json.loads((ROOT / "tests/fixtures/sepolia-workspace.json").read_text())
        proof["verified"] = False
        path = self.site / "proof.json"
        path.write_text(json.dumps(proof))
        with self.assertRaises(ValueError):
            recorded_workspace(path)


if __name__ == "__main__":
    unittest.main()
