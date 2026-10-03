"""Observed public market state -> native statistics -> owner-reviewed venue plan.

Market reads are authenticated by HTTPS, not a signed oracle. No market event
dispatches an order. Account state is separately re-read before/after execution.
"""

import json
import re
import secrets
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

from economic_machine.values import MachineError, canonical, digest, ident, require_keys

URL = "https://data-api.binance.vision/api/v3/klines"
MARKETS = {
    'binance-public-market': (URL, 'binance-spot'),
    'binance-public-futures': ('https://fapi.binance.com/fapi/v1/klines', 'binance-usdm'),
    'binance-testnet-market': ('https://testnet.binance.vision/api/v3/klines', 'binance-spot-testnet'),
    'binance-testnet-futures': ('https://demo-fapi.binance.com/fapi/v1/klines', 'binance-usdm-testnet'),
}
SCALE = 1000000


def read_market(http, symbol, now, *, profile='binance-public-market'):
    if profile not in MARKETS:
        raise MachineError('FIXED_MARKET_FEED_REQUIRED')
    url, venue_profile = MARKETS[profile]
    rows = http.request(url, params={"symbol": symbol, "interval": "1m", "limit": 31})
    if not isinstance(rows, list) or not 2 <= len(rows) <= 31:
        raise MachineError("BOUNDED_MARKET_WINDOW_REQUIRED")
    samples = []
    with localcontext() as context:
        context.prec = 80
        for row in rows:
            if not isinstance(row, list) or len(row) < 7 or type(row[0]) is not int or type(row[6]) is not int:
                raise MachineError("INVALID_MARKET_SAMPLE")
            if row[6] >= int(now * 1000):
                continue  # Never use a still-forming candle as a closed observation.
            if row[6] != row[0] + 59999 or row[0] % 60000:
                raise MachineError("INVALID_MARKET_INTERVAL")
            if not isinstance(row[4], str) or re.fullmatch(r"[0-9]{1,20}(\.[0-9]{1,18})?", row[4]) is None:
                raise MachineError("INVALID_MARKET_PRICE")
            price = int((Decimal(row[4]) * SCALE).to_integral_value(rounding=ROUND_HALF_EVEN))
            if not 0 < price < 2**63 or (samples and row[0] * 1000000 != samples[-1]["timestamp_ns"] + 60000000000):
                raise MachineError("MARKET_GAP_OR_PRICE_OVERFLOW")
            samples.append({"timestamp_ns": row[0] * 1000000, "price": price})
    if not 2 <= len(samples) <= 30 or int(now * 1000000000) - samples[-1]["timestamp_ns"] > 180000000000:
        raise MachineError("STALE_OR_INCOMPLETE_MARKET_WINDOW")
    return {"symbol": symbol, "samples": samples, "last_close_price": str(Decimal(samples[-1]["price"]) / SCALE),
            "assurance": "PUBLIC_HTTPS_CLOSED_CANDLES_NOT_SIGNED_ORACLE", "price_rounding": "NEAREST_MICRO_UNIT_HALF_EVEN",
            "window_sha256": digest(samples), "network": profile, "venue_profile": venue_profile}


class LiveDecisions:
    def __init__(self, workspace):
        self.work = workspace
        with self.work.runtime.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS engine_live_watches (id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS engine_live_decisions (id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, "
                       "source_hash TEXT NOT NULL, body TEXT NOT NULL, order_id TEXT UNIQUE, "
                       "UNIQUE(watch_id,source_hash))")

    def policy(self, raw):
        require_keys(raw, {"name", "market_connection_id", "venue_policy_id", "trigger_drawdown_bps", "quantity",
                          "side", "reduce_only", "maximum_slippage_bps", "max_age_seconds"}, "live watch")
        ident(raw["name"], "watch name")
        ident(raw["market_connection_id"], "market connection")
        ident(raw["venue_policy_id"], "venue policy")
        for key in ["trigger_drawdown_bps", "maximum_slippage_bps", "max_age_seconds"]:
            if type(raw[key]) is not int or not 1 <= raw[key] <= (180 if key == "max_age_seconds" else 10000):
                raise MachineError("BOUNDED_LIVE_WATCH_REQUIRED")
        from economic_machine.values import decimal
        if not isinstance(raw["side"], str) or raw["side"] not in {"BUY", "SELL"} or type(raw["reduce_only"]) is not bool or not 0 < decimal(raw["quantity"]) <= 1000000:
            raise MachineError("BOUNDED_LIVE_ORDER_REQUIRED")
        with self.work.runtime.connect() as db:
            source = db.execute("SELECT body,status FROM engine_connections WHERE id=?", (raw["market_connection_id"],)).fetchone()
            policy = db.execute("SELECT body,status FROM engine_trade_policies WHERE id=?", (raw["venue_policy_id"],)).fetchone()
            if not source or source["status"] == "DISCONNECTED" or json.loads(source["body"])["profile"] not in MARKETS:
                raise MachineError("PUBLIC_MARKET_CONNECTION_REQUIRED")
            if not policy or policy["status"] != "ACTIVE":
                raise MachineError("ACTIVE_VENUE_POLICY_REQUIRED")
            venue_policy = json.loads(policy['body'])
            venue = db.execute('SELECT body FROM engine_connections WHERE id=?', (venue_policy['connection_id'],)).fetchone()
            source_profile = json.loads(source['body'])['profile']
            venue_profile = json.loads(venue['body'])['profile'] if venue else None
            if (venue_profile != MARKETS[source_profile][1]
                    or raw['reduce_only'] != venue_profile.startswith('binance-usdm')):
                raise MachineError('MATCHED_FEED_AND_REDUCTION_POLICY_REQUIRED')
            ticker = json.loads(source["body"])["config"]["symbol"]
            if ticker not in json.loads(policy["body"])["symbols"] or raw["side"] not in json.loads(policy["body"])["sides"]:
                raise MachineError("WATCH_OUTSIDE_VENUE_POLICY")
            wid = "watch-" + secrets.token_hex(12)
            db.execute("INSERT INTO engine_live_watches VALUES (?,?,'ACTIVE')", (wid, canonical(raw).decode()))
            self.work.event(db, "LIVE_WATCH_CONFIGURED", {"id": wid, "policy_hash": digest(raw)})
        return {"id": wid, "policy": raw, "execution": "OWNER_APPROVAL_REQUIRED"}

    def evaluate(self, wid):
        with self.work.runtime.connect() as db:
            watch = db.execute("SELECT * FROM engine_live_watches WHERE id=?", (wid,)).fetchone()
            if not watch or watch["status"] != "ACTIVE":
                raise MachineError("ACTIVE_LIVE_WATCH_REQUIRED")
            policy = json.loads(watch["body"])
            row = db.execute("SELECT * FROM engine_connections WHERE id=?", (policy["market_connection_id"],)).fetchone()
            if not row or row["status"] != "CONNECTED" or not row["snapshot"]:
                raise MachineError("CURRENT_MARKET_CONNECTION_REQUIRED")
            source = json.loads(row["snapshot"])
            now = int(self.work.clock())
            if not 0 <= now - source["observed_at"] <= policy["max_age_seconds"]:
                raise MachineError("LIVE_MARKET_STATE_EXPIRED")
            closed_at = source["samples"][-1]["timestamp_ns"] // 1000000000 + 60
            if not 0 <= now - closed_at <= policy["max_age_seconds"]:
                raise MachineError("LIVE_MARKET_CANDLE_EXPIRED")
            source_hash = digest({"window": source["window_sha256"], "connection_version": row["version"]})
            prior = db.execute("SELECT body FROM engine_live_decisions WHERE watch_id=? AND source_hash=?", (wid, source_hash)).fetchone()
            if prior:
                value = json.loads(prior[0])
                if value["expires_at"] <= now:
                    raise MachineError("LIVE_MARKET_STATE_EXPIRED")
                return value
        calculated = self.work.economics.calculate({"operation": "RETURN_STATISTICS", "input": {
            "samples": source["samples"], "count": len(source["samples"]), "expected_interval_ns": 60000000000,
            "interval_tolerance_ns": 0}})
        if not calculated["computed"]:
            raise MachineError("NATIVE_MARKET_STATISTICS_REJECTED")
        triggered = calculated["result"]["maximum_drawdown"] >= policy["trigger_drawdown_bps"] * 100
        body = {"id": "live-decision-" + secrets.token_hex(12), "watch_id": wid, "action": "PLAN" if triggered else "HOLD",
            "source_hash": source_hash, "connection_version": row["version"], "source_observed_at": source["observed_at"],
            "market_window_end_ns": source["samples"][-1]["timestamp_ns"], "policy_hash": digest(policy),
            "symbol": source["symbol"], "reference_price": source["last_close_price"], "native": calculated,
            "expires_at": min(now + policy["max_age_seconds"], closed_at + policy["max_age_seconds"]),
            "execution_authority": "NONE", "language_model_calls": 0,
            "input_assurance": source["assurance"]}
        with self.work.runtime.connect() as db:
            current = db.execute("SELECT version,status,snapshot FROM engine_connections WHERE id=?", (policy["market_connection_id"],)).fetchone()
            if current["version"] != row["version"] or current["status"] != "CONNECTED" or json.loads(current["snapshot"])["window_sha256"] != source["window_sha256"]:
                raise MachineError("MARKET_CHANGED_DURING_NATIVE_DECISION")
            db.execute("INSERT OR IGNORE INTO engine_live_decisions VALUES (?,?,?,?,NULL)", (body["id"], wid, source_hash, canonical(body).decode()))
            saved = json.loads(db.execute("SELECT body FROM engine_live_decisions WHERE watch_id=? AND source_hash=?", (wid, source_hash)).fetchone()[0])
            self.work.event(db, "OBSERVED_NATIVE_DECISION", saved)
        return saved

    def validate(self, db, did):
        row = db.execute("SELECT * FROM engine_live_decisions WHERE id=?", (did,)).fetchone()
        if not row:
            raise MachineError("LIVE_DECISION_REQUIRED")
        decision = json.loads(row["body"])
        watch = db.execute("SELECT * FROM engine_live_watches WHERE id=?", (row["watch_id"],)).fetchone()
        policy = json.loads(watch["body"])
        source = db.execute("SELECT * FROM engine_connections WHERE id=?", (policy["market_connection_id"],)).fetchone()
        if (not source or not source["snapshot"] or watch["status"] != "ACTIVE" or decision["policy_hash"] != digest(policy)
                or decision["expires_at"] <= int(self.work.clock()) or source["status"] != "CONNECTED"
                or source["version"] != decision["connection_version"] or decision["action"] != "PLAN"
                or digest({"window": json.loads(source["snapshot"])["window_sha256"],
                           "connection_version": source["version"]}) != decision["source_hash"]):
            raise MachineError("LIVE_DECISION_AUTHORITY_EXPIRED")
        return row, decision, policy

    def plan(self, did):
        with self.work.runtime.connect() as db:
            row, decision, policy = self.validate(db, did)
            if row["order_id"]:
                existing = row["order_id"]
            else:
                existing = None
        if existing:
            return self.work.trading.order(existing)
        with localcontext() as context:
            context.prec = 80
            offset = Decimal(policy["maximum_slippage_bps"]) / 10000
            price = Decimal(decision["reference_price"]) * (1 - offset if policy["side"] == "SELL" else 1 + offset)
            with self.work.runtime.connect() as db:
                venue_policy = json.loads(db.execute("SELECT body FROM engine_trade_policies WHERE id=?", (policy["venue_policy_id"],)).fetchone()[0])
                _, venue = self.work.trading.connection(db, venue_policy["connection_id"])
            instrument = self.work.trading.broker_factory(venue, clock=self.work.clock).instrument(decision["symbol"])
            tick = Decimal(instrument["filters"]["PRICE_FILTER"]["tickSize"])
            if tick > 0:
                price = (price / tick).to_integral_value(rounding=ROUND_CEILING if policy["side"] == "SELL" else ROUND_FLOOR) * tick
        order = self.work.trading.plan({"request_id": did, "policy_id": policy["venue_policy_id"], "symbol": decision["symbol"],
            "side": policy["side"], "quantity": policy["quantity"], "price": str(price), "time_in_force": "IOC", "reduce_only": policy["reduce_only"]})
        with self.work.runtime.connect() as db:
            self.validate(db, did)
            db.execute("UPDATE engine_live_decisions SET order_id=? WHERE id=?", (order["id"], did))
            self.work.event(db, "LIVE_DECISION_PLAN_LINKED", {"decision_id": did, "order_id": order["id"], "plan_hash": order["plan_hash"]})
        return order

    def verify_order(self, db, oid):
        # An interruption between plan creation and link commit cannot remove
        # the source guard: request_id is already durable on the venue order.
        row = db.execute("SELECT d.id FROM engine_live_decisions d JOIN engine_trade_orders o "
                         "ON d.id=o.request_id WHERE o.id=?", (oid,)).fetchone()
        if row:
            self.validate(db, row[0])

    def on_sync(self, cid):
        with self.work.runtime.connect() as db:
            watches = [r[0] for r in db.execute("SELECT id FROM engine_live_watches WHERE status='ACTIVE' ORDER BY id LIMIT 32")
                       if json.loads(db.execute("SELECT body FROM engine_live_watches WHERE id=?", (r[0],)).fetchone()[0])["market_connection_id"] == cid]
        for wid in watches:
            self.evaluate(wid)

    def status(self):
        with self.work.runtime.connect() as db:
            return {"watches": [dict(r) | {"body": json.loads(r["body"])} for r in db.execute("SELECT * FROM engine_live_watches LIMIT 32")],
                    "decisions": [json.loads(r[0]) for r in db.execute("SELECT body FROM engine_live_decisions ORDER BY rowid DESC LIMIT 32")],
                    "automatic_dispatch": False}
