"""Buyer-scoped API and app. Public deployment stays in explicit sandbox mode."""

import asyncio
import ipaddress
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from economic_machine.values import MachineError, require_keys

from .access import Access
from .domain import CATALOG, DEFAULT_REQUESTS, now_seconds, terms_hash
from .market import Market
from .operations import Operations, Settings
from .payments import Payments
from .providers import CommerceEngine, Workers
from .store import Store
from .wallet_auth import WalletAuth

LOGGER = logging.getLogger(__name__)


def create_app(db_path=None, clock=now_seconds, workers=None, settings=None, payment_transport=None, payment_chain=None):
    root = Path(__file__).resolve().parents[2]
    web = root / "web"
    store = Store(db_path or os.environ.get("COMMERCE_DB", str(root / "runtime/commerce.sqlite3")), clock)
    engine = CommerceEngine(store, workers or Workers(clock))
    market = Market(store)
    settings = (settings or Settings.environment()).validate()
    payments = Payments(store, market, settings.resources, payment_transport, payment_chain)
    operations = Operations(store, settings)
    access = Access(store, settings.mode)
    wallet_auth = WalletAuth(store, settings.origin)

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
        async def payment_recovery():
            while True:
                try:
                    result = await asyncio.to_thread(payments.recover)
                    _app.state.payment_worker_error = "PAYMENT_RECOVERY_DEGRADED" if result["failures"] else None
                except Exception:  # noqa: BLE001 - keep recovery alive without exposing authorization material.
                    _app.state.payment_worker_error = "PAYMENT_RECOVERY_FAILED"
                await asyncio.sleep(15)
        task = asyncio.create_task(timers())
        recovery = asyncio.create_task(payment_recovery())
        yield
        task.cancel()
        recovery.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await recovery

    app = FastAPI(title="Economic Machine Commerce", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.access = access
    app.state.wallet_auth = wallet_auth
    app.state.market, app.state.payments, app.state.operations = market, payments, operations
    app.state.timer_error = None
    app.state.payment_worker_error = None

    @app.exception_handler(MachineError)
    async def machine_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.middleware("http")
    async def origin_and_size(request: Request, call_next):
        if settings.mode == "production" and request.url.path != "/healthz":
            if request.url.scheme != "https":
                return JSONResponse({"error": "HTTPS required"}, status_code=403)
            peer = request.client.host if request.client else "unknown"
            # Only the trusted loopback reverse proxy can supply the real peer.
            if peer in {"127.0.0.1", "::1"}:
                try:
                    peer = str(ipaddress.ip_address(request.headers.get("x-real-ip", peer)))
                except ValueError:
                    pass
            credential = request.headers.get("authorization", request.cookies.get("machine_buyer", ""))
            budget = 60 if request.method == "POST" else 600
            if (not operations.admit_rate("peer:" + peer + request.method, budget * 10)
                    or not operations.admit_rate("caller:" + (credential or peer) + request.method, budget)):
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
            if request.url.path == "/api/sessions" and not operations.admit_rate("login:" + peer, 5):
                return JSONResponse({"error": "login rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
            if request.method == "POST" and request.url.path.startswith("/api/auth/"):
                if request.headers.get("origin") != settings.origin:
                    return JSONResponse({"error": "same-origin wallet sign-in required"}, status_code=403)
                if not operations.admit_rate("wallet:" + peer + request.url.path, 10):
                    return JSONResponse({"error": "sign-in rate limit exceeded"}, status_code=429,
                                        headers={"Retry-After": "60"})
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and ((settings.mode == "production" and origin != settings.origin)
                           or (settings.mode != "production" and urlsplit(origin).netloc != request.headers.get("host"))):
                return JSONResponse({"error": "cross-origin mutation rejected"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"error": "JSON request required"}, status_code=415)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 50_000:
                    return JSONResponse({"error": "request too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
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
        if auth and not auth.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required")
        token = auth[7:] if auth.startswith("Bearer ") else request.cookies.get("machine_buyer")
        try:
            principal = access.resolve(token)
        except MachineError as exc:
            raise HTTPException(401, str(exc)) from exc
        if settings.mode == "production" and not (operations.known_operator(principal.session_id)
                                                   or wallet_auth.identity(principal.session_id)):
            raise HTTPException(401, "authenticated owner workspace required")
        try:
            access.authorize(principal, request.method, request.url.path)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        request.state.principal = principal
        return principal.session_id

    def bind_order(request, policy_id):
        try:
            access.bind_order(request.state.principal, policy_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    def bind_payment(request, mandate_id):
        try:
            access.bind_payment(request.state.principal, mandate_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/healthz")
    def health():
        degraded = app.state.timer_error or app.state.payment_worker_error
        return JSONResponse({"status": "degraded" if degraded else "ok", "mode": settings.mode,
                "timer_error": app.state.timer_error, "product": "economic-machine-commerce",
                "payment_worker_error": app.state.payment_worker_error, "x402_configured": bool(settings.resources),
                "settlement_mode": "X402_EIP3009_AND_TEST_LEDGER" if settings.resources else "SANDBOX_LEDGER", "onchain_deployment": None,
                "customer_signing_authority": "NONE"}, status_code=503 if degraded else 200)

    @app.get("/api/catalog")
    def catalog():
        return {"offers": [{**offer, "default_request": DEFAULT_REQUESTS[offer_id],
                             "terms_hash": terms_hash(offer_id)} for offer_id, offer in CATALOG.items()],
                "settlement_mode": "SANDBOX_LEDGER", "asset": "TEST_CREDIT"}

    @app.post("/api/sessions")
    def sessions(raw: dict, request: Request):
        # Re-opening a browser never silently resets its existing budget.
        prior = request.cookies.get("machine_buyer")
        if prior:
            try:
                sid = store.authenticate(prior)
                if settings.mode != "production" or operations.known_operator(sid) or wallet_auth.identity(sid):
                    return JSONResponse({"snapshot": store.snapshot(sid), "resumed": True})
            except MachineError:
                pass
        if settings.mode == "production":
            try:
                sid, token = operations.login(raw)
            except PermissionError as exc:
                raise HTTPException(401, str(exc)) from exc
            response = JSONResponse({"snapshot": store.snapshot(sid), "resumed": False})
        else:
            sid, token = store.create_session()
            response = JSONResponse({"snapshot": store.snapshot(sid), "api_token": token, "resumed": False})
        secure = settings.mode == "production" or os.environ.get("COMMERCE_SECURE_COOKIE", "0") == "1"
        response.set_cookie("machine_buyer", token, httponly=True, samesite="strict", secure=secure,
                            max_age=86400, path="/")
        return response

    @app.post("/api/auth/challenge")
    def wallet_challenge(raw: dict):
        result, binding = wallet_auth.challenge(raw)
        response = JSONResponse(result)
        response.set_cookie("machine_login", binding, httponly=True, samesite="strict", secure=True,
                            max_age=300, path="/")
        return response

    @app.post("/api/auth/verify")
    def wallet_verify(raw: dict, request: Request):
        try:
            sid, token = wallet_auth.verify(raw, request.cookies.get("machine_login"))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        response = JSONResponse({"identity": wallet_auth.identity(sid), "snapshot": store.snapshot(sid)})
        response.set_cookie("machine_buyer", token, httponly=True, samesite="strict", secure=True,
                            max_age=86400, path="/")
        response.delete_cookie("machine_login", path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/auth/session")
    def wallet_session(request: Request, sid=Depends(buyer)):
        if not request.state.principal.owner:
            raise HTTPException(403, "owner session required")
        return {"identity": wallet_auth.identity(sid), "buyer_id": sid}

    @app.post("/api/auth/logout")
    def wallet_logout(raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, set(), "sign-out")
        if not request.state.principal.owner:
            raise HTTPException(403, "owner session required")
        wallet_auth.logout(sid)
        response = JSONResponse({"signed_out": True})
        response.delete_cookie("machine_buyer", path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/workspace")
    def workspace(sid=Depends(buyer)):
        return store.snapshot(sid)

    @app.get("/api/keys")
    def keys(sid=Depends(buyer)):
        return access.list(sid)

    @app.post("/api/keys")
    def create_key(raw: dict, sid=Depends(buyer)):
        return access.create(sid, raw)

    @app.post("/api/keys/{kid}/revoke")
    def revoke_key(kid: str, raw: dict, sid=Depends(buyer)):
        require_keys(raw, set(), "key revocation")
        return access.revoke(sid, kid)

    @app.get("/api/market")
    def market_view(sid=Depends(buyer)):
        return market.snapshot(sid)

    @app.post("/api/demands")
    def register_demand(raw: dict, sid=Depends(buyer)):
        return market.register(sid, "demand", raw)

    @app.get("/api/demands/{demand_id}/matches")
    def demand_matches(demand_id: str, sid=Depends(buyer)):
        return market.demand_matches(sid, demand_id)

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
        if settings.mode == "production":
            raise HTTPException(403, "test-credit policies are disabled; create a real payment mandate")
        return store.create_policy(sid, raw)

    @app.get("/api/payments")
    def payment_snapshot(sid=Depends(buyer)):
        return payments.snapshot(sid)

    @app.post("/api/payment-mandates")
    def create_mandate(raw: dict, sid=Depends(buyer)):
        return payments.mandate(sid, raw)

    @app.post("/api/payments")
    def prepare_payment(raw: dict, request: Request, sid=Depends(buyer)):
        bind_payment(request, raw.get("mandate_id"))
        return payments.prepare(sid, raw)

    @app.get("/api/payments/{pid}")
    def payment(pid: str, sid=Depends(buyer)):
        return payments.get(sid, pid)

    @app.post("/api/payments/{pid}/challenge")
    def payment_challenge(pid: str, raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, set(), "payment challenge")
        bind_payment(request, payments.get(sid, pid)["mandate_id"])
        return payments.challenge(sid, pid)

    @app.post("/api/payments/{pid}/submit")
    def submit_payment(pid: str, raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, {"payment_signature"}, "signed payment submission")
        bind_payment(request, payments.get(sid, pid)["mandate_id"])
        return payments.submit(sid, pid, raw["payment_signature"])

    @app.post("/api/payments/{pid}/reconcile")
    def reconcile_payment(pid: str, raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, set(), "payment reconciliation")
        bind_payment(request, payments.get(sid, pid)["mandate_id"])
        return payments.reconcile(sid, pid)

    @app.post("/api/payments/{pid}/cancel")
    def cancel_payment(pid: str, raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, set(), "unsent payment cancellation")
        bind_payment(request, payments.get(sid, pid)["mandate_id"])
        return payments.cancel_unsigned(sid, pid)

    @app.post("/api/orders")
    def order(raw: dict, request: Request, sid=Depends(buyer)):
        if settings.mode == "production":
            raise HTTPException(403, "test-credit orders are disabled in production")
        require_keys(raw, {"policy_id", "offer_id", "request", "idempotency_key"}, "order")
        bind_order(request, raw["policy_id"])
        return store.reserve(sid, **raw)

    @app.get("/api/orders/{oid}")
    def get_order(oid: str, sid=Depends(buyer)):
        return store.order(sid, oid)

    @app.post("/api/orders/{oid}/run")
    def run_order(oid: str, request: Request, sid=Depends(buyer)):
        bind_order(request, store.order(sid, oid)["policy_id"])
        return engine.run(sid, oid)

    @app.post("/api/orders/{oid}/cancel")
    def cancel_order(oid: str, request: Request, sid=Depends(buyer)):
        bind_order(request, store.order(sid, oid)["policy_id"])
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

    @app.get("/wallet.js")
    def wallet_javascript():
        return FileResponse(web / "wallet.js")

    @app.get("/assets/phantom-wallet.png")
    def wallet_logo():
        return FileResponse(web / "assets/phantom-wallet.png")

    return app


def app_factory():
    return create_app()
