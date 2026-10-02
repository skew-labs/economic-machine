"""Read-only public integration checks from the authorized remote host."""

import json
import time
from pathlib import Path

import httpx

from machine_commerce.atlas import verify_report

ROOT = Path(__file__).resolve().parents[1]
PORTAL = "https://machine.148-113-153-116.nip.io/commerce"
SITE = "https://skew-economic-machine.angus4314.chatgpt.site"


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Run integration verification on the remote host")
    pages = []
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=True) as client:
        for origin, paths in [(PORTAL, ["/", "/tools.html", "/atlas.html", "/site-lens.html", "/data-pass.html", "/engine-product.html", "/console", "/engine", "/data.js", "/demo/datapass", "/demo/native"])]:
            for path in paths:
                response = client.get(origin + path)
                response.raise_for_status()
                pages.append({"url": origin + path, "status": response.status_code, "resolved_url": str(response.url)})
        report = client.get(PORTAL + "/demo/atlas").json()
        verify_report(report)
        site_report = client.get(PORTAL + "/atlas.json").json()
        verify_report(site_report)
        assert report["report_sha256"] == site_report["report_sha256"]
        config = client.get(PORTAL + "/demo/datapass").json()
        assert config["datapass"]["status"] == "NOT_DEPLOYED"
        example = client.get(PORTAL + "/demo/native-program/example").json()["example"]
        okay = client.post(PORTAL + "/demo/native-program", json=example)
        okay.raise_for_status(); accepted = okay.json()
        assert accepted["accepted"] and accepted["candidate"] == "REDUCE"
        example["frame"]["values"] = [900000]
        no = client.post(PORTAL + "/demo/native-program", json=example)
        no.raise_for_status(); rejected = no.json()
        assert not rejected["accepted"] and rejected["candidate"] is None
        assert rejected["code"] == "ASSERTION_FAILED"
        for result in [accepted, rejected]:
            assert result["execution_authority"] == "NONE" and result["language_model_calls"] == 0
        assert accepted["library_sha256"] == rejected["library_sha256"]
        build = json.loads((ROOT / "artifacts/atlas-release/native-build.json").read_text())
        assert build["library_sha256"] == accepted["library_sha256"]
        site_response = client.get(SITE + "/")
        site_access = {"url": SITE, "unauthenticated_status": site_response.status_code,
                       "visibility": "AUTHENTICATED_CUSTOM_AUDIENCE" if site_response.status_code == 401 else "PUBLIC"}
        assert site_response.status_code in (200, 401)
    body = {"schema": "machine-tools-web-check-1", "accepted": True, "checked_at": int(time.time()),
            "pages": pages, "native_site_access": site_access, "report_sha256": report["report_sha256"],
            "native_accept": accepted, "native_abstain": rejected,
            "new_financial_transmissions": 0, "datapass_public_deployment": "NOT_DEPLOYED"}
    (ROOT / "artifacts/atlas-release/live-web-check.json").write_text(json.dumps(body, indent=2) + "\n")
    print(json.dumps({"accepted": True, "pages": len(pages), "report_sha256": body["report_sha256"], "native_library": accepted["library_sha256"]}))


if __name__ == "__main__":
    main()
