"""Small, isolated wallet-signed gas-acquisition portal. No secrets required."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from economic_machine.values import MachineError

from .gas_router import GasRouter

ROOT = Path(__file__).resolve().parents[2]


class SwapStore:
    def __init__(self, path, router, guard=None):
        self.path, self.router, self.lock = str(path), router, threading.RLock()
        self.guard = guard
        parent = Path(path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or Path(path).is_symlink():
            raise MachineError("PRIVATE_SWAP_STORE_REQUIRED")
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS swaps (id TEXT PRIMARY KEY, owner TEXT, state TEXT, payload TEXT, created INTEGER)")
        os.chmod(self.path, 0o600)

    def call(self, action, body):
        if not isinstance(body, dict):
            raise MachineError("OBJECT_REQUIRED")
        with self.lock, sqlite3.connect(self.path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            if action == "quote":
                if (set(body) != {"owner", "amount_atoms"} or not isinstance(body["amount_atoms"], str)
                        or not re.fullmatch(r"[1-9][0-9]{0,77}", body["amount_atoms"])):
                    raise MachineError("OWNER_AND_EXACT_USDC_ATOMS_REQUIRED")
                if db.execute("SELECT count(*) FROM swaps").fetchone()[0] >= 10000:
                    raise MachineError("SWAP_STORE_CAPACITY")
                p = self.router.prepare(body["owner"], int(body["amount_atoms"]))
                # A signed attempt must be reconciled, even across process restarts.
                pending = db.execute("SELECT 1 FROM swaps WHERE lower(owner)=lower(?) AND state IN ('UNKNOWN_RECONCILE_ONLY','SUBMITTED')", (p["owner"],)).fetchone()
                if pending:
                    raise MachineError("EXISTING_ORDER_MUST_BE_RECONCILED")
                p["id"] = secrets.token_hex(24)
                db.execute("INSERT INTO swaps VALUES (?,?,?,?,?)", (p["id"], p["owner"], p["status"], json.dumps(p), int(time.time())))
                return p
            if set(body) != ({"id"} if action == "status" else {"id", "signature"}):
                raise MachineError("EXACT_REQUEST_FIELDS_REQUIRED")
            if not isinstance(body["id"], str) or not re.fullmatch(r"[0-9a-f]{48}", body["id"]):
                raise MachineError("SWAP_ID_REQUIRED")
            row = db.execute("SELECT state,payload FROM swaps WHERE id=?", (body["id"],)).fetchone()
            if not row:
                raise MachineError("SWAP_NOT_FOUND")
            state, p = row[0], json.loads(row[1])
            if self.guard and action in {"order", "submit"}:
                self.guard(db, p)
            if action == "order":
                if state != "PERMIT_REQUIRED":
                    raise MachineError("PERMIT_ALREADY_USED_FOR_ORDER")
                p = self.router.order(p, body["signature"])
                db.execute("UPDATE swaps SET state=?,payload=? WHERE id=?", (p["status"], json.dumps(p), p["id"]))
                return p
            if action == "submit":
                if state in {"SUBMITTED", "UNKNOWN_RECONCILE_ONLY", "FILLED_FINALIZED"}:
                    return {"status": state, "order_uid": p["order_uid"], "safe_to_retry": False}
                if state != "ORDER_SIGNATURE_REQUIRED":
                    raise MachineError("SIGNED_ORDER_REQUIRED")
                order = self.router.submission(p, body["signature"])
                # Commit durable uncertainty *before* any externally submitted order.
                p["status"] = "UNKNOWN_RECONCILE_ONLY"
                db.execute("UPDATE swaps SET state=?,payload=? WHERE id=?", (p["status"], json.dumps(p), p["id"]))
                db.commit()
                try:
                    uid = self.router.submit(order)
                    if uid != p["order_uid"]:
                        raise MachineError("ORDER_UID_MISMATCH")
                except Exception:
                    return {"status": "UNKNOWN_RECONCILE_ONLY", "order_uid": p["order_uid"], "safe_to_retry": False}
                p["status"] = "SUBMITTED"
                # A concurrent reconciliation may already have verified finality.
                db.execute("UPDATE swaps SET state=?,payload=? WHERE id=? AND state='UNKNOWN_RECONCILE_ONLY'", (p["status"], json.dumps(p), p["id"]))
                actual = db.execute("SELECT state FROM swaps WHERE id=?", (p["id"],)).fetchone()[0]
                return {"status": actual, "order_uid": uid, "safe_to_retry": False}
            if action == "status":
                if state == "PERMIT_REQUIRED" and p["valid_to"] <= self.router.clock():
                    # No order was ever constructed or exposed for signing.
                    db.execute("UPDATE swaps SET state='EXPIRED_UNFILLED' WHERE id=?", (p["id"],))
                    return {"status": "EXPIRED_UNFILLED", "safe_to_retry": True, "chain_verified": False}
                if state == "ORDER_SIGNATURE_REQUIRED" and p["valid_to"] <= self.router.clock():
                    result = self.router.status(p)
                    if result["status"] == "EXPIRED_UNFILLED":
                        db.execute("UPDATE swaps SET state='EXPIRED_UNFILLED' WHERE id=?", (p["id"],))
                    return result
                if state not in {"SUBMITTED", "UNKNOWN_RECONCILE_ONLY", "FILLED_FINALIZED", "EXPIRED_UNFILLED"}:
                    return {"status": state, "safe_to_retry": False}
                result = self.router.status(p)
                if result["status"] in {"FILLED_FINALIZED", "EXPIRED_UNFILLED"}:
                    db.execute("UPDATE swaps SET state=? WHERE id=?", (result["status"], p["id"]))
                return result
            raise MachineError("UNKNOWN_OPERATION")


def create_gas_portal(store=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gate = asyncio.Semaphore(4)
    instance = store

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get("/commerce/swap")
    def page():
        return FileResponse(ROOT / "web/swap.html")

    @app.get("/commerce/swap-api/health")
    def health():
        return {"status": "ok", "version": "fuel-1", "chain_id": 42161,
                "signing_authority": "USER_WALLET_ONLY", "route": "COW_PERMIT_INTENT",
                "server_wallet_keys": False, "max_usdc_atoms": None,
                "amount_limit": "WALLET_BALANCE_AND_OWNER_POLICY"}

    @app.get("/commerce/swap-assets/{asset}")
    def static(asset: str):
        if asset not in {"swap.js", "swap-wallet.js", "swap.css", "swap-crypto.js"}:
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        return FileResponse(ROOT / "web" / asset)

    @app.post("/commerce/swap-api/{action}")
    async def api(action: str, request: Request):
        nonlocal instance
        if action not in {"quote", "order", "submit", "status"}:
            return JSONResponse({"error": "UNKNOWN_OPERATION"}, status_code=404)
        # The public service accepts same-origin JSON, not cross-site form posts.
        expected_origin = os.environ.get("SKEW_SWAP_ORIGIN", "https://machine.148-113-153-116.nip.io")
        if request.headers.get("origin") != expected_origin or request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "SAME_ORIGIN_JSON_REQUIRED"}, status_code=403)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 2048:
                return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
        try:
            parsed = json.loads(body)
            if instance is None:
                instance = SwapStore(os.environ.get("SKEW_SWAP_DB", str(ROOT / "runtime/gas-swaps.sqlite3")), GasRouter())
            async with gate:
                result = await asyncio.to_thread(instance.call, action, parsed)
            return JSONResponse(result)
        except MachineError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "INVALID_REQUEST_OR_QUOTE"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "UPSTREAM_UNAVAILABLE_RETRY_STATUS_ONLY"}, status_code=503)

    return app


app = create_gas_portal()
