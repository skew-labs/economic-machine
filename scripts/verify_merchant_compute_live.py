"""Verify deployed subscription gates and unsigned compute discovery on Canada.

Uses an existing operator account solely for a provider connection check.
No new key, mandate, wallet signature, settlement or GPU job is created.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://machine.148-113-153-116.nip.io"
BASE = "/commerce/api/commerce"


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Live evidence runs on the remote host")
    with httpx.Client(base_url=ORIGIN, timeout=30, trust_env=False, follow_redirects=False) as client:
        catalog = client.get(BASE + "/catalog"); catalog.raise_for_status()
        plan = next(p for p in catalog.json()["plans"] if p["id"] == "atlas-monthly")
        assert plan["price"] == "10" and plan["duration_seconds"] == 2592000 and not plan["auto_charge"]
        recipient = json.loads((ROOT / 'artifacts/atlas-release/subscription-wallet-public.json').read_text())["address"].lower()
        assert plan["status"] == "AVAILABLE" and plan["product"]["pay_to"] == recipient
        provider = catalog.json()["compute_connections"]["providers"][0]
        assert not provider["purchase_enabled"] and not provider["capacity_verified"]
        anon = client.post(BASE + "/compute/gate402-inference/check", json={})
        assert anon.status_code == 401
        closed = client.post(BASE + "/merchant/atlas-monthly", json={}, headers={"Idempotency-Key": "unsigned-readiness-check"})
        assert closed.status_code == 409
        private = json.loads(Path("/var/lib/machine-commerce-sepolia/demo-state.json").read_text())
        login = client.post("/commerce/api/sessions", json={"username": "buyer", "password": private["passwords"]["buyer"]})
        assert login.status_code == 200
        response = client.post(BASE + "/compute/gate402-inference/check", json={})
        response.raise_for_status()
        quote = response.json()
        assert not quote["purchase_enabled"] and quote["signatures_sent"] == 0 and quote["workloads_started"] == 0
        js = client.get('/commerce/commerce.js'); js.raise_for_status()
        assert js.content == (ROOT / 'web/commerce.js').read_bytes()
        body = client.get('/commerce/console'); body.raise_for_status()
        assert '/commerce/commerce.js?v=providers-20261003' in body.text
        client.get('/commerce/healthz').raise_for_status()
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        supported = client.get('https://facilitator.payai.network/supported'); supported.raise_for_status()
        kinds = supported.json()["kinds"]
        assert any(k["network"] == "eip155:42161" and k["scheme"] == "exact" and k["x402Version"] == 2 for k in kinds)
    result = {"accepted": True, "checked_at": datetime.now(UTC).isoformat(), "subscription_status": plan["status"],
        "facilitator_supports_arbitrum_v2_exact": True, "anonymous_provider_check_blocked": True,
        "merchant_rejects_missing_checkout": True, "recipient": recipient, "compute_connection": quote, "customer_signatures": 0,
        "payments_submitted": 0, "gpu_workloads_started": 0, "source_matches_deployed_console": True}
    (ROOT / 'artifacts/atlas-release/merchant-compute-live.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({"accepted": True, "provider_status": quote["status"], "subscription_status": plan["status"],
                      "payments_submitted": 0}))


if __name__ == '__main__':
    main()
