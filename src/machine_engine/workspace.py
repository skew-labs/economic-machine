"""Durable, local operations view; reported usage never masquerades as billing."""

import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from economic_machine.journal import append_event, verify_journal
from economic_machine.runtime import MachineRuntime
from economic_machine.values import MachineError, canonical, digest, ident, require_keys

from .connections import PROFILES, Connectors, normalize_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_connections (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
 snapshot TEXT, error_code TEXT);
CREATE TABLE IF NOT EXISTS engine_usage (
 event_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, connection_id TEXT NOT NULL,
 model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cost_microusd INTEGER NOT NULL, occurred_at INTEGER NOT NULL, source TEXT NOT NULL);
"""


def now_iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat()


class Workspace:
    def __init__(self, db_path, *, readers=None, clock=time.time, credential_prefix=None, broker_factory=None, live_enabled=None):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        new_database = not path.exists()
        self.runtime, self.clock = MachineRuntime(db_path), clock
        if new_database:
            os.chmod(path, 0o600)
        self.readers = readers or Connectors(clock=clock)
        self.credential_prefix = credential_prefix
        with self.runtime.connect() as db:
            db.executescript(SCHEMA)
        from .scheduler import SyncScheduler
        from .trading import Trading
        self.scheduler = SyncScheduler(self)
        self.trading = Trading(self, **({"broker_factory": broker_factory} if broker_factory else {}), live_enabled=live_enabled)

    def event(self, db, kind, value):
        if not verify_journal(db):
            raise MachineError("JOURNAL_INTEGRITY_FAILED")
        key = kind + ":" + secrets.token_hex(12)
        append_event(db, key, kind, digest(value), digest(value), value)

    def connect(self, raw):
        body = normalize_connection(raw)
        if self.credential_prefix and any(not body["config"][key].startswith(self.credential_prefix)
            for key in PROFILES[body["profile"]]["credentials"]):
            raise MachineError("OWNER_CREDENTIAL_NAMESPACE_REQUIRED")
        at, cid = int(self.clock()), "connection-" + secrets.token_hex(12)
        with self.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM engine_connections WHERE status!='DISCONNECTED'").fetchone()[0] >= 32:
                raise MachineError("CONNECTION_LIMIT_REACHED")
            db.execute("INSERT INTO engine_connections VALUES (?,?,1,'CONFIGURED',?,?,NULL,NULL)",
                       (cid, canonical(body).decode(), at, at))
            self.event(db, "CONNECTION_CONFIGURED", {"id": cid, "profile": body["profile"], "at": at})
        return {"id": cid, "status": "CONFIGURED", "read_only": True, "secret_storage": "USER_ENVIRONMENT_ONLY"}

    def sync(self, cid):
        ident(cid, "connection ID")
        with self.runtime.connect() as db:
            row = db.execute("SELECT * FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if row is None or row["status"] == "DISCONNECTED":
                raise MachineError("ACTIVE_CONNECTION_REQUIRED")
            body, version = json.loads(row["body"]), row["version"]
        # Network I/O occurs outside the transaction. A concurrent disconnect
        # invalidates the commit by advancing its generation.
        try:
            snapshot = self.readers.read(body)
            status, error = "CONNECTED", None
        except Exception as exc:  # noqa: BLE001 - Connector exceptions must never leak credentials.
            snapshot, status = None, "DEGRADED"
            safe_codes = {"CREDENTIAL_REFERENCE_UNAVAILABLE", "CREDENTIAL_ECHO_BLOCKED", "WRONG_CHAIN",
                          "INVALID_EXCHANGE_BALANCE", "INVALID_MODEL_CATALOG", "READ_RESPONSE_TOO_LARGE",
                          "REMOTE_READ_REJECTED", "CHAIN_READ_UNAVAILABLE", "INVALID_EXCHANGE_SNAPSHOT"}
            error = str(exc) if isinstance(exc, MachineError) and str(exc) in safe_codes else "READ_FAILED"
        at = int(self.clock())
        with self.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if current["version"] != version or current["status"] == "DISCONNECTED":
                raise MachineError("CONNECTION_CHANGED_DURING_READ")
            old = json.loads(current["snapshot"]) if current["snapshot"] else None
            if snapshot and old and snapshot["observed_at"] < old["observed_at"]:
                raise MachineError("OUT_OF_ORDER_SNAPSHOT")
            db.execute("UPDATE engine_connections SET status=?,updated=?,snapshot=COALESCE(?,snapshot),error_code=? WHERE id=?",
                (status, at, canonical(snapshot).decode() if snapshot else None, error, cid))
            self.event(db, "CONNECTION_READ", {"id": cid, "status": status, "error_code": error,
                "source_hash": snapshot["source_hash"] if snapshot else None, "at": at})
        return {"id": cid, "status": status, "error_code": error, "new_snapshot": snapshot is not None,
                "read_only": True}

    def disconnect(self, cid):
        ident(cid, "connection ID")
        with self.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise MachineError("CONNECTION_NOT_FOUND")
            if row["status"] != "DISCONNECTED":
                db.execute("UPDATE engine_connections SET status='DISCONNECTED',version=version+1,updated=? WHERE id=?",
                           (int(self.clock()), cid))
                self.event(db, "CONNECTION_DISCONNECTED", {"id": cid})
        return {"id": cid, "status": "DISCONNECTED", "keys_revoked_at_provider": False}

    def record_usage(self, raw):
        require_keys(raw, {"event_id", "connection_id", "model", "input_tokens", "output_tokens",
                           "cost_microusd", "occurred_at"}, "reported API usage")
        for name in ["event_id", "connection_id", "model"]:
            ident(raw[name], name)
        at = int(self.clock())
        for field in ["input_tokens", "output_tokens", "cost_microusd"]:
            if type(raw[field]) is not int or not 0 <= raw[field] <= 10 ** 12:
                raise MachineError("BOUNDED_INTEGER_USAGE_REQUIRED")
        if type(raw["occurred_at"]) is not int or not 1 <= raw["occurred_at"] <= at:
            raise MachineError("USAGE_TIME_REQUIRED")
        key, fingerprint = raw["event_id"], digest(raw)
        with self.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body,status FROM engine_connections WHERE id=?", (raw["connection_id"],)).fetchone()
            if not row or json.loads(row["body"])["kind"] != "ai" or row["status"] == "DISCONNECTED":
                raise MachineError("AI_CONNECTION_REQUIRED")
            previous = db.execute("SELECT input_hash FROM engine_usage WHERE event_id=?", (key,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise MachineError("USAGE_IDEMPOTENCY_CONFLICT")
                return {"idempotent": True, "source": "LOCAL_ADAPTER_REPORT_NOT_PROVIDER_INVOICE"}
            db.execute("INSERT INTO engine_usage VALUES (?,?,?,?,?,?,?,?,?)", (key, fingerprint, raw["connection_id"],
                raw["model"], raw["input_tokens"], raw["output_tokens"], raw["cost_microusd"], raw["occurred_at"],
                "LOCAL_ADAPTER_REPORT_NOT_PROVIDER_INVOICE"))
            self.event(db, "API_USAGE_REPORTED", raw)
        return {"idempotent": False, "source": "LOCAL_ADAPTER_REPORT_NOT_PROVIDER_INVOICE"}

    def overview(self):
        at = int(self.clock())
        runtime = self.runtime.status()
        with self.runtime.connect() as db:
            connections, assets, positions, orders = [], [], [], []
            for row in db.execute("SELECT * FROM engine_connections ORDER BY created,id"):
                body = json.loads(row["body"])
                snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
                stale = snapshot is None or at - snapshot["observed_at"] >= 60 or row["status"] != "CONNECTED"
                item = {"id": row["id"], "name": body["name"], "profile": body["profile"], "kind": body["kind"],
                    "status": row["status"], "network": body["network"], "updated_at": row["updated"],
                    "observed_at": snapshot["observed_at"] if snapshot else None, "stale": stale,
                    "error_code": row["error_code"], "operations": PROFILES[body["profile"]]["operations"],
                    "secret_storage": "USER_ENVIRONMENT_ONLY", "snapshot": snapshot}
                connections.append(item)
                if snapshot and row["status"] != "DISCONNECTED":
                    for key, destination in [("assets", assets), ("positions", positions), ("orders", orders)]:
                        destination.extend(value | {"connection_id": row["id"], "connection": body["name"],
                            "network": body["network"], "observed_at": snapshot["observed_at"], "stale": stale}
                                           for value in snapshot.get(key, []))
            usage = [dict(row) for row in db.execute("SELECT connection_id,model,SUM(input_tokens) AS input_tokens,"
                "SUM(output_tokens) AS output_tokens,SUM(cost_microusd) AS cost_microusd,COUNT(*) AS calls "
                "FROM engine_usage GROUP BY connection_id,model ORDER BY connection_id,model")]
            programs = []
            for row in db.execute("SELECT p.* FROM active_programs a JOIN programs p ON p.program_id=a.program_id AND p.version=a.version"):
                body = json.loads(row["body_json"])
                programs.append({"id": row["program_id"], "name": body["agent_id"], "version": row["version"],
                    "status": row["state"], "network": body["network"], "sandbox": body["sandbox"],
                    "program_hash": row["program_hash"], "source": {k: v for k, v in body.items() if k != "program_hash"},
                    "approval": "OWNER_AUTHORIZATION_AND_EXTERNAL_EVIDENCE_REQUIRED"})
            runs = []
            for row in db.execute("SELECT * FROM receipts ORDER BY rowid DESC LIMIT 100"):
                receipt = json.loads(row["receipt_json"])
                runs.append({"id": row["receipt_hash"], "program_id": row["program_id"],
                    "status": receipt["terminal_state"], "reason": receipt.get("reason"),
                    "receipt": receipt, "verification": self.runtime.verify_receipt(row["receipt_hash"])})
        return {"mode": "SELF_HOSTED", "read_only": False, "as_of": at, "connections": connections,
            "assets": assets, "positions": positions, "orders": orders, "usage": usage,
            "programs": programs, "runs": runs, "payments": [], "runtime": runtime,
            "usage_assurance": "LOCAL_REPORT_NOT_PROVIDER_BILLING",
            "execution_authority": "OWNER_PER_ORDER_VENUE_ONLY" if self.trading.live_enabled is True else "NONE",
            "payment_scope": "EXTERNAL_DATA_AND_COMPUTE_ONLY", "venue_trades_use_venue_api": True,
            "capital_aggregation": "NO_CROSS_ASSET_VALUATION_WITHOUT_PRICE_EVIDENCE",
            "credential_namespace": self.credential_prefix,
            "sync_jobs": self.scheduler.status(), "trading": self.trading.status()}
