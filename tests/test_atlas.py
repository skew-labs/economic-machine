import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from economic_machine.values import MachineError, digest
from machine_commerce.atlas import TERMS, azure_hardware, azure_rows, build_report, load_report, observation, quantile, site_lens, verify_report
from machine_commerce.api import create_app
from machine_commerce.providers import Workers
from machine_commerce.datapass import DataProducts, DataPassChain
from machine_commerce.portal import create_portal

NOW = 1791030000


def sample_report():
    rows = [observation("Azure", "japaneast", "Standard_NC24ads_A100_v4", price,
        {"url": "https://prices.azure.com/api/retail/prices", "raw_sha256": str(index) * 64,
         "retrieved_at": NOW - 1, "effective_at": "2026-10-01"}, gpu_model="A100", gpu_count=1)
        for index, price in enumerate(["2", "4", "6"], 1)]
    return build_report(rows, [{"provider": "Azure", "region": "japaneast", "status": "OBSERVED"}], NOW)


def scenario():
    return dict(facility_mw="10", pue="1.3", network_fraction="0.12", reserve_fraction="0.15",
        gpu_watts="700", gpus_per_server="8", server_overhead_watts="1400",
        electricity_usd_kwh="0.12", utilization="0.65", gpu_usd_hour="2.5")


class AtlasTests(unittest.TestCase):
    def test_original_report_recomputes_and_tampering_fails(self):
        report = sample_report()
        self.assertTrue(verify_report(report)["accepted"])
        self.assertEqual(report["derived"]["statistics"][0]["median"], "4.00000000")
        for path in ["price", "summary", "source", "coverage", "terms"]:
            altered = copy.deepcopy(report)
            if path == "price": altered["observations"][0]["instance_usd_hour"] = "0.01"
            if path == "summary": altered["derived"]["statistics"][0]["median"] = "0.1"
            if path == "source": altered["observations"][0]["source"]["url"] = "https://evil.example/prices"
            if path == "coverage": altered["derived"]["coverage"]["rows"] = 20
            if path == "terms": altered["terms"]["ownership_of_hardware"] = True
            with self.subTest(path=path), self.assertRaises(MachineError): verify_report(altered)

    def test_rehashing_a_forged_row_does_not_make_it_normalized(self):
        report = sample_report()
        row = report["observations"][0]
        row["gpu_usd_hour"] = "0.01"
        row["id"] = digest({k: v for k, v in row.items() if k != "id"})
        with self.assertRaises(MachineError): build_report(report["observations"], [], NOW)

    def test_unknown_hardware_is_not_normalized_as_one_gpu(self):
        row = observation("Azure", "japaneast", "Standard_ND_unknown", "20",
            {"url": "https://prices.azure.com/api/retail/prices", "raw_sha256": "a" * 64,
             "retrieved_at": NOW, "effective_at": None})
        report = build_report([row], [], NOW)
        self.assertIsNone(row["gpu_usd_hour"])
        self.assertEqual(report["derived"]["coverage"]["known_hardware_rows"], 0)
        self.assertEqual(report["derived"]["statistics"], [])

    def test_gpu_family_counts_are_explicit(self):
        self.assertEqual(azure_hardware("Standard_NC48ads_A100_v4"), ("A100", 2))
        self.assertEqual(azure_hardware("Standard_NC64as_T4_v3"), ("T4", 4))
        self.assertEqual(azure_hardware("Standard_ND96isr_H100_v5"), ("H100", 8))
        self.assertEqual(azure_hardware("Standard_NC48ads_A100_v5"), (None, None))

    def test_dedup_order_and_exact_quantiles(self):
        report = sample_report()
        rows = report["observations"]
        self.assertEqual(build_report(rows[::-1] + rows, report["derived"]["collection"], NOW), report)
        self.assertEqual(quantile(["1", "2", "4", "9"], 1, 2), "3.00000000")
        with self.assertRaises(MachineError): build_report(rows, [], NOW - 100)

    def test_public_source_and_finite_bounded_prices(self):
        for value in ["NaN", "Infinity", "-1", "0", "1e100", "1e-100", True]:
            with self.subTest(value=value), self.assertRaises(MachineError):
                observation("Azure", "japaneast", "sku", value,
                    {"url": "https://prices.azure.com/api", "raw_sha256": "a" * 64,
                     "retrieved_at": NOW, "effective_at": None})
        with self.assertRaises(MachineError):
            observation("AWS", "japaneast", "sku", "1", {})

    def test_site_lens_reports_integer_capacity_and_exclusions(self):
        result = site_lens(scenario())
        self.assertEqual(result["gpus"], result["servers"] * 8)
        self.assertEqual(result["servers"], 821)
        self.assertEqual(result["input_sha256"], digest(scenario()))
        self.assertFalse(result["capacity_availability_verified"])
        self.assertIn("capex", result["excluded_costs"])
        slower = scenario() | {"pue": "2"}
        self.assertLess(site_lens(slower)["gpus"], result["gpus"])

    def test_site_lens_rejects_unbounded_and_unit_ambiguity(self):
        for key, value in [("pue", "0.9"), ("reserve_fraction", "1"), ("gpu_watts", "NaN"),
                           ("gpu_watts", "1e-999999"), ("gpus_per_server", "1.5"), ("utilization", True)]:
            with self.subTest(key=key, value=value), self.assertRaises(MachineError):
                site_lens(scenario() | {key: value})

    def test_versioned_worker_verifies_delivered_bytes(self):
        report = sample_report()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "release.json"; path.write_text(json.dumps(report))
            with patch.dict("os.environ", {"MACHINE_ATLAS_RELEASE": str(path)}):
                worker = Workers(lambda: NOW)
                request = {"report_sha256": report["report_sha256"], "max_age_seconds": 300, "license": "internal-use"}
                artifact = worker.fulfill("apac-compute-brief", request)
                self.assertTrue(worker.verify("apac-compute-brief", request, artifact)["accepted"])
                bad = copy.deepcopy(artifact); bad["report"]["coverage"]["rows"] = 999
                self.assertFalse(worker.verify("apac-compute-brief", request, bad)["accepted"])
                worker.clock = lambda: NOW + 301
                with self.assertRaises(MachineError): worker.fulfill("apac-compute-brief", request)

    def test_data_api_scopes_cannot_gain_deployment_or_delivery_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "release.json"; path.write_text(json.dumps(sample_report()))
            with patch.dict("os.environ", {"MACHINE_ATLAS_RELEASE": str(path)}):
                app = create_app(Path(folder) / "app.db", lambda: NOW)
                with TestClient(app) as owner, TestClient(app) as agent:
                    self.assertEqual(agent.get("/api/data/catalog").status_code, 401)
                    owner.post("/api/sessions", json={})
                    created = owner.post("/api/keys", json={"name": "data reader", "scopes": ["data:read"],
                        "policy_id": None, "ttl_seconds": 3600}).json()
                    agent.headers["Authorization"] = "Bearer " + created["secret"]
                    self.assertEqual(agent.get("/api/data/catalog").status_code, 200)
                    self.assertEqual(agent.post("/api/data/deployment-plan", json={}).status_code, 403)
                    self.assertEqual(agent.get("/api/data/licenses/1/delivery").status_code, 409)
                    self.assertEqual(owner.post("/api/data/deployment-plan", json={}).status_code, 403)

    def test_report_file_cannot_be_a_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "report"; source.write_text(json.dumps(sample_report()))
            link = Path(folder) / "link"; link.symlink_to(source)
            with self.assertRaises(MachineError): load_report(link)

    def test_public_tools_serve_verified_snapshot_and_explicit_scenario(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "atlas.json"; path.write_text(json.dumps(sample_report()))
            with patch.dict("os.environ", {"MACHINE_ATLAS_RELEASE": str(path)}), TestClient(create_portal(folder)) as client:
                result = client.get("/demo/atlas")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.headers["access-control-allow-origin"], "*")
                self.assertNotIn("set-cookie", result.headers)
                response = client.post("/demo/site-lens", json=scenario())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["input_sha256"], digest(scenario()))
                self.assertEqual(client.post("/demo/site-lens", content="x" * 4097).status_code, 413)
                self.assertEqual(client.post("/demo/site-lens", json=scenario() | {"pue": "NaN"}).status_code, 400)
                self.assertEqual(client.get("/demo/datapass").json()["datapass"]["status"], "NOT_DEPLOYED")
