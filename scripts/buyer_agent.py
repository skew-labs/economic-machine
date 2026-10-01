"""A real API client: buy two services, verify delivery, collect receipts."""

import argparse
import json
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4260")
    parser.add_argument("--evidence", type=Path, default=Path("artifacts/agent-purchases.json"))
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=40, trust_env=False) as client:
        def call(method, url, payload=None):
            response = client.request(method, url, json=payload if method != "GET" else None)
            response.raise_for_status()
            return response.json()
        initial = call("POST", "/api/sessions", {})
        offers = call("GET", "/api/catalog")["offers"]
        policy = call("POST", "/api/policies", {"budget": "2", "max_order": "0.25",
            "allowed_offers": [o["id"] for o in offers], "ttl_seconds": 3600})
        orders = []
        for offer in offers:
            payload = {"policy_id": policy["id"], "offer_id": offer["id"],
                       "request": offer["default_request"], "idempotency_key": str(uuid.uuid4())}
            order = call("POST", "/api/orders", payload)
            duplicate = call("POST", "/api/orders", payload)
            if duplicate["id"] != order["id"]:
                raise RuntimeError("idempotency check failed")
            result = call("POST", f"/api/orders/{order['id']}/run", {})
            replay = call("POST", f"/api/orders/{order['id']}/run", {})
            if replay["receipt"] != result["receipt"]:
                raise RuntimeError("settlement replay changed receipt")
            orders.append({**result, "events": call("GET", f"/api/orders/{order['id']}/events")["events"]})
        final = call("GET", "/api/workspace")
        evidence = {"schema_version": "machine-commerce-agent-run-1",
            "initial_balance": initial["snapshot"]["balance"], "orders": orders,
            "final_balance": final["balance"], "spent": final["spent"],
            "reserved": final["reserved"], "journal_integrity": final["journal_integrity"],
            "settlement": "SANDBOX_LEDGER", "arbitrum_onchain_deployment": None}
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"orders": [{"id": o["id"], "service": o["offer_id"], "status": o["status"],
            "receipt_hash": o["receipt"]["receipt_hash"] if o["receipt"] else None} for o in orders],
            "spent": final["spent"], "journal_integrity": final["journal_integrity"]}, indent=2))


if __name__ == "__main__":
    main()
