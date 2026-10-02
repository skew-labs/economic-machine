"""Public product preview: isolated real-engine examples and a recorded receipt.

This process has no operator credentials, wallet keys or payment authority.
Authenticated console calls are proxied separately to the existing runtime.
"""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from economic_machine.values import MachineError

from .domain import DEFAULT_REQUESTS, money_atoms, money_string
from .market import Market, negotiate, normalize_demand, normalize_supply
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {"standard", "tight", "fresh", "budget", "recovery"}


def compare(store, case, now):
    market = Market(store)
    buyer, _ = store.create_session()
    demand = {"data_type": "chain.snapshot", "purpose": "research", "license": "internal-use",
              "units": 10, "max_unit_price": "0.04", "max_total_price": "0.40",
              "max_age_seconds": 60, "max_refresh_seconds": 30, "response_seconds": 10,
              "ttl_seconds": 600}
    if case == "tight":
        demand.update(max_unit_price="0.03", max_total_price="0.30")
    if case == "fresh":
        demand["max_age_seconds"] = 5
    suppliers = []
    for i, name in enumerate(["Orbit", "Relay", "Archive", "Prism", "Signal", "Scope"]):
        rules = {"name": name, "data_type": "chain.snapshot", "version": "snapshot-v1",
                 "unit_price": "0.05", "floor_price": "0.035", "discount_bps": 2000,
                 "discount_min_units": 10, "min_units": 1, "max_units": 100,
                 "purposes": ["research"], "licenses": ["internal-use"],
                 "updated_at": now - (3 if i == 0 else 8), "refresh_seconds": 15,
                 "response_seconds": 5, "ttl_seconds": 600}
        if i == 1:
            rules.update(unit_price="0.04", discount_bps=500)
        if i == 2:
            rules["updated_at"] = now - 180
        if i == 3:
            rules["licenses"] = ["commercial-use"]
        if i == 4:
            rules.update(unit_price="0.06", floor_price="0.06", discount_bps=0)
        if i == 5:
            rules["purposes"] = ["commercial"]
        seller, _ = store.create_session()
        market.register(seller, "supply", rules)
        result = negotiate(normalize_demand(demand), normalize_supply(rules, now), now)
        unit = result["trace"][1]["unit_price"]
        suppliers.append({"name": name, "mark": i, "age_seconds": now - rules["updated_at"],
                          "version": rules["version"], "ask_price": rules["unit_price"],
                          "total_price": money_string(money_atoms(unit) * 10),
                          "status": result["status"], "reason_codes": result["reason_codes"]})
    registered = market.register(buyer, "demand", demand)
    count = len(registered["matches"])
    return {"scenario": case, "fixture": True, "payment_requested": False,
            "policy": {"max_total_price": demand["max_total_price"], "max_age_seconds": demand["max_age_seconds"]},
            "suppliers": suppliers, "compatible": count, "language_model_calls": 0,
            "trace_title": "One policy. Six providers. Zero model calls.",
            "trace_summary": f"{count} agreements. Price, freshness and usage rights checked by the running engine."}


def boundary(store, case, clock):
    sid, _ = store.create_session()
    policy = store.create_policy(sid, {"budget": "0.20", "max_order": "0.12",
             "allowed_offers": ["csv-normalize"], "ttl_seconds": 600})
    args = (sid, policy["id"], "csv-normalize", DEFAULT_REQUESTS["csv-normalize"])
    first = store.reserve(*args, "portal-order-one")
    if case == "budget":
        try:
            store.reserve(*args, "portal-order-two")
        except MachineError as exc:
            reason = str(exc)
        else:
            raise RuntimeError("shared budget unexpectedly admitted a second order")
        replay = store.reserve(*args, "portal-order-one")
        steps = [{"title": "0.20 shared budget", "description": "One limit reused by both requests."},
                 {"title": "0.12 reserved", "description": "First request admitted; 0.08 remains."},
                 {"title": "Second request blocked", "description": "Another 0.12 would exceed the same limit."}]
        title, summary = "One budget. No double allocation.", "Second request blocked. Retry kept the original order."
    else:
        store.claim(sid, first["id"])
        clock[0] += 31
        recovered = store.recover_expired(sid)
        replay = store.reserve(*args, "portal-order-one")
        reason = "DELIVERY_DEADLINE_EXPIRED"
        steps = [{"title": "Reserve 0.12", "description": "A delivery starts under the approved limit."},
                 {"title": "Delivery times out", "description": "The engine releases the unused test-credit hold."},
                 {"title": "Retry reads the same order", "description": "No second reservation or payment."}]
        title, summary = "A timeout. One retained identity.", "0.12 released. Zero spent. No duplicate order."
        if recovered != 1 or store.recover_expired(sid) != 0:
            raise RuntimeError("recovery was not idempotent")
    return {"scenario": case, "fixture": True, "payment_requested": False,
            "title": title, "steps": steps, "summary": summary, "reason": reason,
            "same_order": replay["id"] == first["id"], "snapshot": store.snapshot(sid)}


def run_scenario(case):
    # Per-request stores are bounded, disposable and disconnected from live funds.
    clock = [1_790_920_000]
    with tempfile.TemporaryDirectory(prefix="commerce-preview-") as directory:
        store = Store(Path(directory) / "demo.sqlite3", lambda: clock[0])
        return compare(store, case, clock[0]) if case in {"standard", "tight", "fresh"} else boundary(store, case, clock)


def recorded_workspace(proof_path):
    proof = json.loads(Path(proof_path).read_text())
    if proof.get("verified") is not True or proof.get("mainnet") is not False or proof.get("chain_id") != 421614:
        raise ValueError("verified testnet record required")
    mandate = proof["mandate"]
    # Explicit allowlist; never expose private runtime state or wallet material.
    payment = {"id": proof["payment_id"], "status": "SETTLED", "amount_atoms": "10000",
               "network": "eip155:421614", "asset": proof["token"], "tx_hash": proof["tx_hash"],
               "created": proof["checked_at"], "delivery": proof["artifact"],
               "recorded_at": proof["checked_at"], "mainnet": False}
    safe_mandate = {k: mandate[k] for k in ["id", "payer", "payment_asset", "budget", "max_order", "expires", "reserved", "spent"]}
    return {"read_only": True, "recorded_at": proof["checked_at"],
            "snapshot": {"buyer_id": "Recorded testnet workspace", "balance": "0", "reserved": "0", "spent": "0",
                         "policies": [], "orders": [], "journal_integrity": True, "execution_authority": "NONE"},
            "keys": {"keys": [], "workspace_expires": mandate["expires"]},
            "health": {"status": "ok", "mode": "production", "recorded": True},
            "payments": {"mandates": [safe_mandate], "payments": [payment], "resource_details": []}}


def create_portal(site_dir=None, proof_path=None):
    site = Path(site_dir or os.environ.get("MACHINE_SITE_DIR", ROOT / "site"))
    proof_path = Path(proof_path or ROOT / "artifacts/arbitrum-sepolia/proof.json")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gate = asyncio.Semaphore(4)

    @app.middleware("http")
    async def public_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path == "/console":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path == "/demo/run":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.options("/demo/run")
    async def preflight():
        return JSONResponse({}, headers={"Access-Control-Allow-Methods": "POST, OPTIONS",
                                        "Access-Control-Allow-Headers": "Content-Type"})

    @app.post("/demo/run")
    async def demo(request: Request):
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 2048:
                return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"scenario"} or not isinstance(data["scenario"], str) or data["scenario"] not in SCENARIOS:
                raise ValueError()
        except (ValueError, TypeError):
            return JSONResponse({"error": "Choose an available preview scenario"}, status_code=400)
        async with gate:
            result = await run_in_threadpool(run_scenario, data["scenario"])
        return result

    @app.get("/demo/workspace")
    async def workspace():
        return JSONResponse(recorded_workspace(proof_path), headers={"Cache-Control": "no-store"})

    @app.get("/console", response_class=HTMLResponse)
    async def console():
        html = (ROOT / "web/index.html").read_text()
        version = hashlib.sha256(b"".join((ROOT / "web" / name).read_bytes() for name in
            ["index.html", "app.css", "app.js", "wallet.js", "console-theme.css", "assets/ui-icons.svg"])).hexdigest()[:16]
        html = html.replace("Machine Market | Console", "skew | Console")
        html = html.replace('href="/" aria-label="Economic Machine console"', 'href="/commerce/" aria-label="Economic Machine console"')
        html = html.replace("<span>Machine<small>Economic infrastructure</small></span>", "<span>skew<small>Economic Machine</small></span>")
        html = html.replace("</head>", '<meta name="machine-api-prefix" content="/commerce"><link rel="stylesheet" href="/commerce/console-theme.css?v=wallet-20261002-3"></head>')
        html = html.replace('href="/app.css', 'href="/commerce/app.css').replace('src="/app.js', 'src="/commerce/app.js')
        html = html.replace('src="/wallet.js', 'src="/commerce/wallet.js')
        html = html.replace('href="/assets/ui-icons.svg', 'href="/commerce/assets/ui-icons.svg')
        html = html.replace("wallet-20261002-3", version)
        html = html.replace('<body>', '<body><div class="portal-bar"><a href="/commerce/">← skew Economic Machine</a><a href="/commerce/console?preview=1">Verified workspace</a></div>')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/{asset:path}")
    async def files(asset):
        if asset in {"app.css", "app.js", "wallet.js", "console-theme.css"}:
            return FileResponse(ROOT / "web" / asset)
        if asset in {"assets/phantom-wallet.png", "assets/ui-icons.svg", "assets/PHOSPHOR-LICENSE.txt", "assets/icon-provenance.json"}:
            return FileResponse(ROOT / "web" / asset)
        if asset in {"", "index.html"}:
            return HTMLResponse((site / "index.html").read_text().replace('<head>', '<head><base href="/commerce/">'))
        if asset not in {"style.css", "site.js", "favicon.svg", "evidence.json", "assets/nvidia-logo.svg",
                         "assets/arbitrum-logo.svg", "assets/provider-orbit.svg", "assets/provider-relay.svg",
                         "assets/provider-archive.svg", "assets/provider-prism.svg", "assets/provider-signal.svg",
                         "assets/provider-scope.svg"}:
            return JSONResponse({"error": "Not found"}, status_code=404)
        target = site / asset
        return FileResponse(target) if target.is_file() else JSONResponse({"error": "Not found"}, status_code=404)

    return app


app = create_portal()
