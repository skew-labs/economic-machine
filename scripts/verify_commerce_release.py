"""Read-only verification of the deployed checkout surface; never signs or pays."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://machine.148-113-153-116.nip.io/commerce"


def main():
    checks = []
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for path in ["/commerce.js", "/commerce.css", "/app.js", "/wallet.js", "/data.js"]:
            response = client.get(BASE + path)
            response.raise_for_status()
            local = (ROOT / "web" / path[1:]).read_bytes()
            same = response.content == local
            checks.append({"path": path, "http_status": response.status_code, "source_matches": same,
                           "sha256": hashlib.sha256(response.content).hexdigest()})
            if not same:
                raise RuntimeError("deployed asset differs from reviewed source: " + path)
        html = client.get(BASE + "/console")
        html.raise_for_status()
        if '/commerce/commerce.js?v=' not in html.text or 'id="subscriptions-view"' not in html.text:
            raise RuntimeError("console does not include the new purchase surface")
        catalog = client.get(BASE + "/api/commerce/catalog")
        catalog.raise_for_status()
        catalog = catalog.json()
        plan = next(p for p in catalog["plans"] if p["id"] == "atlas-monthly")
        if plan["price"] != "10" or plan["auto_charge"] or plan["duration_seconds"] != 30 * 86400:
            raise RuntimeError("published subscription terms differ from owner approval")
        for path in ["/api/commerce/checkouts", "/api/commerce/subscriptions/atlas-monthly/delivery", "/api/keys", "/api/data/purchase-status?purchase_id=0x" + "a" * 64]:
            status = client.get(BASE + path).status_code
            checks.append({"path": path, "http_status": status, "anonymous_access_blocked": status == 401})
            if status != 401:
                raise RuntimeError("private purchase or key route does not require authentication")
        health = client.get(BASE + "/healthz")
        health.raise_for_status()
    result = {"checked_at": datetime.now(UTC).isoformat(), "url": BASE, "accepted": True,
              "scope": "READ_ONLY_HTTP_AND_ASSET_IDENTITY", "checks": checks,
              "catalog": catalog, "customer_signatures": 0, "customer_payments": 0,
              "gpu_workloads_started": 0}
    path = ROOT / "artifacts/atlas-release/commerce-live-check.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"accepted": True, "checks": len(checks), "plan_status": plan["status"],
                      "compute_providers": sum(p["category"] == "compute" for p in catalog["products"])}))


if __name__ == "__main__":
    main()
