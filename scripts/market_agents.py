"""Independent buyer/seller API clients exercise policy negotiation, without signing."""

import argparse
import json
import time
from pathlib import Path

import httpx


def call(client, method, path, body=None):
    response = client.request(method, path, json=body if method != "GET" else None)
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4260")
    parser.add_argument("--evidence", type=Path, default=Path("artifacts/bilateral-agents.json"))
    args = parser.parse_args()
    now = int(time.time())
    with (httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as seller,
          httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as buyer):
        call(seller, "POST", "/api/sessions", {})
        initial = call(buyer, "POST", "/api/sessions", {})["snapshot"]
        rule = {"name": "Demo Seller Agent", "data_type": "arbitrum.block-state", "version": "demo-v1",
                "unit_price": "0.05", "floor_price": "0.035", "discount_bps": 2000,
                "discount_min_units": 10, "min_units": 1, "max_units": 100,
                "purposes": ["research", "automation"], "licenses": ["internal-use"],
                "updated_at": now, "refresh_seconds": 30, "response_seconds": 10, "ttl_seconds": 3600}
        supply = call(seller, "POST", "/api/supplies", rule)
        demand = {"data_type": "arbitrum.block-state", "purpose": "research", "license": "internal-use",
                  "units": 10, "max_unit_price": "0.04", "max_total_price": "0.4",
                  "max_age_seconds": 3600, "max_refresh_seconds": 60, "response_seconds": 30, "ttl_seconds": 3600}
        start = time.perf_counter()
        result = call(buyer, "POST", "/api/demands", demand)
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        view = call(buyer, "GET", "/api/market")
        agreement = next(m for m in view["matches"] if m["terms"]["supply_id"] == supply["id"])
        if agreement["terms"]["total_price"] != "0.4":
            raise RuntimeError("agreed price outside expected bilateral rule")
        payment = call(buyer, "POST", f'/api/matches/{agreement["id"]}/payment-request',
                       {"terms_hash": agreement["terms_hash"]})
        if payment["payment_status"] != "NOT_REQUESTED" or payment["tx_hash"] is not None:
            raise RuntimeError("sandbox agreement must not imply actual payment")
        call(seller, "POST", f'/api/supplies/{supply["id"]}/refresh', {"version": "demo-v2"})
        new_view = call(buyer, "GET", "/api/market")
        refreshed = next(m for m in new_view["matches"] if m["terms"]["supply_id"] == supply["id"])
        old_request = buyer.post(f'/api/matches/{agreement["id"]}/payment-request',
                                json={"terms_hash": agreement["terms_hash"]})
        if old_request.status_code != 409 or refreshed["terms_hash"] == agreement["terms_hash"]:
            raise RuntimeError("old data-version agreement was not invalidated")
        incompatible = call(buyer, "POST", "/api/demands", {**demand, "license": "redistribution"})
        final = call(buyer, "GET", "/api/workspace")
        if final["balance"] != initial["balance"] or final["spent"] != "0":
            raise RuntimeError("negotiation must not move money")
        evidence = {"schema_version": "bilateral-agent-run-1", "created_at": int(time.time()),
                    "data_assurance": "SYNTHETIC_SELLER_POLICY_DECLARATION_NOT_DATA_DELIVERY",
                    "demand": result, "supply": supply, "agreement": agreement,
                    "updated_agreement": refreshed, "old_agreement_admission": old_request.status_code,
                    "incompatible_demand": incompatible, "payment_gate": payment,
                    "language_model_calls": 0, "demand_request_latency_ms": latency_ms,
                    "latency_measurement": "ONE_HTTP_SAMPLE_NOT_A_BENCHMARK",
                    "buyer_balance": final["balance"], "buyer_spent": final["spent"]}
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"agreed_total": agreement["terms"]["total_price"], "llm_calls": 0,
                          "old_version_rejected": old_request.status_code == 409,
                          "payment_status": payment["payment_status"], "latency_ms": latency_ms}, indent=2))


if __name__ == "__main__":
    main()
