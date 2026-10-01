"""Explicit production configuration, durable operator identity and admission limits."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from economic_machine.values import MachineError


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=128 * 1024 ** 2)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(derived).decode()


def valid_password_hash(encoded):
    try:
        marker, salt, expected = encoded.split("$")
        return (marker == "scrypt" and len(base64.b64decode(salt, validate=True)) == 16
                and len(base64.b64decode(expected, validate=True)) == 64)
    except (ValueError, TypeError, AttributeError):
        return False


def password_ok(password, encoded):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        return False
    try:
        _marker, salt, expected = encoded.split("$")
        if not valid_password_hash(encoded):
            return False
        actual = password_hash(password, base64.b64decode(salt, validate=True)).split("$")[2]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def config_file(path):
    if not path:
        return {}
    source = Path(path)
    if source.stat().st_size > 200_000:
        raise MachineError("configuration exceeds size limit")
    return json.loads(source.read_text())


@dataclass(frozen=True)
class Settings:
    mode: str = "development"
    origin: str | None = None
    operators: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)

    def validate(self):
        if self.mode not in {"development", "production"}:
            raise MachineError("explicit supported deployment mode required")
        if self.mode == "production":
            parsed = urlsplit(self.origin or "")
            if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
                    or parsed.fragment or parsed.username or parsed.password):
                raise MachineError("production requires an exact HTTPS console origin")
            if (not isinstance(self.operators, dict) or not 1 <= len(self.operators) <= 100
                    or any(not isinstance(k, str) or not 1 <= len(k) <= 60
                           or not valid_password_hash(v) for k, v in self.operators.items())):
                raise MachineError("production requires provisioned operator password hashes")
            if not self.resources:
                raise MachineError("production requires an approved x402 resource registry")
        return self

    @classmethod
    def environment(cls):
        return cls(os.environ.get("MACHINE_MODE", "development"), os.environ.get("MACHINE_PUBLIC_ORIGIN"),
                   config_file(os.environ.get("MACHINE_OPERATORS_FILE")),
                   config_file(os.environ.get("MACHINE_RESOURCES_FILE"))).validate()


class Operations:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings.validate()
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operator_identities (subject TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id))")
            db.execute("CREATE TABLE IF NOT EXISTS rate_buckets (key TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(key,window))")
        self.dummy = password_hash(secrets.token_urlsafe(32)) if settings.mode == "production" else None

    def login(self, raw):
        if set(raw) != {"username", "password"} or not isinstance(raw["username"], str):
            raise MachineError("operator username and password required")
        subject = raw["username"]
        encoded = self.settings.operators.get(subject, self.dummy)
        valid = password_ok(raw["password"], encoded)
        if not valid or subject not in self.settings.operators:
            raise PermissionError("invalid operator credentials")
        # The SQL transaction serializes first-login identity creation and rotations.
        with self.store.connect() as db:
            row = db.execute("SELECT session_id FROM operator_identities WHERE subject=?", (subject,)).fetchone()
            now, token = self.store.clock(), secrets.token_urlsafe(32)
            if row:
                sid = row["session_id"]
                db.execute("UPDATE sessions SET token_hash=?,expires=? WHERE id=?", (
                    hashlib.sha256(token.encode()).hexdigest(), now + 86400, sid))
            else:
                sid = "buyer-" + secrets.token_hex(12)
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,0,0,0,0)", (
                    sid, hashlib.sha256(token.encode()).hexdigest(), now, now + 86400))
                db.execute("INSERT INTO operator_identities VALUES (?,?)", (subject, sid))
            self.store._event(db, sid + ":login:" + secrets.token_hex(8), "OPERATOR_AUTHENTICATED", {
                "owner": sid, "at": now})
        return sid, token

    def known_operator(self, sid):
        with self.store.connect() as db:
            row = db.execute("SELECT subject FROM operator_identities WHERE session_id=?", (sid,)).fetchone()
            return bool(row and row["subject"] in self.settings.operators)

    def admit_rate(self, identity, budget):
        now = self.store.clock()
        key = hashlib.sha256(identity.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("DELETE FROM rate_buckets WHERE window<?", (now // 60 - 2,))
            row = db.execute("SELECT count FROM rate_buckets WHERE key=? AND window=?", (key, now // 60)).fetchone()
            if row and row["count"] >= budget:
                return False
            db.execute("INSERT INTO rate_buckets VALUES (?,?,1) ON CONFLICT(key,window) DO UPDATE SET count=count+1", (key, now // 60))
            return True
