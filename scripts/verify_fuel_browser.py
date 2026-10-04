"""Remote browser inspection: synthetic wallet discovery, no signing methods."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://machine.148-113-153-116.nip.io"


def main():
    if not str(ROOT).startswith("/srv/skew/"):
        raise SystemExit("Run on the authorized remote host.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, help="Public wallet address for read-only quoting")
    args = parser.parse_args()
    output = ROOT/"artifacts/fuel"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE+"/commerce/swap")
        page.get_by_role("heading", name="Get ETH for gas.").wait_for()
        assert page.locator("#quote").is_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output/"desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output/"mobile.png"), full_page=True)
        results.append("desktop_and_mobile_without_horizontal_overflow")
        # UI-only provider. Financial signing and transaction methods always fail.
        page.evaluate("""owner => {
          window.qaWalletCalls=[];
          const provider={request:async ({method})=>{
            window.qaWalletCalls.push(method);
            if(method==='eth_requestAccounts'||method==='eth_accounts')return [owner];
            if(method==='eth_chainId')return '0xa4b1';
            throw new Error('QA_FORBIDS_SIGNING_AND_TRANSACTIONS');
          }};
          dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{
            info:{uuid:'f001f001-1234-4123-8123-123456789abc',name:'Read-only QA wallet',icon:'',rdns:'invalid.fixture'},provider}}));
        }""", args.owner)
        page.get_by_role("button", name="Connect Read-only QA wallet").click()
        with page.expect_response(lambda r: r.url.endswith("/swap-api/quote"), timeout=90000) as response:
            page.get_by_role("button", name="Find a route").click()
        reply = response.value
        if reply.status != 200:
            raise AssertionError({"quote_status": reply.status, "error": reply.json().get("error")})
        try:
            page.locator("#permit").wait_for(state="visible", timeout=10000)
        except Exception as exc:
            raise AssertionError({"visible_status": page.locator("#status").inner_text(), "page_errors": errors}) from exc
        assert "2 USDC" in page.locator("#details").inner_text()
        assert "Quote received" in page.locator("#status").inner_text()
        results.append("live_public_quote_to_exact_amount_review")
        page.screenshot(path=str(output/"quote-review.png"), full_page=True, mask=[page.locator("#account")])
        page.locator("#amount").fill("3")
        assert page.locator("#permit").is_hidden()
        assert page.locator("#review").is_hidden()
        results.append("editing_amount_invalidates_old_review")
        calls = page.evaluate("window.qaWalletCalls")
        assert not any("sign" in x.lower() or "send" in x.lower() for x in calls)
        assert not errors, errors
        health = page.request.get(BASE+"/commerce/swap-api/health")
        assert health.status == 200 and health.json()["server_wallet_keys"] is False
        assert page.request.post(BASE+"/commerce/swap-api/quote", data={}).status == 403
        for path in ["/commerce/api/engine/fuel/requests/fuel-missing", "/commerce/api/engine/profiles"]:
            assert page.request.get(BASE+path).status in {401,403}
        results.append("public_health_and_unauthenticated_engine_denied")
        browser.close()
    evidence = {"checks": results, "page_errors": errors, "wallet_provider": "SYNTHETIC_READ_ONLY",
                "signatures_requested": 0, "orders_submitted": 0, "transactions_broadcast": 0}
    (output/"browser-verification.json").write_text(json.dumps(evidence, indent=2)+"\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
