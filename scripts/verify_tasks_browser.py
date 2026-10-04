"""Real task UI/API/reload checks on Canada; no model, payment, or venue call."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/tasks-pr1"


def login(context, base, wallet):
    headers = {"Origin": "https://machine.148-113-153-116.nip.io"}
    challenged = context.request.post(
        base + "/api/auth/challenge", data={"address": wallet.address, "chain_id": 42161}, headers=headers
    )
    assert challenged.status == 200
    body = challenged.json()
    signed = wallet.sign_message(encode_defunct(text=body["message"]))
    response = context.request.post(
        base + "/api/auth/verify",
        data={"challenge_id": body["challenge_id"], "signature": "0x" + signed.signature.hex()},
        headers=headers,
    )
    assert response.status == 200


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote browser operation required")
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    errors, checks = [], []
    with tempfile.TemporaryDirectory() as temp:
        server = None
        base = "https://machine.148-113-153-116.nip.io/commerce" if args.public else "http://127.0.0.1:4293"
        if not args.public:
            env = os.environ.copy()
            env["TASK_QA_DB"] = str(Path(temp) / "commerce.db")
            env["PYTHONPATH"] = str(ROOT / "src")
            code = "import os,uvicorn;from machine_commerce.api import create_app;uvicorn.run(create_app(os.environ['TASK_QA_DB']),host='127.0.0.1',port=4293,log_level='error')"
            server = subprocess.Popen(
                ["/srv/skew/economic-machine-commerce-20261002/.venv/bin/python", "-c", code], env=env
            )
            for _ in range(100):
                try:
                    if httpx.get(base + "/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Isolated runtime not ready")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path="/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell"
                )
                context = browser.new_context()
                wallet = Account.create() if args.public else None
                if wallet:
                    login(context, base, wallet)
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                failed_http = []
                page.on("response", lambda r: failed_http.append({"status": r.status, "path": r.url.split("?")[0].split("/commerce")[-1]}) if r.status >= 400 else None)
                page.goto(base + ("/console" if args.public else "/") + "#tasks")
                try:
                    page.get_by_role("button", name="Save task", exact=True).wait_for()
                except Exception:
                    page.screenshot(path=str(OUT / "initialization-failure.jpg"), full_page=True)
                    print(json.dumps({"failed_http": failed_http, "notice": page.locator("#notice").inner_text(), "javascript_errors": errors}), flush=True)
                    raise
                page.locator("#task-title").fill("CRM vendor shortlist")
                page.locator("#task-instructions").fill(
                    "Compare software subscription vendors for a procurement meeting."
                )
                page.locator("#task-criteria").fill("Price, Support")
                page.get_by_text("More conditions", exact=True).click()
                page.locator("#task-regions").fill("SG, JP")
                with page.expect_response(
                    lambda r: r.url.endswith("/api/engine/tasks") and r.request.method == "POST"
                ) as response:
                    page.get_by_role("button", name="Save task", exact=True).click()
                assert response.value.status == 200
                task = response.value.json()
                tid = task["id"]
                assert task["status"] == "READY_TO_PLAN"
                page.get_by_role("button", name="Save revision", exact=True).wait_for()
                page.get_by_text("Save conditions for next time", exact=True).click()
                with page.expect_response(lambda r: r.url.endswith("/remember")) as remembered:
                    page.get_by_role("button", name="Confirm selected conditions", exact=True).click()
                assert remembered.value.status == 200
                preference = remembered.value.json()
                page.get_by_role("button", name="New task", exact=True).click()
                page.locator("#task-title").fill("Next procurement comparison")
                page.locator("#task-instructions").fill(
                    "Use the same conditions as last time. Update the shortlist."
                )
                page.locator("#task-budget").fill("3.25")
                page.locator("#task-preference").select_option("last")
                with page.expect_response(
                    lambda r: r.url.endswith("/api/engine/tasks") and r.request.method == "POST"
                ) as response:
                    page.get_by_role("button", name="Save task", exact=True).click()
                derived = response.value.json()
                assert response.value.status == 200
                assert (
                    derived["brief"]["constraints"]["regions"] == ["SG", "JP"]
                    and derived["brief"]["budget"]["cents"] == 325
                )
                assert derived["brief"]["inherited_preference"]["id"] == preference["id"]
                page.get_by_role("button", name="Save revision", exact=True).wait_for()
                page.reload()
                page.get_by_role("button", name="New task", exact=True).wait_for()
                page.locator(".task-row").filter(has_text="Next procurement comparison").click()
                page.get_by_role("button", name="Save revision", exact=True).wait_for()
                assert page.locator("#task-budget").input_value() == "3.25"
                page.locator("#task-title").fill("Edited for tomorrow")
                page.get_by_role("button", name="Save revision", exact=True).click()
                page.get_by_role("heading", name="Edited for tomorrow", exact=True).wait_for()
                page.get_by_role("button", name="Forget", exact=True).click()
                page.get_by_text("Needs details", exact=True).first.wait_for()
                assert (
                    context.request.get(base + "/api/engine/tasks/" + derived["id"]).json()["status"]
                    == "NEEDS_INPUT"
                )
                checks += [
                    "task_saved_from_ui",
                    "confirmed_preferences",
                    "explicit_budget_not_inherited",
                    "reload_persistence",
                    "revision_saved",
                    "revocation_invalidates_inheritance",
                ]
                for kind, title, format in [
                    ("research_brief", "Competitor meeting brief", "document"),
                    ("document_draft", "Customer proposal", "document"),
                    ("data_cleanup", "CRM import cleanup", "csv"),
                    ("content_localization", "Product copy in Korean", "document"),
                ]:
                    page.get_by_role("button", name="New task", exact=True).click()
                    page.locator("#task-kind").select_option(kind)
                    page.locator("#task-title").fill(title)
                    page.locator("#task-instructions").fill(
                        "Prepare a reviewed deliverable for the operations team."
                    )
                    page.locator("#task-output_language").select_option("ko")
                    page.locator("#task-output_format").select_option(format)
                    if kind == "data_cleanup":
                        page.locator("#task-criteria").fill("email, company")
                    if kind == "content_localization":
                        page.locator("#task-source_language").select_option("en")
                    with page.expect_response(
                        lambda r: r.url.endswith("/api/engine/tasks") and r.request.method == "POST"
                    ) as response:
                        page.get_by_role("button", name="Save task", exact=True).click()
                    assert response.value.status == 200 and response.value.json()["status"] == "READY_TO_PLAN"
                    page.get_by_role("button", name="Save revision", exact=True).wait_for()
                    checks.append(kind + "_ui_to_api")
                for width in [1440, 390]:
                    page.set_viewport_size({"width": width, "height": 1000})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth<=document.documentElement.clientWidth"
                    )
                    assert (
                        page.locator("img").evaluate_all(
                            "(imgs)=>imgs.filter(i=>!i.complete||!i.naturalWidth).length"
                        )
                        == 0
                    )
                    page.screenshot(
                        path=str(
                            OUT / (f"tasks-{width}-" + ("public" if args.public else "isolated") + ".jpg")
                        ),
                        full_page=True,
                        type="jpeg",
                        quality=85,
                    )
                    checks.append("responsive_" + str(width))
                records = context.request.get(base + "/api/engine/tasks").json()
                if wallet:
                    second = browser.new_context()
                    login(second, base, wallet)
                    again = second.request.get(base + "/api/engine/tasks").json()
                    assert any(t["id"] == tid for t in again["tasks"])
                    checks.append("same_wallet_new_session_context")
                    assert context.request.get(base + "/api/engine/tasks").status == 401
                    checks.append("previous_session_rejected_after_relogin")
                    second.close()
                assert all(t["payment_authority"] == "NONE" for t in records["tasks"])
                assert all(not t["brief"]["automatic_execution"] for t in records["tasks"])
                assert not errors
                assert not failed_http, "Public UI issued failing HTTP requests"
                browser.close()
        finally:
            if server:
                server.terminate()
                server.wait(timeout=10)
    evidence = {
        "accepted": True,
        "environment": "PUBLIC_WALLET_WORKSPACE" if args.public else "ISOLATED_RUNTIME",
        "checks": checks,
        "javascript_errors": errors,
        "failed_http_responses": failed_http,
        "customer_payment_signatures": 0,
        "payments_submitted": 0,
        "venue_orders_submitted": 0,
        "language_model_calls": 0,
        "completed_deliverables": 0,
        "scope": "TASK_BRIEFS_CONFIRMED_CONTEXT_AND_REVISIONS_NOT_FULFILLMENT_OR_PAYMENT",
    }
    (OUT / ("browser-public.json" if args.public else "browser-isolated.json")).write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
