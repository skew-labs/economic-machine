"""Hashed agent credentials; console owners alone grant authority.

Keys share their owner's workspace but never acquire its administrative role.
An order-writing key is bound to one immutable, shared spending policy.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass

from economic_machine.values import MachineError, canonical, require_keys

from .domain import identifier

SCOPES = frozenset({"read", "demands:write", "supplies:write", "orders:write", "payments:request", "engine:read", "engine:write", "data:read", "agents:run"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, prefix TEXT NOT NULL,
 scopes TEXT NOT NULL, policy_id TEXT REFERENCES policies(id), created INTEGER NOT NULL,
 expires INTEGER NOT NULL, last_used INTEGER, revoked_at INTEGER);
CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys(session_id,created);
"""


@dataclass(frozen=True)
class Principal:
    session_id: str
    key_id: str | None = None
    scopes: frozenset = frozenset()
    policy_id: str | None = None
    payment_mandate_id: str | None = None
    engine_agent_id: str | None = None

    @property
    def owner(self):
        return self.key_id is None


class Access:
    def __init__(self, store, mode="development"):
        self.store, self.mode = store, mode
        with store.connect() as db:
            # executescript commits implicitly; individual statements retain Store's transaction.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            if "payment_mandate_id" not in {r[1] for r in db.execute("PRAGMA table_info(api_keys)")}:
                db.execute("ALTER TABLE api_keys ADD COLUMN payment_mandate_id TEXT")
            if "engine_agent_id" not in {r[1] for r in db.execute("PRAGMA table_info(api_keys)")}:
                db.execute("ALTER TABLE api_keys ADD COLUMN engine_agent_id TEXT")

    def _owner(self, db, sid):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row or row["expires"] <= self.store.clock():
            raise MachineError("session expired or invalid")
        return row

    def resolve(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 120:
            raise MachineError("credential required")
        if not token.startswith(("em_test_", "em_live_")):
            return Principal(self.store.authenticate(token))
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE token_hash=?", (
                hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            now = self.store.clock()
            if not row or row["revoked_at"] is not None or row["expires"] <= now:
                raise MachineError("API key expired, revoked or invalid")
            self._owner(db, row["session_id"])
            db.execute("UPDATE api_keys SET last_used=? WHERE id=?", (now, row["id"]))
            return Principal(row["session_id"], row["id"], frozenset(json.loads(row["scopes"])),
                             row["policy_id"], row["payment_mandate_id"], row["engine_agent_id"])

    def create(self, sid, raw):
        require_keys({k: v for k, v in raw.items() if k not in {"payment_mandate_id", "engine_agent_id"}},
                     {"name", "scopes", "policy_id", "ttl_seconds"}, "API key")
        name, scopes, ttl, pid = raw["name"], raw["scopes"], raw["ttl_seconds"], raw["policy_id"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise MachineError("key name must contain 1 to 60 printable characters")
        if (not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) for s in scopes)
                or len(scopes) != len(set(scopes)) or not set(scopes) <= SCOPES):
            raise MachineError("choose unique supported key permissions")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= 86400:
            raise MachineError("key lifetime must be 60 to 86400 seconds")
        if "orders:write" in scopes:
            identifier(pid, "spending policy id")
        elif pid is not None:
            raise MachineError("only order-writing keys bind a spending policy")
        mandate_id = raw.get("payment_mandate_id")
        engine_agent_id = raw.get("engine_agent_id")
        if "agents:run" in scopes:
            identifier(engine_agent_id, "engine agent ID")
            if not set(scopes) <= {"agents:run", "engine:read"}:
                raise MachineError("agent-bound keys cannot acquire unbound write permissions")
        elif engine_agent_id is not None:
            raise MachineError("an engine agent binding requires agents:run permission")
        if mandate_id is not None:
            identifier(mandate_id, "payment mandate id")
            if "payments:request" not in scopes:
                raise MachineError("a payment mandate requires payment permission")
        now, kid = self.store.clock(), "key-" + secrets.token_hex(12)
        token = ("em_live_" if self.mode == "production" else "em_test_") + secrets.token_urlsafe(32)
        with self.store.connect() as db:
            owner = self._owner(db, sid)
            expires = min(now + ttl, owner["expires"])
            if pid:
                policy = db.execute("SELECT * FROM policies WHERE id=? AND session_id=?", (pid, sid)).fetchone()
                if not policy or policy["expires"] <= now:
                    raise MachineError("active owned spending policy required")
                expires = min(expires, policy["expires"])
            if mandate_id:
                mandate = db.execute("SELECT * FROM payment_mandates WHERE id=? AND owner=?", (mandate_id, sid)).fetchone()
                if not mandate or mandate["expires"] <= now:
                    raise MachineError("active owned payment mandate required")
                expires = min(expires, mandate["expires"])
            count = db.execute("SELECT COUNT(*) FROM api_keys WHERE session_id=? "
                "AND revoked_at IS NULL AND expires>?", (sid, now)).fetchone()[0]
            if count >= 100:
                raise MachineError("active API key limit reached; revoke an unused key")
            db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)", (
                kid, sid, hashlib.sha256(token.encode()).hexdigest(), name.strip(), token[:16],
                canonical(sorted(scopes)).decode(), pid, now, expires, mandate_id, engine_agent_id))
            self.store._event(db, kid + ":created", "API_KEY_CREATED", {
                "key_id": kid, "session_id": sid, "scopes": sorted(scopes), "policy_id": pid,
                "payment_mandate_id": mandate_id,
                "engine_agent_id": engine_agent_id,
                "expires": expires, "at": now})
            metadata = self._public(db.execute("SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone())
        return {"key": metadata, "secret": token, "shown_once": True}

    def _public(self, row):
        status = "revoked" if row["revoked_at"] is not None else (
            "expired" if row["expires"] <= self.store.clock() else "active")
        return {"id": row["id"], "name": row["name"], "prefix": row["prefix"],
                "scopes": json.loads(row["scopes"]), "policy_id": row["policy_id"],
                "payment_mandate_id": row["payment_mandate_id"],
                "engine_agent_id": row["engine_agent_id"],
                "created": row["created"], "expires": row["expires"], "last_used": row["last_used"],
                "revoked_at": row["revoked_at"], "status": status, "mode": self.mode.upper()}

    def list(self, sid):
        with self.store.connect() as db:
            owner = self._owner(db, sid)
            return {"keys": [self._public(row) for row in db.execute(
                "SELECT * FROM api_keys WHERE session_id=? ORDER BY created DESC,rowid DESC", (sid,))],
                "supported_scopes": sorted(SCOPES), "workspace_expires": owner["expires"]}

    def revoke(self, sid, kid):
        identifier(kid, "API key id")
        with self.store.connect() as db:
            self._owner(db, sid)
            row = db.execute("SELECT * FROM api_keys WHERE id=? AND session_id=?", (kid, sid)).fetchone()
            if not row:
                raise MachineError("API key not found")
            if row["revoked_at"] is None:
                now = self.store.clock()
                db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (now, kid))
                self.store._event(db, kid + ":revoked", "API_KEY_REVOKED", {
                    "key_id": kid, "session_id": sid, "at": now})
                row = db.execute("SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone()
            return self._public(row)

    @staticmethod
    def authorize(principal, method, path):
        if principal.owner:
            return
        # Positive route allowlist: future mutations default to owner-only.
        parts = path.strip("/").split("/")
        scope = None
        if path.startswith("/api/engine/"):
            if len(parts) in {4, 5, 6} and parts[2] == "agents" and (
                    (method == "GET" and (len(parts) == 4 or (len(parts) == 6 and parts[4] == "runs")))
                    or (method == "POST" and len(parts) == 5 and parts[4] == "runs")):
                if principal.engine_agent_id != parts[3]:
                    raise PermissionError("API key is bound to another engine agent")
                scope = "agents:run"
            elif method == "GET" and (path in {"/api/engine/overview", "/api/engine/profiles", "/api/engine/control"}
                    or (len(parts) == 5 and parts[2:4] == ["trade", "orders"])):
                scope = "engine:read"
            elif method == "POST" and (path in {"/api/engine/trade/orders", "/api/engine/usage", "/api/engine/programs/compile", "/api/engine/native/evaluate", "/api/engine/native/programs/compile", "/api/engine/native/programs/evaluate"}
                    or (len(parts) == 6 and parts[2:4] == ["trade", "orders"] and parts[5] == "reconcile")
                    or (len(parts) == 5 and parts[2] == "connections" and parts[4] == "sync")):
                scope = "engine:write"
        elif method == "GET" and path.startswith("/api/data/"):
            scope = "data:read"
        elif method == "GET" and len(parts) >= 2 and parts[1] in {"workspace", "market", "orders", "demands", "payments"}:
            scope = "read"
        elif method == "POST":
            if path == "/api/demands":
                scope = "demands:write"
            elif path == "/api/supplies" or (len(parts) == 4 and parts[1] == "supplies" and parts[3] == "refresh"):
                scope = "supplies:write"
            elif path == "/api/orders" or (len(parts) == 4 and parts[1] == "orders" and parts[3] in {"run", "cancel"}):
                scope = "orders:write"
            elif (path == "/api/payments"
                  or (len(parts) == 4 and parts[1] == "matches" and parts[3] == "payment-request")
                  or (len(parts) == 4 and parts[1] == "payments" and parts[3] in {"challenge", "submit", "reconcile", "cancel"})):
                scope = "payments:request"
        if scope is None or scope not in principal.scopes:
            raise PermissionError("API key does not permit this operation")

    @staticmethod
    def bind_order(principal, policy_id):
        if not principal.owner and principal.policy_id != policy_id:
            raise PermissionError("order is outside the API key's spending policy")

    @staticmethod
    def bind_payment(principal, mandate_id):
        if not principal.owner and (not principal.payment_mandate_id or principal.payment_mandate_id != mandate_id):
            raise PermissionError("payment is outside the API key's approved mandate")
