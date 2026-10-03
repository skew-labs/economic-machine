"""Canada-only PR2 browser checks. Isolated PayPal is explicitly a fixture."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from eth_account import Account
from playwright.sync_api import sync_playwright
from verify_tasks_browser import login

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/tasks-pr2"


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Remote browser operation required")
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    checks, errors, failed_http = [], [], []
    with tempfile.TemporaryDirectory() as temp:
        server = None
        base = "https://machine.148-113-153-116.nip.io/commerce" if args.public else "http://127.0.0.1:4294"
        if not args.public:
            env = os.environ.copy()
            env["TASK_QA_DB"] = str(Path(temp) / "commerce.db")
            env["PYTHONPATH"] = str(ROOT / "src") + ":" + str(ROOT / "tests")
            code = """import os,uvicorn
from test_task_checkout import FakePayPal
from machine_engine.paypal import PayPalSandbox
from machine_commerce.api import create_app
provider=FakePayPal()
PayPalSandbox.configured=classmethod(lambda cls:provider)
app=create_app(os.environ['TASK_QA_DB'])
@app.post('/fixture/buyer-approval')
def approve():
 provider.order['status']='APPROVED'
 return {'fixture':True}
@app.get('/fixture/counts')
def counts():
 return {'creates':len(provider.creates),'captures':len(provider.captures)}
uvicorn.run(app,host='127.0.0.1',port=4294,log_level='error')
"""
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
                context = browser.new_context(accept_downloads=True)
                if args.public:
                    login(
                        context, base, Account.create()
                    )  # Login only, unfunded ephemeral key, never persisted.
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "response",
                    lambda r: (
                        failed_http.append({"status": r.status, "url": r.url.split("?")[0]})
                        if r.status >= 400
                        else None
                    ),
                )
                page.goto(base + ("/console" if args.public else "/") + "#tasks")
                page.get_by_role("button", name="Save task", exact=True).wait_for()
                page.locator("#task-kind").select_option("data_cleanup")
                page.locator("#task-title").fill("Operations contact import")
                page.locator("#task-instructions").fill(
                    "Trim the contact import and export a clean JSON file for the operations team."
                )
                page.locator("#task-output_format").select_option("json")
                page.locator("#task-criteria").fill("name, email")
                with page.expect_response(
                    lambda r: r.url.endswith("/api/engine/tasks") and r.request.method == "POST"
                ) as response:
                    page.get_by_role("button", name="Save task", exact=True).click()
                assert response.value.status == 200
                checks.append("general_business_task_saved")
                if args.public:
                    page.get_by_text(
                        "PayPal sandbox setup is pending. Saving a brief never charges you.", exact=True
                    ).wait_for()
                    assert (
                        context.request.get(base + "/api/engine/task-checkout").json()["configured"] is False
                    )
                    assert page.get_by_role("button", name="Approve $1.00 · sandbox", exact=True).count() == 0
                    checks.append("public_unconfigured_payment_fail_closed")
                else:
                    page.locator("#task-source_csv").fill("discard this unfinished input")
                    page.get_by_role("button", name="Clear CSV input", exact=True).click()
                    assert page.locator("#task-source_csv").input_value() == ""
                    page.locator("#task-source_csv").fill("name,email\n Alice , alice@example.test \n")
                    with page.expect_response(lambda r: r.url.endswith("/purchase-plan")) as planned:
                        page.get_by_role("button", name="Review exact price", exact=True).click()
                    assert planned.value.status == 200
                    purchase = planned.value.json()
                    checks.append("input_and_exact_usd_price_frozen")
                    page.get_by_role("button", name="Approve $1.00 · sandbox", exact=True).click()
                    link = page.get_by_role("link", name="Continue to PayPal sandbox", exact=True)
                    link.wait_for()
                    assert (
                        link.get_attribute("href")
                        == "https://www.sandbox.paypal.com/checkoutnow?token=TESTORDER12345"
                    )
                    assert context.request.post(base + "/fixture/buyer-approval", data={}).status == 200
                    page.get_by_role(
                        "button", name="Confirm payment after PayPal approval", exact=True
                    ).click()
                    page.get_by_role("button", name="Prepare my result", exact=True).click()
                    download_button = page.get_by_role("button", name="Download result", exact=True)
                    download_button.wait_for()
                    assert page.get_by_role("button", name="Cancel unsent task", exact=True).count() == 0
                    with page.expect_download() as downloaded:
                        download_button.click()
                    payload = json.loads(Path(downloaded.value.path()).read_text())
                    assert payload == [{"name": "Alice", "email": "alice@example.test"}]
                    checks += [
                        "owner_approval_creates_bound_order",
                        "fixture_capture_get_readback",
                        "verified_entitlement",
                        "actual_transformed_file_downloaded",
                    ]
                    path = base + "/api/engine/task-purchases/" + purchase["id"]
                    assert (
                        context.request.post(
                            path + "/capture", data={"plan_hash": purchase["plan"]["hash"]}
                        ).json()["status"]
                        == "DELIVERED"
                    )
                    assert context.request.get(base + "/fixture/counts").json() == {
                        "creates": 1,
                        "captures": 1,
                    }
                    page.reload()
                    page.get_by_role("button", name="Download result", exact=True).wait_for()
                    checks.append("reload_resumes_purchase_without_duplicate_charge")
                    page.get_by_role("button", name="New task", exact=True).click()
                    page.get_by_role("button", name="Save task", exact=True).wait_for()
                    checks.append("new_work_remains_available_after_purchase")
                    page.locator(".task-row").filter(has_text="Operations contact import").click()
                    page.get_by_role("button", name="Download result", exact=True).wait_for()
                for width in [1440, 390]:
                    page.set_viewport_size({"width": width, "height": 1100})
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                    )
                    page.screenshot(
                        path=str(
                            OUT / ("checkout-" + ("public" if args.public else "fixture") + f"-{width}.jpg")
                        ),
                        full_page=True,
                        type="jpeg",
                        quality=85,
                    )
                    checks.append("responsive_" + str(width))
                if errors or failed_http:
                    (OUT / "browser-failure.json").write_text(
                        json.dumps(
                            {"javascript_errors": errors, "failed_http_responses": failed_http}, indent=2
                        )
                        + "\n"
                    )
                    print(
                        json.dumps({"javascript_errors": errors, "failed_http_responses": failed_http}),
                        flush=True,
                    )
                assert not errors and not failed_http
                browser.close()
        finally:
            if server:
                server.terminate()
                server.wait(timeout=10)
    evidence = {
        "accepted": True,
        "environment": "PUBLIC_UNCONFIGURED" if args.public else "ISOLATED_PAYPAL_FIXTURE",
        "checks": checks,
        "javascript_errors": errors,
        "failed_http_responses": failed_http,
        "actual_paypal_sandbox_transactions": 0,
        "real_money_transactions": 0,
        "actual_transform_download": not args.public,
        "disposable_unfunded_login_signatures": 1 if args.public else 0,
        "customer_payment_signatures": 0,
    }
    (OUT / ("browser-public.json" if args.public else "browser-isolated.json")).write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(json.dumps({"accepted": True, "checks": len(checks), "environment": evidence["environment"]}))


if __name__ == "__main__":
    main()
