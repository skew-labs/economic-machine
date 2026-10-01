"""Buyer-scoped API and app. Public deployment stays in explicit sandbox mode."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from economic_machine.values import MachineError, require_keys

from .domain import CATALOG, DEFAULT_REQUESTS, now_seconds, terms_hash
from .market import Market
from .providers import CommerceEngine, Workers
from .store import Store

LOGGER = logging.getLogger(__name__)


def create_app(db_path=None, clock=now_seconds, workers=None):
    root = Path(__file__).resolve().parents[2]
    web = root / "web"
    store = Store(db_path or os.environ.get("COMMERCE_DB", str(root / "runtime/commerce.sqlite3")), clock)
    engine = CommerceEngine(store, workers or Workers(clock))
    market = Market(store)

    @asynccontextmanager
    async def lifespan(_app):
        async def timers():
            while True:
                try:
                    await asyncio.to_thread(market.tick)
                    _app.state.timer_error = None
                except Exception:
                    LOGGER.exception("Agreement timer failed; health reports degraded")
                    _app.state.timer_error = "AGREEMENT_TIMER_FAILED"
                await asyncio.sleep(15)
        task = asyncio.create_task(timers())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="Economic Machine Commerce", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.timer_error = None

    @app.exception_handler(MachineError)
    async def machine_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.middleware("http")
    async def origin_and_size(request: Request, call_next):
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "cross-origin mutation rejected"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"error": "JSON request required"}, status_code=415)
            body = await request.body()
            if len(body) > 50_000:
                return JSONResponse({"error": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def buyer(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.cookies.get("machine_buyer")
        try:
            return store.authenticate(token)
        except MachineError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/healthz")
    def health():
        return JSONResponse({"status": "degraded" if app.state.timer_error else "ok",
                "timer_error": app.state.timer_error, "product": "economic-machine-commerce",
                "settlement_mode": "SANDBOX_LEDGER", "onchain_deployment": None,
                "customer_signing_authority": "NONE"}, status_code=503 if app.state.timer_error else 200)

    @app.get("/api/catalog")
    def catalog():
        return {"offers": [{**offer, "default_request": DEFAULT_REQUESTS[offer_id],
                             "terms_hash": terms_hash(offer_id)} for offer_id, offer in CATALOG.items()],
                "settlement_mode": "SANDBOX_LEDGER", "asset": "TEST_CREDIT"}

    @app.post("/api/sessions")
    def sessions(request: Request):
        # Re-opening a browser never silently resets its existing budget.
        prior = request.cookies.get("machine_buyer")
        if prior:
            try:
                sid = store.authenticate(prior)
                return JSONResponse({"snapshot": store.snapshot(sid), "resumed": True})
            except MachineError:
                pass
        sid, token = store.create_session()
        response = JSONResponse({"snapshot": store.snapshot(sid), "api_token": token, "resumed": False})
        secure = os.environ.get("COMMERCE_SECURE_COOKIE", "0") == "1"
        response.set_cookie("machine_buyer", token, httponly=True, samesite="strict", secure=secure,
                            max_age=86400, path="/")
        return response

    @app.get("/api/workspace")
    def workspace(sid=Depends(buyer)):
        return store.snapshot(sid)

    @app.get("/api/market")
    def market_view(sid=Depends(buyer)):
        return market.snapshot(sid)

    @app.post("/api/demands")
    def register_demand(raw: dict, sid=Depends(buyer)):
        return market.register(sid, "demand", raw)

    @app.post("/api/supplies")
    def register_supply(raw: dict, sid=Depends(buyer)):
        return market.register(sid, "supply", raw)

    @app.post("/api/supplies/{supply_id}/refresh")
    def refresh_supply(supply_id: str, raw: dict, sid=Depends(buyer)):
        require_keys(raw, {"version"}, "data update event")
        return market.refresh_supply(sid, supply_id, raw["version"])

    @app.post("/api/matches/{match_id}/payment-request")
    def payment_request(match_id: str, raw: dict, sid=Depends(buyer)):
        require_keys(raw, {"terms_hash"}, "agreed payment request")
        return market.admit(sid, match_id, raw["terms_hash"])

    @app.post("/api/policies")
    def policy(raw: dict, sid=Depends(buyer)):
        return store.create_policy(sid, raw)

    @app.post("/api/orders")
    def order(raw: dict, sid=Depends(buyer)):
        require_keys(raw, {"policy_id", "offer_id", "request", "idempotency_key"}, "order")
        return store.reserve(sid, **raw)

    @app.get("/api/orders/{oid}")
    def get_order(oid: str, sid=Depends(buyer)):
        return store.order(sid, oid)

    @app.post("/api/orders/{oid}/run")
    def run_order(oid: str, sid=Depends(buyer)):
        return engine.run(sid, oid)

    @app.post("/api/orders/{oid}/cancel")
    def cancel_order(oid: str, sid=Depends(buyer)):
        return store.cancel(sid, oid)

    @app.get("/api/orders/{oid}/events")
    def events(oid: str, sid=Depends(buyer)):
        return {"events": store.order_events(sid, oid)}

    @app.get("/api/orders/{oid}/artifact")
    def artifact(oid: str, sid=Depends(buyer)):
        order = store.order(sid, oid)
        if not order["artifact"]:
            raise HTTPException(404, "artifact unavailable")
        return JSONResponse(order["artifact"], headers={"Content-Disposition":
            f'attachment; filename="{oid}-delivery.json"'})

    @app.get("/api/orders/{oid}/receipt")
    def receipt(oid: str, sid=Depends(buyer)):
        order = store.order(sid, oid)
        if not order["receipt"]:
            raise HTTPException(404, "settlement receipt unavailable")
        return JSONResponse(order["receipt"], headers={"Content-Disposition":
            f'attachment; filename="{oid}-receipt.json"'})

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/app.css")
    def stylesheet():
        return FileResponse(web / "app.css")

    @app.get("/app.js")
    def javascript():
        return FileResponse(web / "app.js")

    return app


def app_factory():
    return create_app()
