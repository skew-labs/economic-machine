"""Owner-authenticated loopback console for a user's own Economic Machine."""

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from economic_machine.compiler import compile_program
from economic_machine.values import MachineError, require_keys
from machine_commerce.domain import money_string

from .connections import PROFILES
from .workspace import Workspace, now_iso

ROOT = Path(__file__).resolve().parents[2]
ASSETS = {"assistant.js", "assistant.css", "swap-wallet.js", "swap-crypto.js","app.js", "app.css", "wallet.js", "console-theme.css", "operations.js", "operations.css", "workspace.css", "commerce.js", "commerce.css",
          "agents.js", "data.js", "tasks.js", "mining.js", "tasks.css", "assets/ui-icons.svg", "assets/PHOSPHOR-LICENSE.txt", "assets/app-engine.svg"}


def create_engine_app(db_path, *, admin_token=None, origin="http://127.0.0.1:8800", workspace=None, clock=time.time):
    token = admin_token or os.environ.get("ENGINE_ADMIN_TOKEN", "")
    if not isinstance(token, str) or not 32 <= len(token) <= 256 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Set ENGINE_ADMIN_TOKEN to a private random value of at least 32 characters")
    try:
        parsed = urlsplit(origin)
        loopback = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.username is None and parsed.password is None
                    and parsed.port is not None and 1024 <= parsed.port <= 65535
                    and not parsed.path and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        loopback = False
    if not loopback:
        raise ValueError("Engine console is bound to loopback")
    work = workspace or Workspace(db_path, clock=clock)
    @asynccontextmanager
    async def lifespan(_app):
        worker = asyncio.create_task(work.scheduler.loop())
        async def reconcile_orders():
            while True:
                try:
                    await asyncio.to_thread(work.control.recover_once)
                    for oid in work.trading.pending_ids():
                        await asyncio.to_thread(work.trading.reconcile, oid)
                except Exception:  # noqa: BLE001 - bounded retry, no exception or credentials logged.
                    _app.state.recovery_error = "VENUE_RECOVERY_DEGRADED"
                await asyncio.sleep(15)
        recovery = asyncio.create_task(reconcile_orders())
        try:
            yield
        finally:
            worker.cancel()
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            with suppress(asyncio.CancelledError):
                await recovery

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workspace = work

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.url.path.startswith("/api/"):
            if request.headers.get("Origin") not in {None, origin}:
                return JSONResponse({"error": "LOCAL_ORIGIN_REQUIRED"}, status_code=403)
            authorization = request.headers.get("Authorization", "")
            if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
                try:
                    if not authorization.startswith("Bearer "):
                        raise MachineError("AGENT_KEY_REQUIRED")
                    aid = work.control.resolve_key(authorization.removeprefix("Bearer "))
                except MachineError:
                    return JSONResponse({"error": "LOCAL_OWNER_OR_AGENT_AUTH_REQUIRED"}, status_code=401)
                parts = request.url.path.strip("/").split("/")
                own_agent = len(parts) in {4, 5, 6} and parts[:3] == ["api", "engine", "agents"] and parts[3] == aid
                allowed = own_agent and ((request.method == "GET" and (len(parts) == 4 or (len(parts) == 6 and parts[4] == "runs"))) or (request.method == "POST" and len(parts) == 5 and parts[4] == "runs"))
                if not allowed:
                    return JSONResponse({"error": "AGENT_BOUND_OPERATION_ONLY"}, status_code=403)
            body = bytearray()
            async for part in request.stream():
                body.extend(part)
                if len(body) > 200_000:
                    return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
            request._body = bytes(body)
            # Starlette routes consume its cached body after the streaming bound.
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Referrer-Policy": "no-referrer"})
        return response

    @app.exception_handler(MachineError)
    async def machine_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    async def raw(request):
        try:
            body = json.loads(await request.body())
            if not isinstance(body, dict):
                raise TypeError()
            return body
        except (ValueError, TypeError, UnicodeError) as exc:
            raise MachineError("JSON_OBJECT_REQUIRED") from exc

    @app.get("/api/engine/overview")
    def overview():
        return work.overview()

    @app.get("/api/engine/profiles")
    def profiles():
        return {"profiles": PROFILES, "secret_storage": "USER_ENVIRONMENT_ONLY"}

    @app.post("/api/engine/agents/{aid}/keys")
    async def agent_key(aid, request: Request):
        data = await raw(request)
        require_keys(data, {"ttl_seconds"}, "local agent key")
        return work.control.issue_key(aid, data["ttl_seconds"])

    @app.post("/api/engine/agent-keys/{kid}/revoke")
    async def revoke_agent_key(kid, request: Request):
        require_keys(await raw(request), set(), "local key revocation")
        return work.control.revoke_key(kid)

    @app.post("/api/engine/connections")
    async def connect(request: Request):
        return work.connect(await raw(request))

    @app.post("/api/engine/connections/{cid}/sync")
    def sync(cid):
        return work.sync(cid)

    @app.post("/api/engine/connections/{cid}/disconnect")
    def disconnect(cid):
        return work.disconnect(cid)

    @app.post("/api/engine/usage")
    async def usage(request: Request):
        return work.record_usage(await raw(request))

    @app.post("/api/engine/state")
    async def install_state(request: Request):
        return work.runtime.install_state(await raw(request))

    @app.post("/api/engine/deltas")
    async def delta(request: Request):
        data = await raw(request)
        require_keys(data, {"event_id", "delta"}, "state event")
        return work.runtime.ingest(data["delta"], event_id=data["event_id"])

    @app.post("/api/engine/programs")
    async def program(request: Request):
        return work.runtime.register_program(await raw(request))

    @app.post("/api/engine/programs/compile")
    async def compile_only(request: Request):
        return {"program": compile_program(await raw(request)), "execution_authority": "NONE"}

    @app.post("/api/engine/programs/{pid}/{operation}")
    async def control(pid, operation, request: Request):
        data = await raw(request)
        if operation == "evaluate":
            require_keys(data, {"at"}, "evaluation")
            return work.runtime.evaluate(pid, at=data["at"])
        if operation in {"pause", "resume"}:
            require_keys(data, {"reason"}, "program control")
            method = work.runtime.pause_program if operation == "pause" else work.runtime.resume_program
            return method(pid, reason=data["reason"])
        raise MachineError("SUPPORTED_PROGRAM_OPERATION_REQUIRED")

    @app.post("/api/engine/receipts/{rid}/lock")
    async def lock(rid, request: Request):
        data = await raw(request)
        require_keys(data, {"at"}, "capital lock")
        return work.runtime.begin_execution(rid, at=data["at"])

    @app.get("/api/engine/receipts/{rid}/verify")
    def verify(rid):
        return {"receipt_hash": rid, "verified_replay": work.runtime.verify_receipt(rid)}

    @app.post("/api/engine/tick")
    def tick():
        return work.runtime.tick(at=now_iso(clock()))

    @app.get("/", response_class=HTMLResponse)
    @app.get("/engine", response_class=HTMLResponse)
    @app.get("/console", response_class=HTMLResponse)
    def console():
        html = (ROOT / "web/index.html").read_text().replace("</head>", '<meta name="engine-auth" content="LOCAL_OWNER_TOKEN"><meta name="engine-mode" content="SELF_HOSTED"></head>')
        return HTMLResponse(html)

    from .routes import engine_routes
    router = engine_routes(lambda: work)
    existing = {route.path for route in app.routes if hasattr(route, "path")}
    router.routes = [route for route in router.routes if getattr(route, "path", None) not in existing]
    app.include_router(router)

    @app.get("/{asset:path}")
    def assets(asset):
        if asset not in ASSETS:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        return FileResponse(ROOT / "web" / asset)

    return app


def recorded_overview(bundle):
    """Public showcase of an actual purchase; no invented connections or holdings."""
    payment, policy, capital = bundle["payment"], bundle["policy"], bundle["capital"]
    return {"mode": "RECORDED", "read_only": True, "as_of": bundle["checked_at"],
        "execution_authority": "NONE", "payment_scope": "EXTERNAL_DATA_AND_COMPUTE_ONLY",
        "venue_trades_use_venue_api": True, "capital_aggregation": "NO_CROSS_ASSET_VALUATION_WITHOUT_PRICE_EVIDENCE",
        "connections": [{"id": "recorded-sepolia", "name": "Buyer wallet", "profile": "arbitrum-sepolia-wallet",
            "kind": "wallet", "status": "RECORDED", "network": "eip155:421614", "stale": True,
            "observed_at": None, "updated_at": bundle["checked_at"], "error_code": None,
            "operations": ["READ_BALANCES"], "secret_storage": "EXTERNAL_DISPOSABLE_BUYER_CLI",
            "snapshot": {"address": bundle["authorization"]["signer"], "assurance": capital["balance_evidence"],
                "block_number": payment["receipt_block"]}}],
        "assets": [{"symbol": "USDC", "quantity": money_string(capital["buyer_after_atoms"]),
            "network": "eip155:421614", "connection": "Buyer wallet", "connection_id": "recorded-sepolia",
            "stale": True, "observed_at": None, "balance_at_block": payment["receipt_block"],
            "assurance": capital["balance_evidence"]}],
        "positions": [], "orders": [], "usage": [], "usage_assurance": "NO_LIVE_USAGE_IMPORT",
        "programs": [{"id": bundle["agreement"]["id"], "name": "DATA BUYER", "version": 1,
            "network": "eip155:421614", "status": "COMPLETED", "program_hash": bundle["agreement"]["terms_hash"],
            "sandbox": {"asset": "test USDC", "capital_limit": policy["budget"],
                "allowed_protocols": [bundle["supplier"]["name"]]},
            "approval": "EXTERNAL_BUYER_EIP3009_SIGNATURE_VERIFIED", "source": bundle["agreement"]["terms"]}],
        "runs": [{"id": payment["id"], "program_id": bundle["agreement"]["id"], "status": payment["status"],
            "reason": None, "verification": True, "receipt": {"tx_hash": payment["tx_hash"],
                "receipt_block": payment["receipt_block"], "terms_hash": bundle["agreement"]["terms_hash"],
                "artifact_hash": bundle["delivery"]["artifact_hash"], "spent_atoms": capital["spent_atoms"],
                "reserved_atoms": capital["reserved_atoms"], "external_signer_verified": True}}],
        "payments": [payment], "runtime": {"journal_integrity": True, "execution_port": "RECORDED_X402_PURCHASE",
            "active_reservations": 0, "events": len(bundle["audit"]["timeline"]), "receipts": 1},
        "trade_bundle_hash": bundle["bundle_hash"], "evidence_url": "/commerce/submission"}
