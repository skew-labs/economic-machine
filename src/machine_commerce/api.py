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
from .checkout import DEFAULT_PLANS, Checkout
from .domain import CATALOG, DEFAULT_REQUESTS, now_seconds, terms_hash
from .market import Market
from .operations import Operations, Settings, config_file
from .payments import Payments
from .providers import CommerceEngine, Workers
from .store import Store
from .wallet_auth import WalletAuth
from .x402 import address

LOGGER = logging.getLogger(__name__)


def create_app(db_path=None, clock=now_seconds, workers=None, settings=None, payment_transport=None, payment_chain=None,
               subscription_plans=None):
    root = Path(__file__).resolve().parents[2]
    web = root / "web"
    store = Store(db_path or os.environ.get("COMMERCE_DB", str(root / "runtime/commerce.sqlite3")), clock)
    engine = CommerceEngine(store, workers or Workers(clock))
    market = Market(store)
    settings = (settings or Settings.environment()).validate()
    payments = Payments(store, market, settings.resources, payment_transport, payment_chain)
    plan_file = os.environ.get("MACHINE_SUBSCRIPTIONS_FILE")
    checkout = Checkout(store, market, payments, subscription_plans if subscription_plans is not None
                        else config_file(plan_file) if plan_file else DEFAULT_PLANS)
    operations = Operations(store, settings)
    access = Access(store, settings.mode)
    wallet_auth = WalletAuth(store, settings.origin)
    from machine_engine.routes import HostedWorkspaces, engine_routes
    hosted_engine = HostedWorkspaces(Path(store.path).parent / "engine-workspaces", clock)

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
        engine_worker = asyncio.create_task(hosted_engine.loop())
        yield
        task.cancel()
        recovery.cancel()
        engine_worker.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await recovery
        with suppress(asyncio.CancelledError):
            await engine_worker

    app = FastAPI(title="Economic Machine Commerce", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.access = access
    app.state.wallet_auth = wallet_auth
    app.state.market, app.state.payments, app.state.operations = market, payments, operations
    app.state.checkout = checkout
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

    def engine_workspace(request: Request, sid=Depends(buyer)):
        identity = wallet_auth.identity(sid)
        if identity:
            subject = f"wallet:{identity['chain_id']}:{identity['address'].lower()}"
        elif operations.known_operator(sid):
            with store.connect() as db:
                subject = "operator:" + db.execute("SELECT subject FROM operator_identities WHERE session_id=?", (sid,)).fetchone()[0]
        else:
            subject = "development:" + sid
        return hosted_engine.get(subject)

    def engine_owner(request: Request, sid=Depends(buyer)):
        if not request.state.principal.owner:
            raise HTTPException(403, "owner approval required")

    app.include_router(engine_routes(engine_workspace, require_owner=engine_owner))

    from .datapass import DataProducts
    data_products = DataProducts()

    @app.get("/api/data/catalog")
    def data_catalog(sid=Depends(buyer)):
        return data_products.catalog()

    @app.get("/api/data/licenses/{token_id}/delivery")
    def data_delivery(token_id: int, version: str | None = None, sid=Depends(buyer)):
        try:
            return data_products.delivery(token_id, wallet_auth.identity(sid), version)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/api/data/purchase-plan")
    def data_plan(purchase_id: str, version: str | None = None, sid=Depends(buyer)):
        return data_products.plan(wallet_auth.identity(sid), purchase_id, version)

    @app.get("/api/data/purchase-status")
    def data_purchase_status(purchase_id: str, version: str | None = None, sid=Depends(buyer)):
        try:
            return data_products.purchase_status(wallet_auth.identity(sid), purchase_id, version)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/api/data/release-plan")
    def data_release(raw: dict, request: Request, sid=Depends(buyer), approved=Depends(engine_owner)):
        require_keys(raw, {"price_atoms", "sale_duration_seconds"}, "dataset release")
        return data_products.registration(wallet_auth.identity(sid), raw["price_atoms"], raw["sale_duration_seconds"])

    @app.post("/api/data/sale-plan")
    def data_sale(raw: dict, request: Request, sid=Depends(buyer), approved=Depends(engine_owner)):
        require_keys(raw, {"token_id", "price_atoms", "sale_duration_seconds", "version"}, "license sale")
        return data_products.sell(wallet_auth.identity(sid), raw["token_id"], raw["price_atoms"], raw["sale_duration_seconds"], raw["version"])

    @app.get("/api/data/resale-plan")
    def data_resale(token_id: str, purchase_id: str, min_remaining_seconds: int = 600, version: str | None = None, sid=Depends(buyer)):
        return data_products.resale(wallet_auth.identity(sid), token_id, purchase_id, min_remaining_seconds, version)

    @app.post("/api/data/deployment-plan")
    def data_deployment(request: Request, sid=Depends(buyer), approved=Depends(engine_owner)):
        from .datapass import CHAIN_ID, deployment_draft
        identity = wallet_auth.identity(sid)
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise HTTPException(403, "authenticated Arbitrum Sepolia wallet required")
        return deployment_draft(identity["address"])

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
        degraded = app.state.timer_error or app.state.payment_worker_error or hosted_engine.last_error
        return JSONResponse({"status": "degraded" if degraded else "ok", "mode": settings.mode,
                "timer_error": app.state.timer_error, "product": "economic-machine-commerce",
                "payment_worker_error": app.state.payment_worker_error, "x402_configured": bool(settings.resources),
                "engine_worker_error": hosted_engine.last_error, "engine_sync_worker": "SERVER_SIDE_DURABLE_JOBS",
                "settlement_mode": "X402_EIP3009_AND_TEST_LEDGER" if settings.resources else "SANDBOX_LEDGER", "onchain_deployment": None,
                "customer_signing_authority": "NONE"}, status_code=503 if degraded else 200)

    @app.get("/api/catalog")
    def catalog():
        return {"offers": [{**offer, "default_request": DEFAULT_REQUESTS[offer_id],
                             "terms_hash": terms_hash(offer_id)} for offer_id, offer in CATALOG.items()],
                "settlement_mode": "SANDBOX_LEDGER", "asset": "TEST_CREDIT"}

    @app.get("/api/commerce/catalog")
    def commerce_catalog():
        return checkout.catalog()

    @app.get("/api/commerce/checkouts")
    def commerce_checkouts(sid=Depends(buyer)):
        return checkout.snapshot(sid)

    @app.post("/api/commerce/checkouts")
    def commerce_quote(raw: dict, sid=Depends(buyer)):
        return checkout.quote(sid, raw)

    @app.get("/api/commerce/checkouts/{cid}")
    def commerce_detail(cid: str, sid=Depends(buyer)):
        return checkout.get(sid, cid)

    @app.get("/api/commerce/subscriptions/{plan_id}/delivery")
    def subscription_delivery(plan_id: str, sid=Depends(buyer)):
        grant = checkout.require_access(sid, plan_id)
        if plan_id != "atlas-monthly":
            raise HTTPException(404, "this subscription has no hosted Atlas delivery adapter")
        from .atlas import load_report
        report = load_report(data_products.path)
        return {"entitlement": grant, "report": report["derived"], "report_sha256": report["report_sha256"],
                "source_observation_root": report["derived"]["source_observation_root"], "usage_rights": "internal-use",
                "assurance": "PUBLIC_LIST_PRICE_RESEARCH_NOT_CAPACITY_OR_EXECUTABLE_QUOTE"}

    @app.post("/api/commerce/checkouts/{cid}/prepare")
    def commerce_prepare(cid: str, raw: dict, request: Request, sid=Depends(buyer)):
        require_keys(raw, {"mandate_id"}, "checkout preparation")
        bind_payment(request, raw["mandate_id"])
        identity = wallet_auth.identity(sid)
        if identity:
            mandates = payments.snapshot(sid)["mandates"]
            mandate = next((m for m in mandates if m["id"] == raw["mandate_id"]), None)
            if not mandate or address(mandate["payer"]) != address(identity["address"]):
                raise HTTPException(403, "payment limit must use your signed-in wallet")
        return checkout.prepare(sid, cid, raw["mandate_id"])

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
    def create_key(raw: dict, request: Request, sid=Depends(buyer)):
        if raw.get("engine_agent_id") is not None:
            agent = engine_workspace(request, sid).control.agent(raw["engine_agent_id"], active=True)
            ttl = raw.get("ttl_seconds")
            if type(ttl) is int:
                raw = raw | {"ttl_seconds": min(ttl, agent["policy_expires_at"] - int(clock()))}
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

    @app.get("/operations.js")
    def operations_javascript():
        return FileResponse(web / "operations.js")

    @app.get("/operations.css")
    def operations_stylesheet():
        return FileResponse(web / "operations.css")

    @app.get("/workspace.css")
    def workspace_stylesheet():
        return FileResponse(web / "workspace.css")

    @app.get("/commerce.js")
    def commerce_javascript():
        return FileResponse(web / "commerce.js")

    @app.get("/commerce.css")
    def commerce_stylesheet():
        return FileResponse(web / "commerce.css")

    @app.get("/assets/app-engine.svg")
    def engine_logo():
        return FileResponse(web / "assets/app-engine.svg")

    @app.get("/console-theme.css")
    def console_stylesheet():
        return FileResponse(web / "console-theme.css")

    @app.get("/assets/phantom-wallet.png")
    def wallet_logo():
        return FileResponse(web / "assets/phantom-wallet.png")

    @app.get("/assets/ui-icons.svg")
    def ui_icons():
        return FileResponse(web / "assets/ui-icons.svg")

    return app


def app_factory():
    return create_app()
