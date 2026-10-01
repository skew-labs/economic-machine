"""Transactional budget reservations and durable commerce state.

All ledger units are TEST_CREDIT. This store never claims a blockchain payment.
"""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from economic_machine.journal import append_event, verify_journal
from economic_machine.values import MachineError, canonical, digest

from .domain import (
    ASSET,
    CATALOG,
    identifier,
    money_atoms,
    money_string,
    normalize_policy,
    terms_for,
    terms_hash,
    transition,
    validate_request,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created INTEGER NOT NULL,
 expires INTEGER NOT NULL, available INTEGER NOT NULL CHECK(available >= 0),
 reserved INTEGER NOT NULL CHECK(reserved >= 0), spent INTEGER NOT NULL CHECK(spent >= 0));
CREATE TABLE IF NOT EXISTS policies (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 body TEXT NOT NULL, expires INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
 spent INTEGER NOT NULL DEFAULT 0 CHECK(spent >= 0));
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 policy_id TEXT NOT NULL REFERENCES policies(id), idempotency_key TEXT NOT NULL,
 input_hash TEXT NOT NULL, offer_id TEXT NOT NULL, terms TEXT NOT NULL,
 request TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0), status TEXT NOT NULL,
 created INTEGER NOT NULL, deadline INTEGER NOT NULL, updated INTEGER NOT NULL,
 lease TEXT, artifact TEXT, verification TEXT, receipt TEXT, reason TEXT,
 UNIQUE(session_id,idempotency_key));
CREATE TABLE IF NOT EXISTS payouts (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), provider_id TEXT NOT NULL,
 provider_amount INTEGER NOT NULL CHECK(provider_amount >= 0),
 platform_fee INTEGER NOT NULL CHECK(platform_fee >= 0));
CREATE TABLE IF NOT EXISTS events (
 ordinal INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 input_hash TEXT NOT NULL, output_hash TEXT NOT NULL, event_json TEXT NOT NULL,
 previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_session ON orders(session_id,created);
"""
SEED_BALANCE = 10_000_000


class JournalConnection(sqlite3.Connection):
    # Never survives a transaction. BEGIN IMMEDIATE excludes concurrent writers.
    journal_verified = False


class Store:
    def __init__(self, path, clock):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if "initial_balance" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN initial_balance INTEGER NOT NULL DEFAULT 10000000")
            db.commit()
        finally:
            db.close()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None, factory=JournalConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, key, kind, payload):
        # Verify once before appending within this exclusive transaction. Never cache across transactions.
        if not db.journal_verified:
            if not verify_journal(db):
                raise MachineError("journal integrity failed")
            db.journal_verified = True
        return append_event(db, key, kind, digest(payload), digest(payload), payload)

    def create_session(self, initial_balance=SEED_BALANCE):
        if type(initial_balance) is not int or initial_balance not in {0, SEED_BALANCE}:
            raise MachineError("supported initial ledger balance required")
        token = secrets.token_urlsafe(32)
        sid, now = "buyer-" + secrets.token_hex(12), self.clock()
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", (
                sid, hashlib.sha256(token.encode()).hexdigest(), now, now + 86400, initial_balance, 0, 0, initial_balance))
            self._event(db, sid + ":created", "SANDBOX_OPENED", {
                "session_id": sid, "balance_atoms": initial_balance, "asset": ASSET, "at": now})
        return sid, token

    def authenticate(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 120:
            raise MachineError("session required")
        with self.connect() as db:
            row = db.execute("SELECT id,expires FROM sessions WHERE token_hash=?", (
                hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None or row["expires"] <= self.clock():
                raise MachineError("session expired or invalid")
            return row["id"]

    def create_policy(self, sid, raw):
        body = normalize_policy(raw)
        now, pid = self.clock(), "policy-" + secrets.token_hex(12)
        with self.connect() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None or session["expires"] <= now:
                raise MachineError("session expired or invalid")
            if body["budget_atoms"] > session["available"]:
                raise MachineError("policy budget exceeds available sandbox balance")
            db.execute("INSERT INTO policies VALUES (?,?,?,?,0,0)", (
                pid, sid, canonical(body).decode(), now + body["ttl_seconds"]))
            self._event(db, pid, "POLICY_COMPILED", {
                "policy_id": pid, "session_id": sid, "policy": body, "at": now})
        return {"id": pid, **body, "policy_hash": digest(body), "expires": now + body["ttl_seconds"]}

    def reserve(self, sid, policy_id, offer_id, request, idempotency_key):
        identifier(idempotency_key, "idempotency key")
        identifier(policy_id, "buyer policy id")
        identifier(offer_id, "service id")
        if offer_id not in CATALOG:
            raise MachineError("unknown service")
        validate_request(offer_id, request)
        input_hash = digest({"policy_id": policy_id, "offer_id": offer_id, "request": request,
                             "terms_hash": terms_hash(offer_id)})
        amount, now = money_atoms(CATALOG[offer_id]["price"]), self.clock()
        oid = "order-" + secrets.token_hex(12)
        with self.connect() as db:
            prior = db.execute("SELECT * FROM orders WHERE session_id=? AND idempotency_key=?",
                               (sid, idempotency_key)).fetchone()
            if prior:
                if prior["input_hash"] != input_hash:
                    raise MachineError("idempotency key reused with different order")
                return self._public_order(prior)
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            policy = db.execute("SELECT * FROM policies WHERE id=? AND session_id=?", (
                policy_id, sid)).fetchone()
            if not session or session["expires"] <= now or not policy or policy["expires"] <= now:
                raise MachineError("active buyer policy required")
            body = json.loads(policy["body"])
            if offer_id not in body["allowed_offers"]:
                raise MachineError("service outside buyer policy")
            if amount > body["max_order_atoms"]:
                raise MachineError("price exceeds per-order limit")
            if policy["spent"] + policy["reserved"] + amount > body["budget_atoms"]:
                raise MachineError("policy budget exhausted")
            if amount > session["available"]:
                raise MachineError("insufficient sandbox balance")
            db.execute("UPDATE sessions SET available=available-?,reserved=reserved+? WHERE id=?",
                       (amount, amount, sid))
            db.execute("UPDATE policies SET reserved=reserved+? WHERE id=?", (amount, policy_id))
            deadline = min(now + 30, policy["expires"])
            db.execute("""INSERT INTO orders
                (id,session_id,policy_id,idempotency_key,input_hash,offer_id,terms,request,
                 amount,status,created,deadline,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                oid, sid, policy_id, idempotency_key, input_hash, offer_id,
                canonical(terms_for(offer_id)).decode(), canonical(request).decode(), amount,
                "RESERVED", now, deadline, now))
            self._event(db, oid + ":RESERVED", "RESERVED", {
                "order_id": oid, "buyer": sid, "policy_id": policy_id,
                "input_hash": input_hash, "terms_hash": terms_hash(offer_id),
                "amount_atoms": amount, "deadline": deadline, "at": now})
            self._assert_account(db, sid)
            return self._public_order(db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())

    def _owned(self, db, sid, oid):
        row = db.execute("SELECT * FROM orders WHERE id=? AND session_id=?", (oid, sid)).fetchone()
        if row is None:
            raise MachineError("order not found")
        return row

    def claim(self, sid, oid):
        with self.connect() as db:
            row = self._owned(db, sid, oid)
            if row["status"] != "RESERVED":
                return None
            if row["deadline"] <= self.clock():
                self._refund(db, row, "DELIVERY_DEADLINE_EXPIRED")
                return None
            lease = secrets.token_hex(16)
            transition(row["status"], "FULFILLING")
            db.execute("UPDATE orders SET status='FULFILLING',lease=?,updated=? WHERE id=?",
                       (lease, self.clock(), oid))
            self._event(db, oid + ":FULFILLING", "FULFILLING", {
                "order_id": oid, "at": self.clock()})
            return {**dict(row), "lease": lease, "request": json.loads(row["request"])}

    def complete(self, sid, oid, lease, artifact, verification):
        with self.connect() as db:
            row = self._owned(db, sid, oid)
            if row["status"] != "FULFILLING" or row["lease"] != lease:
                raise MachineError("delivery lease is not active")
            if row["deadline"] <= self.clock():
                self._refund(db, row, "DELIVERY_DEADLINE_EXPIRED")
                return self._public_order(self._owned(db, sid, oid))
            encoded = canonical(artifact)
            if len(encoded) > 200_000:
                raise MachineError("delivery exceeds maximum size")
            artifact_hash = digest(artifact)
            transition(row["status"], "DELIVERED")
            db.execute("UPDATE orders SET status='DELIVERED',artifact=?,verification=? WHERE id=?",
                       (encoded.decode(), canonical(verification).decode(), oid))
            self._event(db, oid + ":DELIVERED", "DELIVERED", {
                "order_id": oid, "artifact_hash": artifact_hash, "at": self.clock()})
            row = self._owned(db, sid, oid)
            if (verification.get("accepted") is not True or verification.get("artifact_hash") != artifact_hash
                    or verification.get("request_hash") != digest(json.loads(row["request"]))):
                self._refund(db, row, "DELIVERY_VERIFICATION_FAILED")
                return self._public_order(self._owned(db, sid, oid))
            transition(row["status"], "VERIFIED")
            db.execute("UPDATE orders SET status='VERIFIED' WHERE id=?", (oid,))
            self._event(db, oid + ":VERIFIED", "VERIFIED", {
                "order_id": oid, "verification": verification, "at": self.clock()})
            fee = row["amount"] * 100 // 10_000
            terms = json.loads(row["terms"])
            receipt = {"schema_version": "commerce-receipt-1", "order_id": oid,
                "buyer": sid, "provider_id": terms["provider_id"],
                "policy_id": row["policy_id"], "terms_hash": digest(terms),
                "input_hash": row["input_hash"], "artifact_hash": artifact_hash,
                "verification_hash": digest(verification), "asset": ASSET,
                "amount": money_string(row["amount"]),
                "provider_amount": money_string(row["amount"] - fee), "platform_fee": money_string(fee),
                "settlement": "SANDBOX_LEDGER", "chain_id": None, "tx_hash": None,
                "settled_at": self.clock()}
            receipt["receipt_hash"] = digest(receipt)
            transition("VERIFIED", "SETTLED")
            db.execute("UPDATE orders SET status='SETTLED',receipt=?,updated=?,lease=NULL WHERE id=?", (
                canonical(receipt).decode(), self.clock(), oid))
            db.execute("UPDATE sessions SET reserved=reserved-?,spent=spent+? WHERE id=?",
                       (row["amount"], row["amount"], sid))
            db.execute("UPDATE policies SET reserved=reserved-?,spent=spent+? WHERE id=?",
                       (row["amount"], row["amount"], row["policy_id"]))
            db.execute("INSERT INTO payouts VALUES (?,?,?,?)", (
                oid, terms["provider_id"], row["amount"] - fee, fee))
            self._event(db, oid + ":SETTLED", "SETTLED", receipt)
            self._assert_account(db, sid)
            return self._public_order(self._owned(db, sid, oid))

    def fail(self, sid, oid, lease, reason):
        with self.connect() as db:
            row = self._owned(db, sid, oid)
            if row["status"] != "FULFILLING" or row["lease"] != lease:
                return self._public_order(row)
            self._refund(db, row, reason)
            return self._public_order(self._owned(db, sid, oid))

    def cancel(self, sid, oid):
        with self.connect() as db:
            row = self._owned(db, sid, oid)
            if row["status"] == "REFUNDED":
                return self._public_order(row)
            if row["status"] != "RESERVED" and row["deadline"] > self.clock():
                raise MachineError("order is active; cancel after delivery deadline")
            self._refund(db, row, "BUYER_CANCELLED")
            return self._public_order(self._owned(db, sid, oid))

    def _refund(self, db, row, reason):
        transition(row["status"], "REFUNDED")
        db.execute("UPDATE sessions SET reserved=reserved-?,available=available+? WHERE id=?",
                   (row["amount"], row["amount"], row["session_id"]))
        db.execute("UPDATE policies SET reserved=reserved-? WHERE id=?", (row["amount"], row["policy_id"]))
        db.execute("UPDATE orders SET status='REFUNDED',reason=?,updated=?,lease=NULL WHERE id=?", (
            reason, self.clock(), row["id"]))
        self._event(db, row["id"] + ":REFUNDED", "REFUNDED", {
            "order_id": row["id"], "reason": reason, "amount_atoms": row["amount"], "at": self.clock()})
        self._assert_account(db, row["session_id"])

    def recover_expired(self, sid):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM orders WHERE session_id=? AND deadline<=? "
                "AND status NOT IN ('SETTLED','REFUNDED')", (sid, self.clock())).fetchall()
            for row in rows:
                self._refund(db, row, "DELIVERY_DEADLINE_EXPIRED")
            return len(rows)

    def _assert_account(self, db, sid):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row["available"] + row["reserved"] + row["spent"] != row["initial_balance"]:
            raise MachineError("buyer capital conservation failed")
        active = db.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE session_id=? "
            "AND status NOT IN ('SETTLED','REFUNDED')", (sid,)).fetchone()[0]
        paid = db.execute("SELECT COALESCE(SUM(p.provider_amount+p.platform_fee),0) "
            "FROM payouts p JOIN orders o ON o.id=p.order_id WHERE o.session_id=?", (sid,)).fetchone()[0]
        if active != row["reserved"] or paid != row["spent"]:
            raise MachineError("orders and ledger do not reconcile")

    def _public_order(self, row):
        return {"id": row["id"], "offer_id": row["offer_id"], "policy_id": row["policy_id"],
            "status": row["status"], "amount": money_string(row["amount"]), "asset": ASSET,
            "created": row["created"], "deadline": row["deadline"], "reason": row["reason"],
            "terms_hash": digest(json.loads(row["terms"])), "input_hash": row["input_hash"],
            "artifact": json.loads(row["artifact"]) if row["artifact"] else None,
            "verification": json.loads(row["verification"]) if row["verification"] else None,
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None}

    def order(self, sid, oid):
        with self.connect() as db:
            return self._public_order(self._owned(db, sid, oid))

    def snapshot(self, sid):
        self.recover_expired(sid)
        with self.connect() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not session:
                raise MachineError("session required")
            self._assert_account(db, sid)
            policies = [{"id": row["id"], **json.loads(row["body"]), "expires": row["expires"],
                "reserved_atoms": row["reserved"], "spent_atoms": row["spent"]}
                for row in db.execute("SELECT * FROM policies WHERE session_id=? ORDER BY rowid DESC", (sid,))]
            orders = [self._public_order(row) for row in db.execute(
                "SELECT * FROM orders WHERE session_id=? ORDER BY created DESC,rowid DESC LIMIT 100", (sid,))]
            return {"buyer_id": sid, "balance": money_string(session["available"]),
                "reserved": money_string(session["reserved"]), "spent": money_string(session["spent"]),
                "asset": ASSET, "policies": policies, "orders": orders,
                "journal_integrity": verify_journal(db), "execution_authority": "SANDBOX_ONLY"}

    def order_events(self, sid, oid):
        with self.connect() as db:
            self._owned(db, sid, oid)
            return [{"kind": row["kind"], "event_hash": row["event_hash"],
                "previous_hash": row["previous_hash"], "event": json.loads(row["event_json"])}
                for row in db.execute("SELECT * FROM events WHERE event_id LIKE ? ORDER BY ordinal", (oid + ":%",))]
