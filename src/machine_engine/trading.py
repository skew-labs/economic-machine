"""Hash-bound approvals, durable turnover reservations and ambiguous-order recovery."""

import json
import os
import secrets
from decimal import localcontext

from economic_machine.values import MachineError, canonical, decimal, decstr, digest, ident, require_keys

from .broker import VenueRejected, broker_for, symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_trade_policies (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL, spent TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS engine_trade_orders (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
 policy_id TEXT NOT NULL, connection_id TEXT NOT NULL, connection_version INTEGER NOT NULL,
 plan TEXT NOT NULL, plan_hash TEXT NOT NULL, status TEXT NOT NULL,
 reserved TEXT NOT NULL, filled_quantity TEXT NOT NULL, filled_quote TEXT NOT NULL,
 venue_order_id TEXT, error_code TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engine_trade_recovery (
 order_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, next_run INTEGER NOT NULL);
"""
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
ACTIVE = {"TRANSMITTING", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "CANCEL_PENDING"}


class Trading:
    def __init__(self, workspace, *, broker_factory=broker_for, live_enabled=None):
        self.work, self.broker_factory = workspace, broker_factory
        self.live_enabled = (
            (os.environ.get("ENGINE_ALLOW_LIVE_TRADING") == "1") if live_enabled is None else live_enabled
        )
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def connection(self, db, cid, *, recovery=False):
        row = db.execute("SELECT * FROM engine_connections WHERE id=?", (cid,)).fetchone()
        if not row or (row["status"] == "DISCONNECTED" and not recovery):
            raise MachineError("ACTIVE_CONNECTION_REQUIRED")
        return row, json.loads(row["body"])

    def policy(self, raw):
        require_keys(
            raw,
            {
                "name",
                "connection_id",
                "symbols",
                "sides",
                "turnover_limit_usdt",
                "max_order_usdt",
                "fee_reserve_bps",
                "expires_at",
            },
            "venue policy",
        )
        ident(raw["name"], "policy name")
        ident(raw["connection_id"], "connection ID")
        if (
            not isinstance(raw["symbols"], list)
            or not 1 <= len(raw["symbols"]) <= 20
            or any(not isinstance(s, str) for s in raw["symbols"])
            or len(set(raw["symbols"])) != len(raw["symbols"])
        ):
            raise MachineError("BOUNDED_UNIQUE_SYMBOLS_REQUIRED")
        for ticker in raw["symbols"]:
            symbol(ticker)
        if (
            not isinstance(raw["sides"], list)
            or not raw["sides"]
            or any(not isinstance(s, str) or s not in {"BUY", "SELL"} for s in raw["sides"])
            or len(set(raw["sides"])) != len(raw["sides"])
        ):
            raise MachineError("SUPPORTED_UNIQUE_SIDES_REQUIRED")
        budget, maximum, fee = (
            decimal(raw["turnover_limit_usdt"]),
            decimal(raw["max_order_usdt"]),
            decimal(raw["fee_reserve_bps"]),
        )
        if not 0 < maximum <= budget <= 100000000 or not 0 <= fee <= 1000:
            raise MachineError("BOUNDED_TURNOVER_POLICY_REQUIRED")
        at = int(self.work.clock())
        if type(raw["expires_at"]) is not int or not at < raw["expires_at"] <= at + 86400 * 30:
            raise MachineError("BOUNDED_POLICY_EXPIRY_REQUIRED")
        pid = "venue-policy-" + secrets.token_hex(12)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, body = self.connection(db, raw["connection_id"])
            if body["profile"] not in {"binance-spot", "binance-usdm"}:
                raise MachineError("EXECUTION_ADAPTER_UNAVAILABLE")
            db.execute(
                "INSERT INTO engine_trade_policies VALUES (?,?,'ACTIVE','0')", (pid, canonical(raw).decode())
            )
            self.work.event(db, "VENUE_POLICY_CREATED", {"id": pid, "policy_hash": digest(raw)})
        return {"id": pid, "policy": raw, "policy_hash": digest(raw), "approval": "OWNER_PER_ORDER"}

    def pause(self, pid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("UPDATE engine_trade_policies SET status='PAUSED' WHERE id=?", (pid,)).rowcount
                != 1
            ):
                raise MachineError("VENUE_POLICY_NOT_FOUND")
            self.work.event(db, "VENUE_POLICY_PAUSED", {"id": pid})
        return {"id": pid, "status": "PAUSED", "existing_orders": "RECONCILE_OR_CANCEL"}

    def plan(self, raw):
        require_keys(
            raw,
            {
                "request_id",
                "policy_id",
                "symbol",
                "side",
                "quantity",
                "price",
                "time_in_force",
                "reduce_only",
            },
            "limit order",
        )
        for key in ["request_id", "policy_id"]:
            ident(raw[key], key)
        symbol(raw["symbol"])
        if (
            not isinstance(raw["side"], str)
            or raw["side"] not in {"BUY", "SELL"}
            or not isinstance(raw["time_in_force"], str)
            or raw["time_in_force"] not in {"GTC", "IOC"}
            or type(raw["reduce_only"]) is not bool
        ):
            raise MachineError("BOUNDED_LIMIT_ORDER_REQUIRED")
        decimal(raw["quantity"])
        decimal(raw["price"])
        fingerprint, at = digest(raw), int(self.work.clock())
        with self.work.runtime.connect() as db:
            previous = db.execute(
                "SELECT * FROM engine_trade_orders WHERE request_id=?", (raw["request_id"],)
            ).fetchone()
            if previous:
                if previous["input_hash"] != fingerprint:
                    raise MachineError("ORDER_IDEMPOTENCY_CONFLICT")
                return self.public(previous)
            policy_row = db.execute(
                "SELECT * FROM engine_trade_policies WHERE id=?", (raw["policy_id"],)
            ).fetchone()
            if not policy_row:
                raise MachineError("VENUE_POLICY_NOT_FOUND")
            policy = json.loads(policy_row["body"])
            connection_row, connection = self.connection(db, policy["connection_id"])
            if policy_row["status"] != "ACTIVE" or policy["expires_at"] <= at:
                raise MachineError("ACTIVE_VENUE_POLICY_REQUIRED")
            if raw["symbol"] not in policy["symbols"] or raw["side"] not in policy["sides"]:
                raise MachineError("ORDER_OUTSIDE_POLICY")
            if (connection["profile"] == "binance-usdm" and not raw["reduce_only"]) or (
                connection["profile"] == "binance-spot" and raw["reduce_only"]
            ):
                raise MachineError("PROFILE_REDUCTION_MODE_MISMATCH")
            version = connection_row["version"]
        broker = self.broker_factory(connection, clock=self.work.clock)
        instrument = broker.instrument(raw["symbol"])
        notional = broker.validate(raw, instrument)
        if decimal(notional) > decimal(policy["max_order_usdt"]):
            raise MachineError("PER_ORDER_TURNOVER_LIMIT")
        broker.guard_account(raw | {"fee_reserve_bps": policy["fee_reserve_bps"]}, instrument)
        oid = "venue-order-" + secrets.token_hex(12)
        plan = raw | {
            "connection_id": policy["connection_id"],
            "profile": connection["profile"],
            "policy_hash": digest(policy),
            "instrument_hash": digest(instrument),
            "notional_usdt": notional,
            "fee_reserve_bps": policy["fee_reserve_bps"],
            "client_order_id": "em-" + secrets.token_hex(12),
            "expires_at": at + 60,
        }
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current, _ = self.connection(db, policy["connection_id"])
            if current["version"] != version:
                raise MachineError("CONNECTION_CHANGED_DURING_PLAN")
            previous = db.execute(
                "SELECT * FROM engine_trade_orders WHERE request_id=?", (raw["request_id"],)
            ).fetchone()
            if previous:
                if previous["input_hash"] != fingerprint:
                    raise MachineError("ORDER_IDEMPOTENCY_CONFLICT")
                return self.public(previous)
            db.execute(
                "INSERT INTO engine_trade_orders VALUES (?,?,?,?,?,?,?,?,'AWAITING_APPROVAL','0','0','0',NULL,NULL,?,?)",
                (
                    oid,
                    raw["request_id"],
                    fingerprint,
                    raw["policy_id"],
                    policy["connection_id"],
                    version,
                    canonical(plan).decode(),
                    digest(plan),
                    at,
                    at,
                ),
            )
            self.work.event(db, "VENUE_PLAN_CREATED", {"id": oid, "plan_hash": digest(plan)})
            return self.public(db.execute("SELECT * FROM engine_trade_orders WHERE id=?", (oid,)).fetchone())

    def approve(self, oid, plan_hash):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, oid)
            if row["plan_hash"] != plan_hash or digest(json.loads(row["plan"])) != plan_hash:
                raise MachineError("EXACT_PLAN_APPROVAL_REQUIRED")
            if row["status"] != "AWAITING_APPROVAL" or json.loads(row["plan"])["expires_at"] <= int(
                self.work.clock()
            ):
                raise MachineError("UNEXPIRED_PENDING_PLAN_REQUIRED")
            db.execute(
                "UPDATE engine_trade_orders SET status='APPROVED',updated=? WHERE id=?",
                (int(self.work.clock()), oid),
            )
            self.work.event(db, "VENUE_PLAN_OWNER_APPROVED", {"id": oid, "plan_hash": plan_hash})
        return self.order(oid)

    def dispatch(self, oid):
        if self.live_enabled is not True:
            raise MachineError("LIVE_TRANSMISSION_DISABLED")
        with self.work.runtime.connect() as db:
            row = self.get(db, oid)
            if row["status"] != "APPROVED":
                raise MachineError("APPROVED_UNSENT_ORDER_REQUIRED")
            _, connection = self.connection(db, row["connection_id"])
            plan = json.loads(row["plan"])
        broker = self.broker_factory(connection, clock=self.work.clock)
        instrument = broker.instrument(plan["symbol"])
        if (
            digest(instrument) != plan["instrument_hash"]
            or broker.validate(plan, instrument) != plan["notional_usdt"]
        ):
            raise MachineError("INSTRUMENT_CHANGED_REPLAN_REQUIRED")
        broker.guard_account(plan, instrument)
        with self.work.runtime.connect() as db, localcontext() as context:
            context.prec = 180
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, oid)
            current, _ = self.connection(db, row["connection_id"])
            policy_row = db.execute(
                "SELECT * FROM engine_trade_policies WHERE id=?", (row["policy_id"],)
            ).fetchone()
            policy, at = json.loads(policy_row["body"]), int(self.work.clock())
            if (
                row["status"] != "APPROVED"
                or current["version"] != row["connection_version"]
                or plan["expires_at"] <= at
                or policy["expires_at"] <= at
                or policy_row["status"] != "ACTIVE"
                or digest(plan) != row["plan_hash"]
                or digest(policy) != plan["policy_hash"]
            ):
                raise MachineError("ORDER_AUTHORITY_CHANGED_OR_EXPIRED")
            if db.execute(
                "SELECT 1 FROM engine_trade_orders WHERE connection_id=? AND status IN "
                "('TRANSMITTING','UNKNOWN','NEW','PARTIALLY_FILLED','CANCEL_PENDING') LIMIT 1",
                (row["connection_id"],),
            ).fetchone():
                raise MachineError("ACCOUNT_ORDER_RECONCILIATION_REQUIRED")
            held = sum(
                (
                    decimal(r[0])
                    for r in db.execute(
                        "SELECT reserved FROM engine_trade_orders WHERE policy_id=?", (row["policy_id"],)
                    )
                ),
                decimal("0"),
            )
            required = decimal(plan["notional_usdt"])
            if decimal(policy_row["spent"]) + held + required > decimal(policy["turnover_limit_usdt"]):
                raise MachineError("SHARED_TURNOVER_LIMIT")
            db.execute(
                "UPDATE engine_trade_orders SET status='TRANSMITTING',reserved=?,updated=? WHERE id=?",
                (decstr(required), at, oid),
            )
            self.work.event(
                db,
                "VENUE_ORDER_TRANSMITTING",
                {"id": oid, "plan_hash": row["plan_hash"], "reserved": decstr(required)},
            )
        try:
            result = broker.submit(plan)
        except VenueRejected:
            return self.reject(oid)
        except Exception as exc:  # noqa: BLE001 - no secret-bearing transport errors reach the journal.
            return self.unknown(oid, retry_after=getattr(exc, "retry_after", 0))
        try:
            return self.observe(oid, result)
        except (MachineError, KeyError, TypeError, ValueError):
            return self.unknown(oid)

    def get(self, db, oid):
        ident(oid, "order ID")
        row = db.execute("SELECT * FROM engine_trade_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise MachineError("VENUE_ORDER_NOT_FOUND")
        return row

    def public(self, row):
        plan = json.loads(row["plan"])
        visible_status = row["status"]
        if visible_status in {"AWAITING_APPROVAL", "APPROVED"} and plan["expires_at"] <= int(
            self.work.clock()
        ):
            visible_status = "EXPIRED_UNSENT"
        return {
            "id": row["id"],
            "status": visible_status,
            "plan": plan,
            "plan_hash": row["plan_hash"],
            "reserved_usdt": row["reserved"],
            "filled_quantity": row["filled_quantity"],
            "filled_quote_usdt": row["filled_quote"],
            "venue_order_id": row["venue_order_id"],
            "error_code": row["error_code"],
            "updated_at": row["updated"],
            "commission_assurance": "NOT_RECONCILED_FROM_VENUE_TRADE_HISTORY",
            "budget_basis": "GROSS_TURNOVER_NOT_WALLET_BALANCE",
        }

    def order(self, oid):
        with self.work.runtime.connect() as db:
            return self.public(self.get(db, oid))

    def unknown(self, oid, retry_after=0):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, oid)
            if row["status"] not in TERMINAL:
                previous = db.execute(
                    "SELECT failures FROM engine_trade_recovery WHERE order_id=?", (oid,)
                ).fetchone()
                failures = min((previous[0] if previous else 0) + 1, 10)
                delay = min(3600, 15 * 2 ** (failures - 1))
                if type(retry_after) is int and 0 <= retry_after <= 86400:
                    delay = max(delay, retry_after)
                db.execute(
                    "INSERT INTO engine_trade_recovery VALUES (?,?,?) ON CONFLICT(order_id) "
                    "DO UPDATE SET failures=excluded.failures,next_run=excluded.next_run",
                    (oid, failures, int(self.work.clock()) + delay),
                )
                db.execute(
                    "UPDATE engine_trade_orders SET status='UNKNOWN',error_code='VENUE_OUTCOME_UNKNOWN',updated=? WHERE id=?",
                    (int(self.work.clock()), oid),
                )
                self.work.event(db, "VENUE_OUTCOME_UNKNOWN", {"id": oid, "reserved": row["reserved"]})
        return self.order(oid)

    def reject(self, oid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.get(db, oid)["status"] == "TRANSMITTING":
                db.execute(
                    "UPDATE engine_trade_orders SET status='REJECTED',reserved='0',error_code='VENUE_ORDER_REJECTED' WHERE id=?",
                    (oid,),
                )
                self.work.event(db, "VENUE_ORDER_REJECTED", {"id": oid})
        return self.order(oid)

    def observe(self, oid, result):
        with self.work.runtime.connect() as db, localcontext() as context:
            context.prec = 180
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, oid)
            plan = json.loads(row["plan"])
            status = result["status"]
            filled = decimal(result["executedQty"])
            quote = decimal(
                result["cumQuote"] if plan["profile"] == "binance-usdm" else result["cummulativeQuoteQty"]
            )
            if (
                status not in TERMINAL | {"NEW", "PARTIALLY_FILLED"}
                or row["status"] not in ACTIVE | TERMINAL
                or result["clientOrderId"] != plan["client_order_id"]
                or result["symbol"] != plan["symbol"]
                or result["side"] != plan["side"]
                or result["type"] != "LIMIT"
                or decimal(result["origQty"]) != decimal(plan["quantity"])
                or decimal(result["price"]) != decimal(plan["price"])
                or result["timeInForce"] != plan["time_in_force"]
                or type(result["orderId"]) is not int
                or result["orderId"] <= 0
                or not decimal(row["filled_quantity"]) <= filled <= decimal(plan["quantity"])
                or quote < decimal(row["filled_quote"])
                or (filled == 0) != (quote == 0)
                or (status == "FILLED" and filled != decimal(plan["quantity"]))
            ):
                raise MachineError("VENUE_ORDER_EVIDENCE_MISMATCH")
            if plan["profile"] == "binance-usdm" and (
                result.get("reduceOnly") is not True or result.get("positionSide") != "BOTH"
            ):
                raise MachineError("VENUE_ORDER_EVIDENCE_MISMATCH")
            if filled and (
                (plan["side"] == "BUY" and quote > filled * decimal(plan["price"]))
                or (plan["side"] == "SELL" and quote < filled * decimal(plan["price"]))
            ):
                raise MachineError("VENUE_LIMIT_PRICE_VIOLATION")
            if row["venue_order_id"] is not None and row["venue_order_id"] != str(result["orderId"]):
                raise MachineError("VENUE_ORDER_ID_CHANGED")
            if row["status"] in TERMINAL:
                if (
                    row["status"] != status
                    or decimal(row["filled_quantity"]) != filled
                    or decimal(row["filled_quote"]) != quote
                ):
                    raise MachineError("TERMINAL_ORDER_CONFLICT")
                return self.public(row)
            reserved = row["reserved"]
            if status in TERMINAL:
                policy = db.execute(
                    "SELECT * FROM engine_trade_policies WHERE id=?", (row["policy_id"],)
                ).fetchone()
                spent = decimal(policy["spent"]) + quote
                breach = spent > decimal(
                    json.loads(policy["body"])["turnover_limit_usdt"]
                ) or quote > decimal(plan["notional_usdt"])
                db.execute(
                    "UPDATE engine_trade_policies SET spent=?,status=? WHERE id=?",
                    (decstr(spent), "BREACHED" if breach else policy["status"], row["policy_id"]),
                )
                reserved = "0"
            db.execute(
                "UPDATE engine_trade_orders SET status=?,reserved=?,filled_quantity=?,filled_quote=?,venue_order_id=?,error_code=NULL,updated=? WHERE id=?",
                (
                    status,
                    reserved,
                    decstr(filled),
                    decstr(quote),
                    str(result["orderId"]),
                    int(self.work.clock()),
                    oid,
                ),
            )
            db.execute(
                "INSERT INTO engine_trade_recovery VALUES (?,0,?) ON CONFLICT(order_id) "
                "DO UPDATE SET failures=0,next_run=excluded.next_run",
                (oid, int(self.work.clock()) + 15),
            )
            self.work.event(
                db,
                "VENUE_ORDER_OBSERVED",
                {
                    "id": oid,
                    "status": status,
                    "filled_quantity": decstr(filled),
                    "filled_quote": decstr(quote),
                    "evidence_hash": digest(result),
                },
            )
        return self.order(oid)

    def reconcile(self, oid):
        with self.work.runtime.connect() as db:
            row = self.get(db, oid)
            if row["status"] not in ACTIVE:
                return self.public(row)
            _, body = self.connection(db, row["connection_id"], recovery=True)
            plan = json.loads(row["plan"])
        try:
            return self.observe(oid, self.broker_factory(body, clock=self.work.clock).query(plan))
        except Exception as exc:  # noqa: BLE001 - query absence is not proof of no execution.
            return self.unknown(oid, retry_after=getattr(exc, "retry_after", 0))

    def cancel(self, oid):
        if self.live_enabled is not True:
            raise MachineError("LIVE_TRANSMISSION_DISABLED")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, oid)
            if row["status"] not in {"NEW", "PARTIALLY_FILLED", "UNKNOWN"}:
                raise MachineError("CANCELABLE_ORDER_REQUIRED")
            _, body = self.connection(db, row["connection_id"], recovery=True)
            plan = json.loads(row["plan"])
            db.execute("UPDATE engine_trade_orders SET status='CANCEL_PENDING' WHERE id=?", (oid,))
            self.work.event(db, "VENUE_CANCEL_REQUESTED", {"id": oid})
        try:
            result = self.broker_factory(body, clock=self.work.clock).cancel(plan)
            return self.observe(oid, result)
        except Exception:  # noqa: BLE001 - uncertain cancellation retains the original hold.
            return self.unknown(oid)

    def status(self):
        with self.work.runtime.connect() as db, localcontext() as context:
            context.prec = 180
            policies = []
            for row in db.execute("SELECT * FROM engine_trade_policies ORDER BY rowid"):
                body = json.loads(row["body"])
                held = sum(
                    (
                        decimal(r[0])
                        for r in db.execute(
                            "SELECT reserved FROM engine_trade_orders WHERE policy_id=?", (row["id"],)
                        )
                    ),
                    decimal("0"),
                )
                policies.append(
                    {
                        "id": row["id"],
                        "policy": body,
                        "status": "EXPIRED"
                        if row["status"] == "ACTIVE" and body["expires_at"] <= int(self.work.clock())
                        else row["status"],
                        "spent_usdt": row["spent"],
                        "reserved_usdt": decstr(held),
                    }
                )
            return {
                "live_transmission_enabled": self.live_enabled is True,
                "approval": "OWNER_PER_ORDER",
                "policies": policies,
                "orders": [
                    self.public(row)
                    for row in db.execute("SELECT * FROM engine_trade_orders ORDER BY rowid DESC LIMIT 100")
                ],
            }

    def pending_ids(self, limit=4):
        with self.work.runtime.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT o.id FROM engine_trade_orders o LEFT JOIN engine_trade_recovery r ON r.order_id=o.id WHERE o.status IN "
                    "('TRANSMITTING','UNKNOWN','NEW','PARTIALLY_FILLED','CANCEL_PENDING') AND COALESCE(r.next_run,o.updated+15)<=? "
                    "ORDER BY COALESCE(r.next_run,o.updated+15),o.id LIMIT ?",
                    (int(self.work.clock()), limit),
                )
            ]
