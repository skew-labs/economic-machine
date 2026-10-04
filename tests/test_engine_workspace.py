"""User-owned accounts, secret boundaries, generation races and local console."""

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient

from economic_machine.values import MachineError
from machine_commerce.transport import PinnedTransport
from machine_engine.api import create_engine_app, recorded_overview
from machine_engine.connections import Connectors, ReadOnlyHTTP, normalize_connection
from machine_engine.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
AT = 1790347260
OWNER_TOKEN = "fixture-owner-" + "x" * 32
WALLET = {"name": "Treasury", "profile": "arbitrum-sepolia-wallet", "config": {"address": "0x" + "12" * 20}}
AI = {"name": "Planner", "profile": "openai-compatible", "config": {"url": "https://example.org/v1/models", "api_key_env": "AI_API_KEY"}}
BINANCE = {"name": "Spot", "profile": "binance-spot", "config": {"api_key_env": "BINANCE_API_KEY", "api_secret_env": "BINANCE_API_SECRET"}}


class FakeHTTP:
    def __init__(self):
        self.calls, self.bad_chain, self.echo_key = [], False, False

    def request(self, url, **options):
        self.calls.append((url, options))
        if url.endswith("/models"):
            return {"data": [{"id": "fixture-key-value" if self.echo_key else "qwen3-32b"}]}
        if url.endswith("/account"):
            return {"balances": [{"asset": "USDT", "free": "100.00000000", "locked": "2.00000000"}]}
        if url.endswith("/openOrders"):
            return [{"orderId": 8, "symbol": "BTCUSDT", "side": "BUY", "status": "NEW", "price": "100",
                     "origQty": "0.1", "executedQty": "0"}]
        method = options["body"]["method"]
        results = {"eth_chainId": hex(1 if self.bad_chain else 421614),
                   "eth_getBlockByNumber": {"number": hex(123)},
                   "eth_getBalance": hex(10 ** 18), "eth_call": hex(19_990_000)}
        return {"jsonrpc": "2.0", "id": 1, "result": results[method]}


class ConnectionContracts(unittest.TestCase):
    def test_secrets_are_environment_references_not_values(self):
        self.assertEqual(normalize_connection(BINANCE)["config"], BINANCE["config"])
        for value in [BINANCE | {"config": {"api_key_env": "sk-real-secret", "api_secret_env": "BINANCE_API_SECRET"}},
                      AI | {"config": AI["config"] | {"secret": "bad"}},
                      WALLET | {"profile": []}, WALLET | {"name": "\n"}]:
            with self.subTest(value=value), self.assertRaises(MachineError):
                normalize_connection(value)

    def test_credential_urls_and_inference_endpoints_are_not_connector_inputs(self):
        for url in ["http://example.org/v1/models", "https://user:pass@example.org/v1/models",
                    "https://example.org/v1/models?api_key=secret", "https://example.org/v1/chat/completions"]:
            with self.subTest(url=url), self.assertRaises(MachineError):
                normalize_connection(AI | {"config": AI["config"] | {"url": url}})

    def test_wallet_uses_one_finalized_height_and_correct_native_units(self):
        http = FakeHTTP()
        result = Connectors(http, clock=lambda: AT).read(normalize_connection(WALLET))
        self.assertEqual(result["assets"][0]["quantity"], "1")
        self.assertEqual(result["assets"][1]["quantity"], "19.99")
        self.assertEqual(result["network"], "eip155:421614")
        self.assertEqual([c[1]["body"]["method"] for c in http.calls],
                         ["eth_chainId", "eth_getBlockByNumber", "eth_getBalance", "eth_call"])
        self.assertEqual(http.calls[2][1]["body"]["params"][1], hex(123))
        self.assertEqual(http.calls[3][1]["body"]["params"][1], hex(123))

    def test_wrong_network_stops_before_balance_reads(self):
        http = FakeHTTP()
        http.bad_chain = True
        with self.assertRaisesRegex(MachineError, "WRONG_CHAIN"):
            Connectors(http).read(normalize_connection(WALLET))
        self.assertEqual(len(http.calls), 1)

    def test_exchange_signature_only_admits_read_only_paths(self):
        http = FakeHTTP()
        with patch.dict(os.environ, {"BINANCE_API_KEY": "fixture-access-key", "BINANCE_API_SECRET": "fixture-hmac-secret"}):
            result = Connectors(http, clock=lambda: AT).read(normalize_connection(BINANCE))
        self.assertEqual(result["assets"][0]["quantity"], "102")
        self.assertEqual(result["orders"][0]["filled"], "0")
        self.assertEqual([c[0].split("/")[-1] for c in http.calls], ["account", "openOrders"])
        for _, options in http.calls:
            params = options["params"]
            unsigned = {k: v for k, v in params.items() if k != "signature"}
            expected = hmac.new(b"fixture-hmac-secret", urlencode(unsigned).encode(), hashlib.sha256).hexdigest()
            self.assertEqual(params["signature"], expected)
            self.assertEqual(options.get("method", "GET"), "GET")
        self.assertNotIn("fixture-", json.dumps(result))

    def test_missing_or_echoed_ai_credentials_do_not_enter_snapshot(self):
        http = FakeHTTP()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(MachineError, "REFERENCE_UNAVAILABLE"):
            Connectors(http).read(normalize_connection(AI))
        http.echo_key = True
        with patch.dict(os.environ, {"AI_API_KEY": "fixture-key-value"}), self.assertRaisesRegex(MachineError, "ECHO_BLOCKED"):
            Connectors(http).read(normalize_connection(AI))

    def test_exchange_balances_keep_all_supported_decimal_digits(self):
        value = "123456789012345678901234567890.123456789012345678"
        http = FakeHTTP()
        original = http.request
        def large_read(url, **options):
            if url.endswith("/account"):
                return {"balances": [{"asset": "TEST", "free": value, "locked": "0.000000000000000001"}]}
            return original(url, **options)
        http.request = large_read
        with patch.dict(os.environ, {"BINANCE_API_KEY": "fixture-access-key", "BINANCE_API_SECRET": "fixture-hmac-secret"}):
            result = Connectors(http).read(normalize_connection(BINANCE))
        self.assertEqual(result["assets"][0]["quantity"], "123456789012345678901234567890.123456789012345679")
    def test_model_catalog_does_not_invoke_the_model(self):
        http = FakeHTTP()
        with patch.dict(os.environ, {"AI_API_KEY": "fixture-key-value"}):
            result = Connectors(http, clock=lambda: AT).read(normalize_connection(AI))
        self.assertEqual(result["models"], ["qwen3-32b"])
        self.assertEqual(result["inference_calls"], 0)
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(http.calls[0][0].endswith("/models"))

    def test_pinned_signed_query_keeps_host_and_tls_identity(self):
        seen = []
        delegate = httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(200, json={"ok": True}))[1])
        def pin(urls, delegate):
            return PinnedTransport(urls, resolver=lambda *_a, **_k: [(2, 1, 6, "", ("8.8.8.8", 443))], delegate=delegate)
        with patch("machine_engine.connections.httpx.HTTPTransport", return_value=delegate), \
                patch("machine_engine.connections.PinnedTransport", side_effect=pin):
            self.assertEqual(ReadOnlyHTTP().request("https://api.binance.com/api/v3/account", params={"timestamp": 1, "signature": "abc"}), {"ok": True})
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["Host"], "api.binance.com")
        self.assertEqual(seen[0].url.query, b"timestamp=1&signature=abc")
        self.assertEqual(seen[0].extensions["sni_hostname"], "api.binance.com")


class WorkspaceContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = AT
        self.http = FakeHTTP()
        self.readers = Connectors(self.http, clock=lambda: self.now)
        self.work = Workspace(Path(self.directory.name) / "engine.sqlite3", readers=self.readers, clock=lambda: self.now)

    def tearDown(self):
        self.directory.cleanup()

    def test_create_does_not_probe_credentials_or_request_payment(self):
        created = self.work.connect(BINANCE)
        self.assertEqual(created["status"], "CONFIGURED")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.work.overview()["execution_authority"], "NONE")

    def test_snapshot_persists_without_key_references_in_public_view(self):
        cid = self.work.connect(WALLET)["id"]
        self.work.sync(cid)
        result = self.work.overview()
        self.assertEqual(result["assets"][1]["quantity"], "19.99")
        self.assertFalse(result["assets"][0]["stale"])
        self.assertNotIn("config", result["connections"][0])
        self.assertTrue(result["runtime"]["journal_integrity"])
        self.now += 61
        self.assertTrue(self.work.overview()["assets"][0]["stale"])

    def test_failure_preserves_prior_snapshot_but_marks_it_degraded(self):
        cid = self.work.connect(WALLET)["id"]
        self.work.sync(cid)
        self.http.bad_chain = True
        result = self.work.sync(cid)
        self.assertFalse(result["new_snapshot"])
        self.assertEqual(result["error_code"], "WRONG_CHAIN")
        view = self.work.overview()
        self.assertEqual(view["assets"][1]["quantity"], "19.99")
        self.assertTrue(view["assets"][1]["stale"])

    def test_unexpected_exception_text_cannot_expose_credentials(self):
        cid = self.work.connect(WALLET)["id"]
        with patch.object(self.readers, "read", side_effect=RuntimeError("Bearer fixture-secret")):
            result = self.work.sync(cid)
        self.assertEqual(result["error_code"], "READ_FAILED")
        self.assertNotIn("fixture-secret", json.dumps(self.work.overview()))

    def test_disconnect_during_read_invalidates_the_generation(self):
        cid = self.work.connect(WALLET)["id"]
        original = self.readers.read
        def read(connection):
            result = original(connection)
            self.work.disconnect(cid)
            return result
        with patch.object(self.readers, "read", side_effect=read), self.assertRaisesRegex(MachineError, "CHANGED_DURING"):
            self.work.sync(cid)
        self.assertEqual(self.work.overview()["connections"][0]["status"], "DISCONNECTED")
        self.assertEqual(self.work.overview()["assets"], [])

    def test_disconnected_sources_leave_history_without_reporting_live_assets(self):
        cid = self.work.connect(WALLET)["id"]
        self.work.sync(cid)
        self.work.disconnect(cid)
        self.work.disconnect(cid)
        self.assertEqual(self.work.overview()["assets"], [])
        with self.assertRaisesRegex(MachineError, "ACTIVE_CONNECTION"):
            self.work.sync(cid)

    def test_usage_is_idempotent_and_not_a_provider_invoice(self):
        cid = self.work.connect(AI)["id"]
        report = {"event_id": "usage-one", "connection_id": cid, "model": "qwen3-32b", "input_tokens": 300,
                  "output_tokens": 117, "cost_microusd": 50, "occurred_at": AT}
        self.assertFalse(self.work.record_usage(report)["idempotent"])
        self.assertTrue(self.work.record_usage(report)["idempotent"])
        with self.assertRaisesRegex(MachineError, "IDEMPOTENCY_CONFLICT"):
            self.work.record_usage(report | {"input_tokens": 301})
        self.assertEqual(self.work.overview()["usage"][0]["calls"], 1)
        self.assertIn("NOT_PROVIDER", self.work.overview()["usage_assurance"])
        with self.assertRaisesRegex(MachineError, "BOUNDED_INTEGER"):
            self.work.record_usage(report | {"input_tokens": True})

    def test_engine_policy_and_run_views_use_actual_durable_receipts(self):
        world = json.loads((ROOT / "cases/economic_state_demo.json").read_text())
        source = json.loads((ROOT / "cases/economic_program_demo.json").read_text())
        self.work.runtime.install_state(world)
        self.work.runtime.register_program(source)
        self.work.runtime.evaluate(source["program_id"], at="2026-09-25T12:01:00+00:00")
        view = self.work.overview()
        self.assertEqual(view["programs"][0]["source"], source)
        self.assertEqual(view["runs"][0]["status"], "AWAITING_AUTHORIZATION")
        self.assertTrue(view["runs"][0]["verification"])
        self.assertEqual(view["runtime"]["active_reservations"], 1)

    def test_journal_tampering_blocks_a_connection_mutation(self):
        self.work.connect(WALLET)
        with self.work.runtime.connect() as db:
            db.execute("UPDATE events SET event_hash='tampered'")
        with self.assertRaisesRegex(MachineError, "INTEGRITY_FAILED"):
            self.work.connect(WALLET)
        self.assertEqual(len(self.work.overview()["connections"]), 1)


class LocalEngineHTTP(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        db = Path(self.directory.name) / "engine.sqlite3"
        self.work = Workspace(db, readers=Connectors(FakeHTTP(), clock=lambda: AT), clock=lambda: AT)
        self.client = TestClient(create_engine_app(db, admin_token=OWNER_TOKEN, workspace=self.work))
        self.headers = {"Authorization": "Bearer " + OWNER_TOKEN}

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def test_no_remote_browser_origin_or_unknown_owner_is_admitted(self):
        self.assertEqual(self.client.get("/api/engine/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/engine/overview", headers=self.headers | {"Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/engine/overview", headers=self.headers).status_code, 200)
        self.assertNotIn("access-control-allow-origin", self.client.get("/api/engine/overview", headers=self.headers).headers)

    def test_owner_can_connect_sync_and_disconnect_without_financial_dispatch(self):
        result = self.client.post("/api/engine/connections", json=WALLET, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        cid = result.json()["id"]
        self.assertEqual(self.client.post(f"/api/engine/connections/{cid}/sync", json={}, headers=self.headers).json()["status"], "CONNECTED")
        self.assertEqual(self.client.get("/api/engine/overview", headers=self.headers).json()["assets"][1]["quantity"], "19.99")
        self.assertEqual(self.client.post(f"/api/engine/connections/{cid}/disconnect", json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/engine/execute", json={}, headers=self.headers).status_code, 405)

    def test_body_stream_limit_and_closed_json(self):
        self.assertEqual(self.client.post("/api/engine/connections", content="x" * 200001, headers=self.headers).status_code, 413)
        for data in [[], {}, WALLET | {"private_key": "never-store"}]:
            self.assertEqual(self.client.post("/api/engine/connections", json=data, headers=self.headers).status_code, 409)

    def test_source_and_secrets_are_not_served_as_assets(self):
        for url in ["/.env", "/runtime/engine.sqlite3", "/engine.py", "/api/secrets"]:
            self.assertEqual(self.client.get(url, headers=self.headers).status_code, 404)
        page = self.client.get("/engine")
        self.assertIn('content="SELF_HOSTED"', page.text)
        self.assertNotIn(OWNER_TOKEN, page.text)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        wallet_loader = self.client.get("/wallet-connectors.js")
        self.assertEqual(wallet_loader.status_code, 200)
        self.assertIn("ManagedWallets", wallet_loader.text)

    def test_weak_or_non_loopback_configuration_does_not_start(self):
        with self.assertRaises(ValueError):
            create_engine_app("unused", admin_token="weak")
        with self.assertRaises(ValueError):
            create_engine_app("unused", admin_token=OWNER_TOKEN, origin="http://0.0.0.0:8800")
        for origin in ["http://127.0.0.1:8800.evil", "http://127.0.0.1:8800/path",
                       "http://user@127.0.0.1:8800", "http://127.0.0.1:8800?key=value",
                       "http://127.0.0.1:80"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_engine_app("unused", admin_token=OWNER_TOKEN, origin=origin)

    def test_new_private_database_is_not_world_readable(self):
        self.assertEqual(self.work.runtime.db_path.stat().st_mode & 0o777, 0o600)

    def test_public_demo_is_bound_to_actual_trade_not_fake_live_accounts(self):
        bundle = json.loads((ROOT / "artifacts/submission/completed-trade.json").read_text())
        result = recorded_overview(bundle)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["mode"], "RECORDED")
        self.assertEqual(result["assets"][0]["quantity"], "19.99")
        self.assertEqual(result["payments"][0]["tx_hash"], bundle["payment"]["tx_hash"])
        self.assertEqual(result["trade_bundle_hash"], bundle["bundle_hash"])
        self.assertEqual(result["as_of"], bundle["checked_at"])
        self.assertIsNone(result["assets"][0]["observed_at"])
        self.assertEqual(result["assets"][0]["balance_at_block"], bundle["payment"]["receipt_block"])
        self.assertEqual(result["usage"], [])
        self.assertEqual(result["positions"], [])
        self.assertTrue(result["venue_trades_use_venue_api"])


if __name__ == "__main__":
    unittest.main()
