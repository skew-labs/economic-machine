"""Public recorded workspace and its connection to the separate Engine page."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from machine_commerce.portal import ROOT, create_portal


class EnginePortal(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.site = Path(self.directory.name)
        self.evidence = self.site / "completed-trade.json"
        self.evidence.write_bytes((ROOT / "artifacts/submission/completed-trade.json").read_bytes())
        self.client = TestClient(create_portal(self.site, evidence_path=self.evidence))

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_engine_is_a_separate_read_only_page_with_correct_assets(self):
        response = self.client.get("/engine")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="engine-mode" content="RECORDED"', response.text)
        self.assertIn('name="engine-prefix" content="/commerce"', response.text)
        for asset in ["engine.css", "engine.js", "assets/ui-icons.svg"]:
            self.assertIn("/commerce/" + asset, response.text)
            self.assertEqual(self.client.get("/" + asset).status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_recorded_accounts_join_actual_settlement_without_fabricated_usage(self):
        response = self.client.get("/demo/engine")
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["read_only"])
        self.assertEqual(data["execution_authority"], "NONE")
        self.assertEqual(data["runs"][0]["status"], "SETTLED")
        self.assertTrue(data["runs"][0]["receipt"]["external_signer_verified"])
        self.assertEqual(data["assets"][0]["quantity"], "19.99")
        self.assertTrue(data["assets"][0]["stale"])
        self.assertEqual(data["orders"], [])
        self.assertEqual(data["positions"], [])
        self.assertEqual(data["usage"], [])
        self.assertEqual(self.client.post("/demo/engine", json={}).status_code, 405)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.post("/api/engine/connections", json={}).status_code, 405)

    def test_corrupt_or_missing_evidence_cannot_masquerade_as_live_account(self):
        bundle = json.loads(self.evidence.read_text())
        bundle["payment"]["tx_hash"] = "0x" + "00" * 32
        self.evidence.write_text(json.dumps(bundle))
        self.assertEqual(self.client.get("/demo/engine").status_code, 503)
        self.evidence.unlink()
        self.assertEqual(self.client.get("/demo/engine").status_code, 503)


if __name__ == "__main__":
    unittest.main()
