"""Exact, read-only maker economics. Not a signer, order sender or P&L stop.

Pacifica's observed trade receipt P&L already includes its signed fee. Negative
maker fees are rebates: report them separately but never add them to P&L again.
Funding's reported payout remains separate until its balance sign is reconciled.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3

D = Decimal
EXPECTED_MAKER_RATE = D("-0.00005")
HORIZONS = (100, 1000, 5000)


def decimal(value):
    if type(value) is not str or not 0 < len(value) <= 40:
        raise ValueError("decimal string required")
    try:
        result = D(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite() or abs(result) > D("1000000000000") or result.as_tuple().exponent < -18:
        raise ValueError("decimal outside domain")
    return result


def integer(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("positive bounded integer required")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Receipts:
    def __init__(self, path: Path, account: str, start_ms: int, end_ms: int):
        integer(start_ms); integer(end_ms)
        if not isinstance(account, str) or not account or end_ms <= start_ms or end_ms-start_ms > 48*3600*1000:
            raise ValueError("invalid audit scope")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS funding(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS marks(id INTEGER, horizon INTEGER, body TEXT NOT NULL,
                                         PRIMARY KEY(id,horizon));
        """)
        body = canonical({"account": account, "start_ms": start_ms, "end_ms": end_ms})
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO binding VALUES(1,?)", (body,))
        if self.db.execute("SELECT body FROM binding WHERE id=1").fetchone()[0] != body:
            self.db.close()
            raise ValueError("audit scope changed")
        self.start, self.end = start_ms, end_ms

    def close(self):
        self.db.close()

    def _insert(self, table, ident, value):
        assert table in ("fills", "funding")
        body = canonical(value)
        with self.db:
            row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (ident,)).fetchone()
            if row:
                if row[0] != body:
                    raise ValueError("receipt identity changed")
                return False
            self.db.execute(f"INSERT INTO {table} VALUES(?,?)", (ident, body))
        return True

    def trade(self, raw):
        fields = ("history_id", "order_id", "client_order_id", "symbol", "amount", "price", "fee", "pnl", "event_type", "side", "created_at", "cause")
        value = {k: raw[k] for k in fields}
        integer(value["history_id"]); integer(value["order_id"]); integer(value["created_at"])
        if not self.start <= value["created_at"] <= self.end:
            return False
        if value["symbol"] != "SOL" or value["event_type"] not in ("fulfill_maker", "fulfill_taker"):
            raise ValueError("receipt market/event scope")
        if value["side"] not in ("open_long", "close_short", "open_short", "close_long"):
            raise ValueError("receipt side")
        if value["cause"] not in ("normal", "liquidation", "adl", "ADL"):
            raise ValueError("unrecognized trade cause")
        if value["client_order_id"] is not None and (type(value["client_order_id"]) is not str or len(value["client_order_id"]) > 64):
            raise ValueError("client identity")
        amount, price = decimal(value["amount"]), decimal(value["price"])
        if amount <= 0 or price <= 0:
            raise ValueError("nonpositive fill")
        decimal(value["fee"]); decimal(value["pnl"])
        return self._insert("fills", value["history_id"], value)

    def funding(self, raw):
        value = {k: raw[k] for k in ("history_id", "symbol", "side", "amount", "payout", "rate", "created_at")}
        integer(value["history_id"]); integer(value["created_at"])
        if not self.start <= value["created_at"] <= self.end:
            return False
        if value["symbol"] != "SOL" or value["side"] not in ("bid", "ask"):
            raise ValueError("funding market/side")
        if decimal(value["amount"]) <= 0:
            raise ValueError("funding quantity")
        decimal(value["payout"]); decimal(value["rate"])
        return self._insert("funding", value["history_id"], value)

    def mark(self, history_id, horizon_ms, *, market_ms, received_ms, bid, ask):
        integer(history_id); integer(market_ms); integer(received_ms)
        if horizon_ms not in HORIZONS:
            raise ValueError("markout horizon")
        row = self.db.execute("SELECT body FROM fills WHERE id=?", (history_id,)).fetchone()
        if row is None:
            raise ValueError("unknown fill")
        fill = json.loads(row[0]); target = fill["created_at"]+horizon_ms
        if not target <= market_ms <= target+250 or not 0 <= received_ms-market_ms <= 250:
            raise ValueError("markout is not a timely future observation")
        b, a = decimal(bid), decimal(ask)
        if b <= 0 or a <= b:
            raise ValueError("crossed markout book")
        mid = (a+b)/2
        side = 1 if fill["side"] in ("open_long", "close_short") else -1
        mark = side*(mid-decimal(fill["price"]))*decimal(fill["amount"])
        value = {"market_ms": market_ms, "received_ms": received_ms,
                 "price_markout_usd": str(mark), "markout_after_fee_usd": str(mark-decimal(fill["fee"]))}
        with self.db:
            # Freeze the first qualifying future observation; no best-price selection.
            cur = self.db.execute("INSERT OR IGNORE INTO marks VALUES(?,?,?)", (history_id, horizon_ms, canonical(value)))
        return cur.rowcount == 1

    def report(self):
        fills = [json.loads(x[0]) for x in self.db.execute("SELECT body FROM fills ORDER BY id")]
        funding = [json.loads(x[0]) for x in self.db.execute("SELECT body FROM funding ORDER BY id")]
        maker = [x for x in fills if x["event_type"] == "fulfill_maker"]
        total = lambda rows, field: sum((decimal(x[field]) for x in rows), D(0))
        pnl, fees = total(fills, "pnl"), total(fills, "fee")
        rebate = sum((-decimal(x["fee"]) for x in maker if decimal(x["fee"]) < 0), D(0))
        notional = sum((decimal(x["amount"])*decimal(x["price"]) for x in maker), D(0))
        mismatch = sum(abs(decimal(x["fee"])-decimal(x["amount"])*decimal(x["price"])*EXPECTED_MAKER_RATE) > D("0.000001") for x in maker)
        marks = {}
        for horizon in HORIZONS:
            rows = [json.loads(x[0]) for x in self.db.execute("SELECT body FROM marks WHERE horizon=?", (horizon,))]
            marks[str(horizon)] = {"samples": len(rows), "missing": len(fills)-len(rows),
                                  "price_markout_usd": str(total(rows, "price_markout_usd")),
                                  "markout_after_fee_usd": str(total(rows, "markout_after_fee_usd"))}
        return {"fills": len(fills), "maker_fills": len(maker), "taker_fills": len(fills)-len(maker),
                "maker_notional_usd": str(notional), "actual_maker_rebate_usd": str(rebate),
                "fee_rate_mismatches": mismatch, "signed_trade_fee_usd": str(fees),
                "gross_trade_pnl_usd": str(pnl+fees), "reported_trade_pnl_after_fee_usd": str(pnl),
                "funding_records": len(funding), "funding_payout_reported_separately": str(total(funding, "payout")),
                "funding_balance_sign_reconciled": False,
                "trade_pnl_does_not_include_rebate_twice": True,
                "markouts": marks, "markouts_are_not_additional_pnl": True,
                "receipt_sha256": hashlib.sha256(canonical({"fills": fills, "funding": funding}).encode()).hexdigest(),
                "audit_does_not_prove_new_session_ownership": True, "orders_submitted": 0}
