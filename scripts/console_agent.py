"""Exercise the running scoped-agent API without writing credentials to evidence."""

import json
from pathlib import Path

import httpx

from machine_commerce.domain import DEFAULT_REQUESTS

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:4260"


def checked(response):
    response.raise_for_status()
    return response.json()


def main():
    with httpx.Client(base_url=BASE, timeout=20) as owner, httpx.Client(base_url=BASE, timeout=20) as agent:
        session = checked(owner.post("/api/sessions", json={}))
        policy = checked(owner.post("/api/policies", json={"budget": "0.2", "max_order": "0.2",
            "allowed_offers": ["csv-normalize"], "ttl_seconds": 3600}))
        credential = checked(owner.post("/api/keys", json={"name": "Console verification agent",
            "scopes": ["read", "orders:write"], "policy_id": policy["id"], "ttl_seconds": 3600}))
        agent.headers["Authorization"] = "Bearer " + credential["secret"]
        try:
            assert agent.post("/api/keys", json={}).status_code == 403
            request = {"policy_id": policy["id"], "offer_id": "csv-normalize",
                "request": DEFAULT_REQUESTS["csv-normalize"], "idempotency_key": "console-verification"}
            order = checked(agent.post("/api/orders", json=request))
            repeat = checked(agent.post("/api/orders", json=request))
            assert order["id"] == repeat["id"]
            settled = checked(agent.post(f'/api/orders/{order["id"]}/run', json={}))
            delivery = checked(agent.get(f'/api/orders/{order["id"]}/artifact'))
            receipt = checked(agent.get(f'/api/orders/{order["id"]}/receipt'))
            assert settled["status"] == "SETTLED" and delivery["row_count"] == 2
            assert receipt["settlement"] == "SANDBOX_LEDGER"
            exhausted = agent.post("/api/orders", json={**request, "idempotency_key": "over-budget"})
            assert exhausted.status_code == 409
            metadata = checked(owner.get("/api/keys"))
            assert credential["secret"] not in json.dumps(metadata)
            result = {"remote_service": BASE, "workspace_id": session["snapshot"]["buyer_id"],
                "key": metadata["keys"][0], "order": settled, "delivery": delivery, "receipt": receipt,
                "checks": {"admin_denied": True, "idempotency": True, "shared_cap_enforced": True,
                    "secret_absent_from_metadata": True}, "real_payment": False}
        finally:
            revoked = checked(owner.post(f'/api/keys/{credential["key"]["id"]}/revoke', json={}))
        assert revoked["status"] == "revoked"
        assert agent.get("/api/workspace").status_code == 401
        result["key_after_run"] = revoked
        result["checks"]["revocation_enforced"] = True
        result["workspace_after_run"] = checked(owner.get("/api/workspace"))
        assert result["workspace_after_run"]["spent"] == "0.12"
        assert result["workspace_after_run"]["journal_integrity"]
        evidence = ROOT / "artifacts/console-agent.json"
        evidence.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"evidence": str(evidence), "checks": result["checks"],
            "settlement": receipt["settlement"], "real_payment": False}))


if __name__ == "__main__":
    main()
