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
        self.evidence.write_bytes((ROOT / "tests/fixtures/engine-settlement.json").read_bytes())
        self.client = TestClient(create_portal(self.site, evidence_path=self.evidence))

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_engine_redirects_to_one_console_with_correct_assets(self):
        redirect = self.client.get("/engine", follow_redirects=False)
        self.assertEqual(redirect.status_code, 307)
        self.assertEqual(redirect.headers['location'], '/commerce/console')
        response = self.client.get("/console")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="machine-api-prefix" content="/commerce"', response.text)
        for asset in ["operations.css", "operations.js", "agents.js", "tasks.js", "tasks.css", "workspace.css", "commerce.js", "commerce.css", "assets/app-engine.svg", "assets/ui-icons.svg"]:
            self.assertIn("/commerce/" + asset, response.text)
            self.assertEqual(self.client.get("/" + asset).status_code, 200)
        self.assertEqual(response.text.count('/commerce/workspace.css?v='), 1)
        self.assertEqual(response.text.count('/commerce/commerce.js?v='), 1)
        self.assertEqual(response.text.count('/commerce/commerce.css?v='), 1)
        self.assertIn('id="market-view"', response.text)
        self.assertIn('id="subscriptions-view"', response.text)
        self.assertIn('id="key-preset"', response.text)
        self.assertGreater(response.text.index('/commerce/workspace.css'), response.text.index('/commerce/operations.css'))
        self.assertEqual(self.client.get('/assets/app-engine.svg').headers['content-type'], 'image/svg+xml')
        self.assertEqual(self.client.get('/assets/../../.env').status_code, 404)
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


    def test_public_playground_runs_the_actual_compiler_without_mutating_runtime(self):
        source = self.client.get('/demo/engine/example').json()['example']
        first = self.client.post('/demo/engine/compile', json=source)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['execution_authority'], 'NONE')
        self.assertEqual(first.json()['mode'], 'STATIC_COMPILE_ONLY')
        self.assertEqual(first.json(), self.client.post('/demo/engine/compile', json=source).json())
        self.assertNotIn('set-cookie', first.headers)

    def test_public_compile_bounds_and_invalid_inputs_fail_closed(self):
        self.assertEqual(self.client.post('/demo/engine/compile', content='x'*20001).status_code, 413)
        for value in [[], {}, {'instructions': []}]:
            self.assertEqual(self.client.post('/demo/engine/compile', json=value).status_code, 400)


if __name__ == "__main__":
    unittest.main()
