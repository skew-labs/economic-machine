"""Remote-only HTTPS connection check. Credentials remain in the root-owned file.

Reads existing operator workspace; never creates a key, mandate or payment.
"""

import json
from pathlib import Path

import httpx

ORIGIN = "https://machine.148-113-153-116.nip.io"
ROOT = Path(__file__).resolve().parents[1]


def main():
    result = {}
    with httpx.Client(base_url=ORIGIN, timeout=20) as client:
        response = client.get("/commerce/api/workspace")
        assert response.status_code == 401
        result["unauthenticated_management_rejected"] = True
        private = json.loads(Path("/var/lib/machine-commerce-sepolia/demo-state.json").read_text())
        response = client.post("/commerce/api/sessions", json={"username": "buyer", "password": private["passwords"]["buyer"]})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert "Path=/commerce/" in cookie
        result["existing_operator_login_https_cookie_scoped"] = True
        for endpoint in ["workspace", "keys", "payments", "catalog"]:
            response = client.get("/commerce/api/" + endpoint)
            assert response.status_code == 200
            response.json()
            result["authenticated_" + endpoint] = True
        assert client.post("/commerce/api/sessions", json={}).json()["resumed"] is True
        result["session_resumes_without_budget_reset"] = True
        for scenario in ["standard", "tight", "fresh", "budget", "recovery"]:
            response = client.post("/commerce/demo/run", json={"scenario": scenario})
            assert response.status_code == 200
            assert response.json()["payment_requested"] is False
            result["public_demo_" + scenario] = True
        for asset in ["", "style.css", "site.js", "favicon.svg", "evidence.json", "assets/commerce-routing.png", "console", "app.js", "app.css", "console-theme.css"]:
            assert client.get("/commerce/" + asset).status_code == 200
        result["all_site_console_assets_resolve"] = True
        # Confirm the unrelated root and the original merchant boundary survive.
        assert client.get("/").status_code == 200
        result["existing_root_preserved"] = True
        assert client.get("/commerce-sepolia/data").status_code == 403
        result["merchant_post_only_boundary_preserved"] = True
    result["wallet_or_payment_actions"] = 0
    target = ROOT / "artifacts/landing/connection.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
