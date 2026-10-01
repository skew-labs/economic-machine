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

SCOPES = frozenset({"read", "demands:write", "supplies:write", "orders:write", "payments:request"})
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

    @property
    def owner(self):
        return self.key_id is None


class Access:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            # executescript commits implicitly; individual statements retain Store's transaction.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)

    def _owner(self, db, sid):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row or row["expires"] <= self.store.clock():
            raise MachineError("session expired or invalid")
        return row

    def resolve(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 120:
            raise MachineError("credential required")
        if not token.startswith("em_test_"):
            return Principal(self.store.authenticate(token))
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE token_hash=?", (
                hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            now = self.store.clock()
            if not row or row["revoked_at"] is not None or row["expires"] <= now:
                raise MachineError("API key expired, revoked or invalid")
            self._owner(db, row["session_id"])
            db.execute("UPDATE api_keys SET last_used=? WHERE id=?", (now, row["id"]))
            return Principal(row["session_id"], row["id"], frozenset(json.loads(row["scopes"])), row["policy_id"])

    def create(self, sid, raw):
        require_keys(raw, {"name", "scopes", "policy_id", "ttl_seconds"}, "API key")
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
        now, kid = self.store.clock(), "key-" + secrets.token_hex(12)
        token = "em_test_" + secrets.token_urlsafe(32)
        with self.store.connect() as db:
            owner = self._owner(db, sid)
            expires = min(now + ttl, owner["expires"])
            if pid:
                policy = db.execute("SELECT * FROM policies WHERE id=? AND session_id=?", (pid, sid)).fetchone()
                if not policy or policy["expires"] <= now:
                    raise MachineError("active owned spending policy required")
                expires = min(expires, policy["expires"])
            count = db.execute("SELECT COUNT(*) FROM api_keys WHERE session_id=? "
                "AND revoked_at IS NULL AND expires>?", (sid, now)).fetchone()[0]
            if count >= 100:
                raise MachineError("active API key limit reached; revoke an unused key")
            db.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)", (
                kid, sid, hashlib.sha256(token.encode()).hexdigest(), name.strip(), token[:16],
                canonical(sorted(scopes)).decode(), pid, now, expires))
            self.store._event(db, kid + ":created", "API_KEY_CREATED", {
                "key_id": kid, "session_id": sid, "scopes": sorted(scopes), "policy_id": pid,
                "expires": expires, "at": now})
            metadata = self._public(db.execute("SELECT * FROM api_keys WHERE id=?", (kid,)).fetchone())
        return {"key": metadata, "secret": token, "shown_once": True}

    def _public(self, row):
        status = "revoked" if row["revoked_at"] is not None else (
            "expired" if row["expires"] <= self.store.clock() else "active")
        return {"id": row["id"], "name": row["name"], "prefix": row["prefix"],
                "scopes": json.loads(row["scopes"]), "policy_id": row["policy_id"],
                "created": row["created"], "expires": row["expires"], "last_used": row["last_used"],
                "revoked_at": row["revoked_at"], "status": status, "mode": "TEST"}

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
        if method == "GET" and len(parts) >= 2 and parts[1] in {"workspace", "market", "orders"}:
            scope = "read"
        elif method == "POST":
            if path == "/api/demands":
                scope = "demands:write"
            elif path == "/api/supplies" or (len(parts) == 4 and parts[1] == "supplies" and parts[3] == "refresh"):
                scope = "supplies:write"
            elif path == "/api/orders" or (len(parts) == 4 and parts[1] == "orders" and parts[3] in {"run", "cancel"}):
                scope = "orders:write"
            elif len(parts) == 4 and parts[1] == "matches" and parts[3] == "payment-request":
                scope = "payments:request"
        if scope is None or scope not in principal.scopes:
            raise PermissionError("API key does not permit this operation")

    @staticmethod
    def bind_order(principal, policy_id):
        if not principal.owner and principal.policy_id != policy_id:
            raise PermissionError("order is outside the API key's spending policy")
