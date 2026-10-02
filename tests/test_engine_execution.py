"""Execution ports, authority boundaries, restart recovery and durable sync."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from economic_machine.values import MachineError
from machine_commerce.access import Access, Principal
from machine_commerce.api import create_app
from machine_engine.api import create_engine_app
from machine_engine.broker import BinanceBroker, VenueHTTP, VenueRejected, VenueUnavailable
from machine_engine.connections import Connectors, normalize_connection
from machine_engine.routes import HostedWorkspaces
from machine_engine.workspace import Workspace

SPOT = {
    "name": "Spot",
    "profile": "binance-spot",
    "config": {"api_key_env": "VENUE_KEY", "api_secret_env": "VENUE_SECRET"},
}
FUTURES = SPOT | {"profile": "binance-usdm"}
INSTRUMENT = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "contractType": "PERPETUAL",
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "1000000", "tickSize": "0.1"},
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
    ],
}


class VenueFixture:
    def __init__(self):
        self.calls, self.submitted, self.result, self.failure = [], None, None, None
        self.balance, self.position, self.hedge, self.can_trade = "1000", "1", False, True

    def response(self, params, status="NEW", filled="0", quote="0"):
        return {
            "symbol": params["symbol"],
            "side": params["side"],
            "type": "LIMIT",
            "timeInForce": params["timeInForce"],
            "origQty": params["quantity"],
            "price": params["price"],
            "clientOrderId": params["newClientOrderId"],
            "orderId": 123,
            "status": status,
            "executedQty": filled,
            "cummulativeQuoteQty": quote,
            "cumQuote": quote,
            "reduceOnly": True,
            "positionSide": "BOTH",
        }

    def request(self, url, **options):
        self.calls.append((url, options))
        if url.endswith("exchangeInfo"):
            return {"symbols": [INSTRUMENT]}
        if url.endswith("/dual"):
            return {"dualSidePosition": self.hedge}
        if url.endswith("/positionRisk"):
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": self.position,
                    "entryPrice": "100",
                    "markPrice": "110",
                    "liquidationPrice": "80",
                    "unRealizedProfit": "10",
                    "notional": "110",
                    "marginAsset": "USDT",
                    "maintMargin": "1",
                    "updateTime": 100000,
                }
            ]
        if url.endswith("/account"):
            if "/fapi/" in url:
                return {
                    "assets": [
                        {
                            "asset": "USDT",
                            "walletBalance": "100",
                            "availableBalance": "90",
                            "unrealizedProfit": "10",
                        }
                    ]
                }
            return {
                "canTrade": self.can_trade,
                "balances": [
                    {"asset": "USDT", "free": self.balance, "locked": "0"},
                    {"asset": "BTC", "free": "1", "locked": "0"},
                ],
            }
        if url.endswith("/openOrders"):
            return []
        if options.get("method") == "POST":
            self.submitted = dict(options["params"])
            if self.failure:
                raise self.failure
            return self.response(self.submitted)
        if options.get("method") == "DELETE":
            if self.failure:
                raise self.failure
            return self.response(self.submitted, "CANCELED")
        if self.failure:
            raise self.failure
        return self.result or self.response(self.submitted)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.at, self.http = 1791046800, VenueFixture()
        self.env = patch.dict(
            os.environ, {"VENUE_KEY": "fixture-exchange-access", "VENUE_SECRET": "fixture-exchange-secret"}
        )
        self.env.start()
        self.work = Workspace(
            Path(self.tmp.name) / "engine.sqlite3",
            clock=lambda: self.at,
            broker_factory=lambda connection, clock: BinanceBroker(connection, http=self.http, clock=clock),
            live_enabled=True,
        )
        self.cid = self.work.connect(SPOT)["id"]
        self.policy = {
            "name": "SPOT_BUYER",
            "connection_id": self.cid,
            "symbols": ["BTCUSDT"],
            "sides": ["BUY", "SELL"],
            "turnover_limit_usdt": "20",
            "max_order_usdt": "10",
            "fee_reserve_bps": "20",
            "expires_at": self.at + 600,
        }
        self.pid = self.work.trading.policy(self.policy)["id"]
        self.request = {
            "request_id": "request-one",
            "policy_id": self.pid,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "0.1",
            "price": "100",
            "time_in_force": "IOC",
            "reduce_only": False,
        }

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def approved(self, raw=None):
        plan = self.work.trading.plan(raw or self.request)
        return self.work.trading.approve(plan["id"], plan["plan_hash"])

    def test_plan_and_approval_never_send_an_order(self):
        plan = self.approved()
        self.assertEqual(plan["status"], "APPROVED")
        self.assertEqual(plan["reserved_usdt"], "0")
        self.assertEqual(self.work.overview()["execution_authority"], "OWNER_PER_ORDER_VENUE_ONLY")
        self.assertFalse(any(o.get("method") == "POST" for _, o in self.http.calls))

    def test_owner_approval_binds_exact_plan_hash(self):
        plan = self.work.trading.plan(self.request)
        with self.assertRaisesRegex(MachineError, "EXACT_PLAN"):
            self.work.trading.approve(plan["id"], "0" * 64)

    def test_live_gate_blocks_before_network_transmission(self):
        order = self.approved()
        self.work.trading.live_enabled = False
        with self.assertRaisesRegex(MachineError, "LIVE_TRANSMISSION_DISABLED"):
            self.work.trading.dispatch(order["id"])
        self.assertIsNone(self.http.submitted)

    def test_one_submission_and_identity_bound_reconciliation(self):
        order = self.approved()
        first = self.work.trading.dispatch(order["id"])
        self.assertEqual((first["status"], first["reserved_usdt"]), ("NEW", "10"))
        with self.assertRaises(MachineError):
            self.work.trading.dispatch(order["id"])
        self.assertEqual(sum(o.get("method") == "POST" for _, o in self.http.calls), 1)
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "9.9")
        result = self.work.trading.reconcile(order["id"])
        self.assertEqual((result["status"], result["reserved_usdt"]), ("FILLED", "0"))
        self.assertEqual(self.work.trading.status()["policies"][0]["spent_usdt"], "9.9")
        self.work.trading.reconcile(order["id"])
        self.assertEqual(self.work.trading.status()["policies"][0]["spent_usdt"], "9.9")

    def test_timeout_retains_hold_and_recovers_after_restart(self):
        order = self.approved()
        self.http.failure = TimeoutError("secret-bearing-error-must-not-leak")
        result = self.work.trading.dispatch(order["id"])
        self.assertEqual((result["status"], result["reserved_usdt"]), ("UNKNOWN", "10"))
        self.http.failure = None
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "10")
        restarted = Workspace(
            self.work.runtime.db_path,
            clock=lambda: self.at,
            broker_factory=lambda connection, clock: BinanceBroker(connection, http=self.http, clock=clock),
            live_enabled=True,
        )
        self.assertEqual(restarted.trading.reconcile(order["id"])["status"], "FILLED")
        self.assertEqual(sum(o.get("method") == "POST" for _, o in self.http.calls), 1)
        with self.work.runtime.connect() as db:
            self.assertNotIn("secret-bearing", str([tuple(r) for r in db.execute("SELECT * FROM events")]))

    def test_partial_fill_retains_full_hold_until_terminal_evidence(self):
        order = self.approved()
        self.work.trading.dispatch(order["id"])
        self.http.result = self.http.response(self.http.submitted, "PARTIALLY_FILLED", "0.04", "4")
        self.assertEqual(self.work.trading.reconcile(order["id"])["reserved_usdt"], "10")
        self.http.result = self.http.response(self.http.submitted, "CANCELED", "0.04", "4")
        self.assertEqual(self.work.trading.reconcile(order["id"])["reserved_usdt"], "0")
        self.assertEqual(self.work.trading.status()["policies"][0]["spent_usdt"], "4")

    def test_mismatched_or_regressing_venue_evidence_keeps_hold(self):
        for mutation in [
            {"clientOrderId": "foreign"},
            {"symbol": "ETHUSDT"},
            {"side": "SELL"},
            {"price": "101"},
            {"origQty": "0.2"},
            {"status": "FILLED", "executedQty": "0"},
            {"executedQty": "1"},
            {"status": "FILLED", "executedQty": "0.1", "cummulativeQuoteQty": "11"},
        ]:
            with self.subTest(mutation=mutation):
                order = self.approved(self.request | {"request_id": "mutation-" + str(len(self.http.calls))})
                self.work.trading.dispatch(order["id"])
                self.http.result = self.http.response(self.http.submitted) | mutation
                self.assertEqual(self.work.trading.reconcile(order["id"])["status"], "UNKNOWN")
                self.assertEqual(self.work.trading.order(order["id"])["reserved_usdt"], "10")
                self.http.result = self.http.response(self.http.submitted, "CANCELED")
                self.work.trading.reconcile(order["id"])
                self.http.result = None

    def test_changed_filters_require_replan(self):
        order = self.approved()
        with (
            patch.dict(
                INSTRUMENT, {"filters": INSTRUMENT["filters"] + [{"filterType": "EXTRA", "value": "1"}]}
            ),
            self.assertRaisesRegex(MachineError, "INSTRUMENT_CHANGED"),
        ):
            self.work.trading.dispatch(order["id"])

    def test_expired_paused_or_disconnected_order_cannot_dispatch(self):
        for mode in ["expiry", "pause", "disconnect"]:
            with self.subTest(mode=mode):
                order = self.approved(self.request | {"request_id": mode})
                if mode == "expiry":
                    self.at += 61
                elif mode == "pause":
                    self.work.trading.pause(self.pid)
                else:
                    self.work.disconnect(self.cid)
                with self.assertRaises(MachineError):
                    self.work.trading.dispatch(order["id"])
                if mode == "pause":
                    with self.work.runtime.connect() as db:
                        db.execute("UPDATE engine_trade_policies SET status='ACTIVE' WHERE id=?", (self.pid,))

    def test_shared_turnover_budget_and_one_inflight_order_per_account(self):
        first = self.approved()
        self.work.trading.dispatch(first["id"])
        second = self.approved(self.request | {"request_id": "second"})
        with self.assertRaisesRegex(MachineError, "RECONCILIATION_REQUIRED"):
            self.work.trading.dispatch(second["id"])
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "10")
        self.work.trading.reconcile(first["id"])
        self.work.trading.dispatch(second["id"])
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "10")
        self.work.trading.reconcile(second["id"])
        third = self.approved(self.request | {"request_id": "third"})
        with self.assertRaisesRegex(MachineError, "SHARED_TURNOVER"):
            self.work.trading.dispatch(third["id"])

    def test_request_id_reuse_is_exact(self):
        first = self.work.trading.plan(self.request)
        self.assertEqual(self.work.trading.plan(self.request)["id"], first["id"])
        with self.assertRaisesRegex(MachineError, "IDEMPOTENCY_CONFLICT"):
            self.work.trading.plan(self.request | {"quantity": "0.01"})

    def test_minimum_tick_step_finite_and_order_size_checks(self):
        for change in [
            {"quantity": "0.0001"},
            {"quantity": "0.01"},
            {"price": "100.01"},
            {"quantity": "0.2"},
            {"price": "NaN"},
            {"price": 100.0},
            {"quantity": "-0.1"},
            {"reduce_only": True},
            {"side": []},
        ]:
            with self.subTest(change=change), self.assertRaises(MachineError):
                self.work.trading.plan(self.request | change)

    def test_fresh_account_recheck_blocks_insufficient_balance(self):
        order = self.approved()
        self.http.balance = "0"
        with self.assertRaisesRegex(MachineError, "INSUFFICIENT"):
            self.work.trading.dispatch(order["id"])
        self.assertIsNone(self.http.submitted)

    def test_explicit_rejection_releases_hold_without_fake_fill(self):
        order = self.approved()
        self.http.failure = VenueRejected("VENUE_ORDER_REJECTED")
        result = self.work.trading.dispatch(order["id"])
        self.assertEqual(
            (result["status"], result["reserved_usdt"], result["filled_quantity"]), ("REJECTED", "0", "0")
        )

    def test_cancel_timeout_retains_reservation_and_query_is_read_only(self):
        order = self.approved()
        self.work.trading.dispatch(order["id"])
        self.http.failure = TimeoutError()
        result = self.work.trading.cancel(order["id"])
        self.assertEqual((result["status"], result["reserved_usdt"]), ("UNKNOWN", "10"))
        self.http.failure = None
        self.http.result = self.http.response(self.http.submitted, "CANCELED")
        self.assertEqual(self.work.trading.reconcile(order["id"])["reserved_usdt"], "0")

    def test_disconnect_preserves_read_reconciliation(self):
        order = self.approved()
        self.work.trading.dispatch(order["id"])
        self.work.disconnect(self.cid)
        self.http.result = self.http.response(self.http.submitted, "FILLED", "0.1", "10")
        self.assertEqual(self.work.trading.reconcile(order["id"])["status"], "FILLED")

    def test_recovery_respects_backoff_and_rate_limit_without_retransmission(self):
        order = self.approved()
        self.http.failure = VenueUnavailable(120)
        self.work.trading.dispatch(order["id"])
        self.assertEqual(self.work.trading.pending_ids(), [])
        self.at += 119
        self.assertEqual(self.work.trading.pending_ids(), [])
        self.at += 1
        self.assertEqual(self.work.trading.pending_ids(), [order["id"]])
        self.work.trading.reconcile(order["id"])
        self.assertEqual(self.work.trading.pending_ids(), [])
        self.assertEqual(self.work.trading.order(order["id"])["reserved_usdt"], "10")
        self.assertEqual(sum(options.get("method") == "POST" for _, options in self.http.calls), 1)

    def test_expired_unsent_plan_is_visible_and_holds_no_capital(self):
        order = self.approved()
        self.at += 61
        self.assertEqual(self.work.trading.order(order["id"])["status"], "EXPIRED_UNSENT")
        self.assertEqual(self.work.trading.order(order["id"])["reserved_usdt"], "0")

    def test_futures_only_reduce_existing_one_way_position(self):
        broker = BinanceBroker(normalize_connection(FUTURES), http=self.http, clock=lambda: self.at)
        instruction = self.request | {"side": "SELL", "reduce_only": True, "fee_reserve_bps": "20"}
        broker.guard_account(instruction, broker.instrument("BTCUSDT"))
        for change in [{"side": "BUY"}, {"quantity": "2"}, {"reduce_only": False}]:
            with self.assertRaises(MachineError):
                broker.guard_account(instruction | change, broker.instrument("BTCUSDT"))
        self.http.hedge = True
        with self.assertRaisesRegex(MachineError, "ONE_WAY"):
            broker.guard_account(instruction, broker.instrument("BTCUSDT"))

    def test_derivatives_reader_preserves_signed_exposure_and_native_pnl(self):
        self.http.position = "-1"
        data = Connectors(self.http, clock=lambda: self.at).read(normalize_connection(FUTURES))
        self.assertEqual(data["positions"][0]["quantity"], "-1")
        self.assertEqual(data["positions"][0]["margin_asset"], "USDT")
        self.assertEqual(data["positions"][0]["liquidation_price"], "80")
        self.assertTrue(data["read_only"])
        self.assertTrue(all(options.get("method", "GET") == "GET" for _, options in self.http.calls))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.at = 1000
        self.calls = 0
        self.fail = False
        test = self

        class Reader:
            def read(self, body):
                test.calls += 1
                if test.fail:
                    raise MachineError("REMOTE_READ_REJECTED")
                return {
                    "assets": [],
                    "positions": [],
                    "orders": [],
                    "observed_at": test.at,
                    "source_hash": "abc",
                }

        self.work = Workspace(Path(self.tmp.name) / "jobs.sqlite3", clock=lambda: self.at, readers=Reader())
        self.cid = self.work.connect(SPOT)["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_schedule_runs_persists_and_resumes_after_restart(self):
        self.work.scheduler.configure(self.cid, enabled=True, interval_seconds=30)
        self.assertTrue(self.work.scheduler.run_once())
        self.assertFalse(self.work.scheduler.run_once())
        resumed = Workspace(self.work.runtime.db_path, readers=self.work.readers, clock=lambda: self.at)
        self.at += 30
        self.assertTrue(resumed.scheduler.run_once())
        self.assertEqual(self.calls, 2)

    def test_exclusive_lease_and_crash_expiry(self):
        self.work.scheduler.configure(self.cid, enabled=True, interval_seconds=15)
        with self.work.runtime.connect() as db:
            db.execute("UPDATE engine_sync_jobs SET lease_token='dead-worker',lease_until=1100")
        self.assertFalse(self.work.scheduler.run_once())
        self.at = 1100
        self.assertTrue(self.work.scheduler.run_once())

    def test_failure_backoff_and_success_reset(self):
        self.work.scheduler.configure(self.cid, enabled=True, interval_seconds=15)
        self.fail = True
        self.work.scheduler.run_once()
        self.assertEqual(self.work.scheduler.status()[0]["next_run"], 1030)
        self.at = 1030
        self.work.scheduler.run_once()
        self.assertEqual(self.work.scheduler.status()[0]["next_run"], 1090)
        self.at, self.fail = 1090, False
        self.work.scheduler.run_once()
        self.assertEqual(self.work.scheduler.status()[0]["failures"], 0)
        self.assertEqual(self.work.scheduler.status()[0]["next_run"], 1105)

    def test_disconnect_disables_durable_job(self):
        self.work.scheduler.configure(self.cid, enabled=True, interval_seconds=15)
        self.work.disconnect(self.cid)
        self.assertFalse(self.work.scheduler.run_once())
        self.assertEqual(self.work.scheduler.status()[0]["enabled"], 0)

    def test_schedule_bounds_and_types(self):
        for interval in [0, 14, 3601, True, "30"]:
            with self.assertRaises(MachineError):
                self.work.scheduler.configure(self.cid, enabled=True, interval_seconds=interval)


class IntegrationTests(unittest.TestCase):
    def test_stable_owner_namespace_and_cross_owner_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = HostedWorkspaces(directory, lambda: 1000)
            first, other = registry.get("wallet:421614:alice"), registry.get("wallet:421614:bob")
            with self.assertRaisesRegex(MachineError, "OWNER_CREDENTIAL_NAMESPACE"):
                first.connect(SPOT)
            config = {key: first.credential_prefix + key.upper() for key in SPOT["config"]}
            first.connect(SPOT | {"config": config})
            self.assertEqual(len(registry.get("wallet:421614:alice").overview()["connections"]), 1)
            self.assertEqual(other.overview()["connections"], [])
            self.assertEqual(Path(first.runtime.db_path).stat().st_mode & 0o777, 0o600)

    def test_existing_keys_never_gain_engine_or_trade_approval_authority(self):
        read = Principal("owner", "agent", frozenset({"read"}))
        for path in ["/api/engine/overview", "/api/engine/trade/orders/order-one/dispatch"]:
            with self.assertRaises(PermissionError):
                Access.authorize(read, "GET" if path.endswith("overview") else "POST", path)
        writer = Principal("owner", "agent", frozenset({"engine:write"}))
        Access.authorize(writer, "POST", "/api/engine/trade/orders")
        for path in [
            "/api/engine/trade/orders/order-one/approve",
            "/api/engine/trade/orders/order-one/dispatch",
            "/api/engine/connections",
            "/api/engine/trade/policies",
        ]:
            with self.assertRaises(PermissionError):
                Access.authorize(writer, "POST", path)

    def test_hosted_routes_share_login_and_isolate_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "commerce.sqlite3", clock=lambda: 1000)
            alice, bob = TestClient(app), TestClient(app)
            alice.post("/api/sessions", json={})
            bob.post("/api/sessions", json={})
            self.assertEqual(alice.get("/api/engine/overview").status_code, 200)
            first = alice.get("/api/engine/profiles").json()["credential_namespace"]
            second = bob.get("/api/engine/profiles").json()["credential_namespace"]
            self.assertNotEqual(first, second)
            self.assertEqual(alice.post("/api/engine/connections", json=SPOT).status_code, 409)
            config = {key: first + key.upper() for key in SPOT["config"]}
            self.assertEqual(
                alice.post("/api/engine/connections", json=SPOT | {"config": config}).status_code, 200
            )
            self.assertEqual(bob.get("/api/engine/overview").json()["connections"], [])
            self.assertFalse(alice.get("/api/engine/overview").json()["trading"]["live_transmission_enabled"])
            alice.close()
            bob.close()

    def test_local_console_and_schedule_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_engine_app(Path(directory) / "local.sqlite3", admin_token="x" * 40)
            client = TestClient(app)
            self.assertIn("operations.js", client.get("/console").text)
            self.assertEqual(
                client.post("/api/engine/connections/foreign/schedule", json={}).status_code, 401
            )
            for path in ["operations.js", "operations.css"]:
                self.assertEqual(client.get("/" + path).status_code, 200)
            client.close()


class TransportTests(unittest.TestCase):
    def call(self, url, *, status=200, payload=None, method="GET"):
        from machine_commerce.transport import PinnedTransport

        delegate = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))

        def pin(urls, delegate):
            return PinnedTransport(
                urls,
                resolver=lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 443))],
                delegate=delegate,
            )

        with (
            patch("machine_engine.broker.PinnedTransport", side_effect=pin),
            patch("httpx.HTTPTransport", return_value=delegate),
        ):
            return VenueHTTP().request(url, method=method)

    def test_futures_catalogue_has_a_bounded_exception_to_the_small_read_limit(self):
        payload = {"catalogue": "x" * 600000}
        self.assertEqual(
            len(self.call("https://fapi.binance.com/fapi/v1/exchangeInfo", payload=payload)["catalogue"]),
            600000,
        )
        for url in ["https://fapi.binance.com/fapi/v1/order", "https://api.binance.com/api/v3/exchangeInfo"]:
            with self.assertRaisesRegex(MachineError, "TOO_LARGE"):
                self.call(url, payload=payload)
        with self.assertRaisesRegex(MachineError, "TOO_LARGE"):
            self.call("https://fapi.binance.com/fapi/v1/exchangeInfo", payload={"catalogue": "x" * 4000001})

    def test_only_explicit_rejection_can_release_an_order_reservation(self):
        for status, code in [
            (500, -1007),
            (400, -1007),
            (429, -1003),
            (400, -2013),
            (400, -2010),
            (403, -1100),
        ]:
            with (
                self.subTest(status=status, code=code),
                self.assertRaisesRegex(MachineError, "OUTCOME_UNKNOWN"),
            ):
                self.call(
                    "https://api.binance.com/api/v3/order",
                    status=status,
                    payload={"code": code},
                    method="POST",
                )
        with self.assertRaises(VenueRejected):
            self.call(
                "https://api.binance.com/api/v3/order", status=400, payload={"code": -1013}, method="POST"
            )

    def test_signed_writes_keep_pinned_host_tls_identity_and_query(self):
        from machine_commerce.transport import PinnedTransport

        seen = []
        delegate = httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200, json={"ok": True}))[1]
        )

        def pin(urls, delegate):
            return PinnedTransport(
                urls,
                resolver=lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 443))],
                delegate=delegate,
            )

        with (
            patch("machine_engine.broker.PinnedTransport", side_effect=pin),
            patch("httpx.HTTPTransport", return_value=delegate),
        ):
            VenueHTTP().request(
                "https://api.binance.com/api/v3/order",
                method="POST",
                params={"symbol": "BTCUSDT", "signature": "fixture-query"},
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["Host"], "api.binance.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "api.binance.com")
        self.assertEqual(seen[0].url.params["signature"], "fixture-query")
        self.assertEqual(seen[0].method, "POST")


if __name__ == "__main__":
    unittest.main()
