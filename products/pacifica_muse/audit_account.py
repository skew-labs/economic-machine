#!/usr/bin/env python3
"""Public GET-only Pacifica account/fee/receipt audit; never a trading run."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"src"))
from economic_machine.muse_receipts import Receipts


def get(path, params):
    if path not in ("account", "account/settings", "positions", "orders", "trades/history", "funding/history"):
        raise ValueError("GET route outside audit scope")
    url = "https://api.pacifica.fi/api/v1/"+path+"?"+urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read(1024*1024+1)
    if len(body) > 1024*1024:
        raise ValueError("audit response budget")
    value = json.loads(body)
    if type(value) is not dict or value.get("success") is not True:
        raise ValueError("audit query rejected")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lookback-hours", type=int, choices=range(1, 49), default=48)
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", args.account):
        parser.error("public account address required")
    os.umask(0o077); args.out.mkdir(parents=True, exist_ok=False)
    end = time.time_ns()//1_000_000; start = end-args.lookback_hours*3600*1000
    scope = {"account": args.account}
    acc = get("account", scope)["data"]
    settings = get("account/settings", scope)["data"]["margin_settings"]
    pos = get("positions", scope); orders = get("orders", scope)
    book = Receipts(args.out/"receipts.sqlite", args.account, start, end)
    pages = 0; complete = False; cursor = None; cursors = set()
    try:
        for _ in range(20):
            params = dict(scope, symbol="SOL", start_time=start, end_time=end, limit=100)
            if cursor: params["cursor"] = cursor
            page = get("trades/history", params); pages += 1
            for row in page["data"]: book.trade(row)
            if page.get("has_more") is False:
                complete = True; break
            cursor = page.get("next_cursor")
            if type(cursor) is not str or not cursor or cursor in cursors:
                raise ValueError("trade pagination did not advance")
            cursors.add(cursor)
        # Funding has no time-range query. Bound this audit and explicitly label
        # incomplete coverage instead of inferring an absent payment is zero.
        funding = get("funding/history", dict(scope, limit=100))
        for row in funding["data"]:
            if row.get("symbol") == "SOL": book.funding(row)
        report = {"audit_started_ms": end, "audit_completed_ms": time.time_ns()//1_000_000,
                  "lookback_hours": args.lookback_hours, "historical_account_audit": True,
                  "new_muse_session_started": False, "new_muse_session_fills": 0,
                  "account": {k: acc[k] for k in ("account_equity", "available_to_spend", "maker_fee", "taker_fee", "positions_count", "orders_count", "stop_orders_count")},
                  "sol_settings": [x for x in settings if x.get("symbol") == "SOL"],
                  "positions_returned": len(pos["data"]), "orders_returned": len(orders["data"]),
                  "position_sequence": pos.get("last_order_id"), "order_sequence": orders.get("last_order_id"),
                  "trade_pages": pages, "trade_history_complete": complete,
                  "funding_history_complete": funding.get("has_more") is False,
                  "economics": book.report(), "credentials_read": False, "financial_mutations": 0}
        (args.out/"report.json").write_text(json.dumps(report, indent=2)+"\n")
        print(json.dumps(report))
    finally:
        book.close()


if __name__ == "__main__": main()
