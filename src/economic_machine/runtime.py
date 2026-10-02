"""Durable, single-node Economic Runtime with idempotent event/replay semantics."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .compiler import compile_program
from .execution_evidence import ExecutionEvidenceVerifier
from .inference import assess_inference, normalize_inference_draft, normalize_inference_scope
from .journal import append_event, verify_journal
from .kernel import EconomicKernel
from .state import apply_delta, normalize_state, state_root
from .values import MachineError, canonical, digest, ident, require_keys, utc

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS programs (
 program_id TEXT NOT NULL, version INTEGER NOT NULL, program_hash TEXT NOT NULL,
 body_json TEXT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(program_id,version)
);
CREATE TABLE IF NOT EXISTS active_programs (
 program_id TEXT PRIMARY KEY, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dependencies (
 program_id TEXT NOT NULL, version INTEGER NOT NULL, state_key TEXT NOT NULL,
 PRIMARY KEY(program_id,version,state_key)
);
CREATE INDEX IF NOT EXISTS dependency_key ON dependencies(state_key);
CREATE TABLE IF NOT EXISTS states (
 sequence INTEGER PRIMARY KEY, state_root TEXT UNIQUE NOT NULL,
 state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 ordinal INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 input_hash TEXT NOT NULL, output_hash TEXT NOT NULL, event_json TEXT NOT NULL,
 previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
 receipt_hash TEXT PRIMARY KEY, program_id TEXT NOT NULL, version INTEGER NOT NULL,
 state_sequence INTEGER NOT NULL, evaluated_at TEXT NOT NULL,
 terminal_state TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipt_program ON receipts(program_id,version,state_sequence);
CREATE TABLE IF NOT EXISTS reservations (
 reservation_id TEXT PRIMARY KEY, program_id TEXT NOT NULL, version INTEGER NOT NULL,
 owner_id TEXT NOT NULL, agent_id TEXT NOT NULL, network TEXT NOT NULL, asset TEXT NOT NULL,
 amount TEXT NOT NULL, cost TEXT NOT NULL, expires_at TEXT NOT NULL,
 status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS active_reservation ON reservations(status,network,asset,expires_at);
CREATE TABLE IF NOT EXISTS inference_assessments (
 assessment_hash TEXT PRIMARY KEY, candidate_hash TEXT NOT NULL, scope_hash TEXT NOT NULL,
 state_sequence INTEGER NOT NULL, assessed_at TEXT NOT NULL,
 candidate_json TEXT NOT NULL, scope_json TEXT NOT NULL, assessment_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inference_state ON inference_assessments(state_sequence);
CREATE TABLE IF NOT EXISTS escalations (
 program_id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
 reason_code TEXT NOT NULL, receipt_hash TEXT NOT NULL, state_root TEXT NOT NULL,
 opened_at TEXT NOT NULL
);
"""


class MachineRuntime:
    """The first implementation is SQLite-backed and replayable, not a fast NIC tile."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        self.kernel = EconomicKernel()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _verify_execution_locks(db: sqlite3.Connection) -> None:
        """Replay the execution projection; settlement is the only release."""
        rows = {row["reservation_id"]: row["status"] for row in db.execute(
            "SELECT reservation_id,status FROM reservations WHERE status IN "
            "('EXECUTION_LOCKED','RECONCILED')")}
        execution_events = db.execute("SELECT count(*) AS n FROM events "
                                      "WHERE kind LIKE 'EXECUTION_%' "
                                      "OR event_id LIKE 'execution-%'").fetchone()["n"]
        if (rows or execution_events) and not verify_journal(db):
            raise MachineError("journal integrity failed; execution lock check denied")
        phases = {}
        transitions = {"EXECUTION_LOCK": (None, "LOCKED"),
                       "EXECUTION_AUTHORIZE": ("LOCKED", "AUTHORIZED"),
                       "EXECUTION_SUBMIT": ("AUTHORIZED", "SUBMITTED"),
                       "EXECUTION_FINALIZE": ("SUBMITTED", "FINALIZED"),
                       "EXECUTION_RECONCILE": ("FINALIZED", "RECONCILED"),
                       "EXECUTION_EXCEPTION": (("SUBMITTED", "FINALIZED", "RECONCILED"),
                                               "DISPUTED")}
        for row in db.execute("SELECT event_id,kind,event_json FROM events "
                              "WHERE kind LIKE 'EXECUTION_%' ORDER BY ordinal"):
            event = json.loads(row["event_json"])
            receipt_hash = event.get("receipt_hash")
            phase = row["kind"]
            if phase not in transitions or not isinstance(receipt_hash, str):
                raise MachineError("execution journal has invalid transition")
            prefix = "execution-lock:" if phase == "EXECUTION_LOCK" else (
                "execution-" + phase.removeprefix("EXECUTION_").lower() + ":")
            before, after = transitions[phase]
            if (row["event_id"] != prefix + receipt_hash
                    or (phases.get(receipt_hash) not in before if isinstance(before, tuple)
                        else phases.get(receipt_hash) != before)):
                raise MachineError("execution journal has invalid transition")
            phases[receipt_hash] = after
        expected = {key: "RECONCILED" if phase == "RECONCILED" else "EXECUTION_LOCKED"
                    for key, phase in phases.items()}
        if rows != expected:
            raise MachineError("execution lock projection differs from journal")

    @staticmethod
    def _execution_history(db: sqlite3.Connection, receipt_hash: str) -> dict:
        result = {}
        for row in db.execute("SELECT kind,event_json FROM events WHERE event_id IN "
                              "(?,?,?,?,?,?) ORDER BY ordinal",
                              tuple(prefix + receipt_hash for prefix in
                                    ("execution-lock:", "execution-authorize:",
                                     "execution-submit:", "execution-finalize:",
                                     "execution-reconcile:", "execution-exception:"))):
            result[row["kind"]] = json.loads(row["event_json"])
        return result

    @staticmethod
    def _ensure_no_execution_dispute(db: sqlite3.Connection) -> None:
        if db.execute("SELECT 1 FROM events WHERE kind='EXECUTION_EXCEPTION' "
                      "LIMIT 1").fetchone() is not None:
            raise MachineError("runtime halted by unresolved execution dispute")

    def install_state(self, value: dict) -> dict:
        state = normalize_state(value)
        if state["sequence"] != 0:
            raise MachineError("initial state sequence must be zero")
        root = state_root(state)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            foreign = db.execute("SELECT 1 FROM programs WHERE state IN ('ACTIVE','PAUSED') "
                                 "AND json_extract(body_json,'$.owner_id')!=? LIMIT 1",
                                 (state["owner_id"],)).fetchone()
            if foreign:
                raise MachineError("initial state owner differs from registered program")
            old = db.execute("SELECT state_root FROM states WHERE sequence=0").fetchone()
            if old:
                if old["state_root"] != root:
                    raise MachineError("initial state is immutable")
                return {"sequence": 0, "state_root": root, "idempotent": True}
            db.execute("INSERT INTO states VALUES (?,?,?)", (0, root, canonical(state).decode()))
            event = {"kind": "INITIAL_STATE", "sequence": 0, "state_root": root}
            append_event(db, digest(event), "INITIAL_STATE", root, root, event)
        return {"sequence": 0, "state_root": root, "idempotent": False}

    def latest_state(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
        if row is None:
            raise MachineError("no state installed")
        return json.loads(row["state_json"])

    def register_program(self, source: dict) -> dict:
        program = compile_program(source)
        keys = {"balance:" + program["sandbox"]["asset"],
                "exposure:" + program["agent_id"] + ":" + program["sandbox"]["asset"],
                "daily_loss:" + program["agent_id"] + ":" + program["sandbox"]["asset"]}
        for ins in program["instructions"]:
            if ins["op"] == "OBSERVE":
                keys.add(ins["path"])
            elif ins["op"] == "PRICE":
                keys.update(ins["paths"])
            elif ins["op"] == "QUOTE":
                keys.add("quote:*")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            current_state = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            if current_state and json.loads(current_state["state_json"])["owner_id"] != program["owner_id"]:
                raise MachineError("program owner differs from this runtime's state owner")
            active = db.execute("SELECT version FROM active_programs WHERE program_id=?",
                                (program["program_id"],)).fetchone()
            if active:
                old = db.execute("SELECT program_hash,state FROM programs WHERE program_id=? AND version=?",
                                 (program["program_id"], active["version"])).fetchone()
                if old is None or old["state"] not in {"ACTIVE", "PAUSED"}:
                    raise MachineError("active program pointer is inconsistent")
                if old["program_hash"] == program["program_hash"]:
                    return {"program_id": program["program_id"], "version": active["version"],
                            "program_hash": program["program_hash"], "state": old["state"],
                            "idempotent": True,
                            "watched_state_keys": sorted(keys)}
                db.execute("UPDATE programs SET state='SUPERSEDED' WHERE program_id=? AND version=?",
                           (program["program_id"], active["version"]))
                db.execute("UPDATE reservations SET status='SUPERSEDED' WHERE program_id=? "
                           "AND status='ACTIVE'", (program["program_id"],))
            db.execute("UPDATE escalations SET status='CLOSED' WHERE program_id=? "
                       "AND status='OPEN'", (program["program_id"],))
            version = active["version"] + 1 if active else 1
            new_state = "PAUSED" if active and old["state"] == "PAUSED" else "ACTIVE"
            db.execute("INSERT INTO programs VALUES (?,?,?,?,?)",
                       (program["program_id"], version, program["program_hash"],
                        canonical(program).decode(), new_state))
            db.execute("INSERT INTO active_programs VALUES (?,?) ON CONFLICT(program_id) "
                       "DO UPDATE SET version=excluded.version", (program["program_id"], version))
            db.executemany("INSERT INTO dependencies VALUES (?,?,?)", [
                (program["program_id"], version, key) for key in sorted(keys)])
            event = {"kind": "REGISTER_PROGRAM", "program_id": program["program_id"],
                     "version": version, "program_hash": program["program_hash"],
                     "state": new_state}
            append_event(db, digest(event), "REGISTER_PROGRAM",
                         program["program_hash"], program["program_hash"], event)
        return {"program_id": program["program_id"], "version": version,
                "program_hash": program["program_hash"], "state": new_state,
                "idempotent": False,
                "watched_state_keys": sorted(keys)}

    def _set_program_state(self, program_id: str, target: str, reason: str) -> dict:
        """Local operator control, serialized with ingest/evaluate by SQLite."""
        ident(program_id, "program id")
        ident(reason, "control reason")
        if target not in {"ACTIVE", "PAUSED"}:
            raise MachineError("invalid program control state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            if target == "ACTIVE" and not verify_journal(db):
                raise MachineError("journal integrity failed; resume denied")
            if target == "ACTIVE":
                self._ensure_no_execution_dispute(db)
            row = db.execute("SELECT p.* FROM programs p JOIN active_programs a "
                             "ON a.program_id=p.program_id AND a.version=p.version "
                             "WHERE p.program_id=?", (program_id,)).fetchone()
            if row is None:
                raise MachineError("program not registered")
            if row["state"] == target:
                return {"program_id": program_id, "version": row["version"],
                        "state": target, "revoked_reservations": [], "idempotent": True}
            if row["state"] not in {"ACTIVE", "PAUSED"}:
                raise MachineError("invalid active program state")
            revoked = []
            if target == "PAUSED":
                revoked = [item["reservation_id"] for item in db.execute(
                    "SELECT reservation_id FROM reservations WHERE program_id=? "
                    "AND status='ACTIVE' ORDER BY reservation_id", (program_id,))]
                db.execute("UPDATE reservations SET status='REVOKED' WHERE program_id=? "
                           "AND status='ACTIVE'", (program_id,))
                db.execute("UPDATE escalations SET status='CLOSED' WHERE program_id=? "
                           "AND status='OPEN'", (program_id,))
            db.execute("UPDATE programs SET state=? WHERE program_id=? AND version=?",
                       (target, program_id, row["version"]))
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 AS next FROM events").fetchone()["next"]
            event = {"kind": "PROGRAM_CONTROL", "program_id": program_id,
                     "version": row["version"], "from": row["state"], "to": target,
                     "reason": reason, "revoked_reservations": revoked}
            append_event(db, f"program-control:{ordinal}", "PROGRAM_CONTROL",
                         digest({"program_id": program_id, "state": row["state"]}),
                         digest(event), event)
            return {"program_id": program_id, "version": row["version"],
                    "state": target, "revoked_reservations": revoked, "idempotent": False}

    def pause_program(self, program_id: str, *, reason: str) -> dict:
        return self._set_program_state(program_id, "PAUSED", reason)

    def resume_program(self, program_id: str, *, reason: str) -> dict:
        return self._set_program_state(program_id, "ACTIVE", reason)

    def intent_status(self, receipt_hash: str, *, at: str) -> dict:
        """Read-only local snapshot; callers must recheck before any signing."""
        at = utc(at)
        with self.connect() as db:
            self._verify_execution_locks(db)
            receipt = db.execute("SELECT program_id,version FROM receipts WHERE receipt_hash=?",
                                 (receipt_hash,)).fetchone()
            if receipt is None:
                raise MachineError("receipt unknown")
            reservation = db.execute("SELECT status,expires_at FROM reservations "
                                     "WHERE reservation_id=?", (receipt_hash,)).fetchone()
            history = self._execution_history(db, receipt_hash)
            active = db.execute("SELECT a.version,p.state FROM active_programs a "
                                "JOIN programs p ON p.program_id=a.program_id "
                                "AND p.version=a.version WHERE a.program_id=?",
                                (receipt["program_id"],)).fetchone()
        status = reservation["status"] if reservation else "NOT_RESERVED"
        if reservation and status == "ACTIVE" and at >= reservation["expires_at"]:
            status = "EXPIRED"
        verified = self.verify_receipt(receipt_hash)
        locally_pending = (verified and status == "ACTIVE" and active is not None
                           and active["version"] == receipt["version"]
                           and active["state"] == "ACTIVE")
        return {"receipt_hash": receipt_hash, "program_id": receipt["program_id"],
                "version": receipt["version"], "at": at,
                "reservation_status": status, "program_state": active["state"] if active else None,
                "historical_receipt_verified": verified,
                "locally_pending": locally_pending,
                "capital_locked": status == "EXECUTION_LOCKED",
                "execution_stage": next(reversed(history)).removeprefix("EXECUTION_")
                if history else "NOT_LOCKED"}

    def begin_execution(self, receipt_hash: str, *, at: str) -> dict:
        """Freeze an intent's capital before any external signer sees it.

        This produces no transaction or authorization. A lock is deliberately
        not released by expiry, program replacement or pause; the later
        settlement lifecycle must prove a safe release.
        """
        at = utc(at)
        if not self.verify_receipt(receipt_hash):
            raise MachineError("decision receipt does not replay")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            self._ensure_no_execution_dispute(db)
            row = db.execute("SELECT r.status,r.expires_at,r.program_id,r.version,"
                             "p.receipt_json,p.state_sequence FROM reservations r "
                             "JOIN receipts p ON p.receipt_hash=r.reservation_id "
                             "WHERE r.reservation_id=?", (receipt_hash,)).fetchone()
            if row is None:
                raise MachineError("receipt has no capital reservation")
            if row["status"] == "EXECUTION_LOCKED":
                event = db.execute("SELECT event_hash,event_json FROM events WHERE event_id=?",
                                   ("execution-lock:" + receipt_hash,)).fetchone()
                if event is None:
                    raise MachineError("execution lock event missing")
                return {"receipt_hash": receipt_hash, "status": "EXECUTION_LOCKED",
                        "locked_at": json.loads(event["event_json"])["at"],
                        "event_hash": event["event_hash"], "capital_locked": True,
                        "execution_authority": "NONE", "idempotent": True}
            if row["status"] != "ACTIVE":
                raise MachineError("retired reservation cannot be execution locked")
            receipt = json.loads(row["receipt_json"])
            if (receipt.get("receipt_hash") != receipt_hash
                    or digest({key: value for key, value in receipt.items()
                               if key != "receipt_hash"}) != receipt_hash
                    or receipt.get("terminal_state") != "AWAITING_AUTHORIZATION"
                    or receipt.get("chain_status") != "NOT_SUBMITTED"
                    or not isinstance(receipt.get("intent"), dict)):
                raise MachineError("receipt has no eligible execution intent")
            intent = receipt["intent"]
            if (at < receipt["at"] or at >= row["expires_at"]
                    or at >= intent["expires_at"]):
                raise MachineError("execution lock is outside intent lifetime")
            active = db.execute("SELECT a.version,p.state FROM active_programs a "
                                "JOIN programs p ON p.program_id=a.program_id "
                                "AND p.version=a.version WHERE a.program_id=?",
                                (row["program_id"],)).fetchone()
            latest = db.execute("SELECT state_root,sequence FROM states "
                                "ORDER BY sequence DESC LIMIT 1").fetchone()
            if (active is None or active["version"] != row["version"]
                    or active["state"] != "ACTIVE" or latest is None
                    or latest["state_root"] != receipt["state_root"]
                    or latest["sequence"] != row["state_sequence"]):
                raise MachineError("intent no longer matches active program and state")
            db.execute("UPDATE reservations SET status='EXECUTION_LOCKED' "
                       "WHERE reservation_id=? AND status='ACTIVE'", (receipt_hash,))
            event = {"kind": "EXECUTION_LOCK", "receipt_hash": receipt_hash,
                     "intent_hash": intent["intent_hash"], "state_root": receipt["state_root"],
                     "program_id": row["program_id"], "version": row["version"], "at": at,
                     "capital_lock": "HELD", "signature_status": "NOT_SIGNED",
                     "chain_status": "NOT_SUBMITTED"}
            event_hash = append_event(db, "execution-lock:" + receipt_hash,
                                      "EXECUTION_LOCK", receipt_hash, digest(event), event)
            return {"receipt_hash": receipt_hash, "status": "EXECUTION_LOCKED",
                    "locked_at": at, "event_hash": event_hash, "capital_locked": True,
                    "execution_authority": "NONE", "idempotent": False}

    def record_execution_evidence(self, receipt_hash: str, *, stage: str,
                                  evidence: dict,
                                  verifier: ExecutionEvidenceVerifier) -> dict:
        """Advance a locked intent using independently verified external evidence.

        This method never signs, submits, or queries a chain. The injected
        verifier owns those trust boundaries. Capital is released only after
        finality and an independently authenticated matching account snapshot.
        """
        stages = {"AUTHORIZE": ("EXECUTION_LOCK", "EXECUTION_AUTHORIZE"),
                  "SUBMIT": ("EXECUTION_AUTHORIZE", "EXECUTION_SUBMIT"),
                  "FINALIZE": ("EXECUTION_SUBMIT", "EXECUTION_FINALIZE"),
                  "RECONCILE": ("EXECUTION_FINALIZE", "EXECUTION_RECONCILE")}
        if stage not in stages:
            raise MachineError("unknown execution evidence stage")
        if not isinstance(verifier, ExecutionEvidenceVerifier):
            raise MachineError("external execution evidence verifier required")
        if not self.verify_receipt(receipt_hash):
            raise MachineError("decision receipt does not replay")
        previous, target = stages[stage]
        event_id = "execution-" + stage.lower() + ":" + receipt_hash
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            record = db.execute("SELECT r.status,p.receipt_json FROM reservations r "
                                "JOIN receipts p ON p.receipt_hash=r.reservation_id "
                                "WHERE r.reservation_id=?", (receipt_hash,)).fetchone()
            if record is None:
                raise MachineError("execution reservation missing")
            history = self._execution_history(db, receipt_hash)
            prior = history.get(target)
            evidence_hash = digest(evidence)
            if prior is not None:
                if prior["evidence_hash"] != evidence_hash or prior["evidence"] != evidence:
                    raise MachineError("conflicting evidence for recorded execution stage")
                return {"receipt_hash": receipt_hash, "stage": stage,
                        "evidence_hash": evidence_hash,
                        "capital_locked": record["status"] == "EXECUTION_LOCKED",
                        "idempotent": True}
            if previous not in history or len(history) != list(stages).index(stage) + 1:
                raise MachineError("execution evidence is out of order")
            if record["status"] != "EXECUTION_LOCKED":
                raise MachineError("execution capital lock is absent")
            receipt = json.loads(record["receipt_json"])
            intent = receipt["intent"]
            if stage == "AUTHORIZE":
                require_keys(evidence, {"owner_id", "intent_hash", "signed_at",
                                        "signature_ref"}, "authorization evidence")
                signed_at = utc(evidence["signed_at"])
                ident(evidence["signature_ref"], "signature reference")
                if (evidence["owner_id"] != intent["owner_id"]
                        or evidence["intent_hash"] != intent["intent_hash"]
                        or signed_at < receipt["at"] or signed_at >= intent["expires_at"]):
                    raise MachineError("authorization does not bind a live intent")
                valid = verifier.verify_authorization(intent, evidence)
            elif stage == "SUBMIT":
                require_keys(evidence, {"network", "intent_hash", "txid", "submitted_at",
                                        "signed_payload_hash", "submission_ref"},
                             "submission evidence")
                submitted_at = utc(evidence["submitted_at"])
                ident(evidence["txid"], "transaction id")
                ident(evidence["submission_ref"], "submission reference")
                if (not isinstance(evidence["signed_payload_hash"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", evidence["signed_payload_hash"]) is None):
                    raise MachineError("signed payload hash required")
                authorization = history["EXECUTION_AUTHORIZE"]["evidence"]
                if (evidence["network"] != intent["network"]
                        or evidence["intent_hash"] != intent["intent_hash"]
                        or submitted_at < utc(authorization["signed_at"])
                        or submitted_at >= intent["expires_at"]):
                    raise MachineError("submission does not bind a live authorization")
                valid = verifier.verify_submission(intent, authorization, evidence)
            elif stage == "FINALIZE":
                require_keys(evidence, {"network", "txid", "finalized_at", "block_height",
                                        "raw_receipt_hash", "execution_status"},
                             "finality evidence")
                finalized_at = utc(evidence["finalized_at"])
                submission = history["EXECUTION_SUBMIT"]["evidence"]
                if (evidence["network"] != intent["network"]
                        or evidence["txid"] != submission["txid"]
                        or finalized_at < utc(submission["submitted_at"])
                        or type(evidence["block_height"]) is not int
                        or evidence["block_height"] < 0
                        or not isinstance(evidence["raw_receipt_hash"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", evidence["raw_receipt_hash"]) is None
                        or evidence["execution_status"] != "SUCCESS"):
                    raise MachineError("finality does not prove a successful submitted action")
                valid = verifier.verify_finality(submission, evidence)
            else:
                require_keys(evidence, {"owner_id", "network", "txid", "state_root",
                                        "sequence", "observed_at", "snapshot_ref"},
                             "account reconciliation evidence")
                observed_at = utc(evidence["observed_at"])
                ident(evidence["snapshot_ref"], "snapshot reference")
                state_row = db.execute("SELECT state_json,state_root FROM states "
                                       "ORDER BY sequence DESC LIMIT 1").fetchone()
                state = json.loads(state_row["state_json"])
                finality = history["EXECUTION_FINALIZE"]["evidence"]
                submission = history["EXECUTION_SUBMIT"]["evidence"]
                if (evidence["owner_id"] != intent["owner_id"]
                        or evidence["network"] != intent["network"]
                        or evidence["txid"] != submission["txid"]
                        or evidence["state_root"] != state_row["state_root"]
                        or evidence["sequence"] != state["sequence"]
                        or state_root(state) != state_row["state_root"]
                        or state["owner_id"] != intent["owner_id"]
                        or state["network"] != intent["network"]
                        or observed_at < utc(finality["finalized_at"])
                        or state["as_of"] < utc(finality["finalized_at"])
                        or observed_at < state["as_of"]):
                    raise MachineError("account snapshot does not bind finalized state")
                asset = receipt["action"]["asset"]
                agent = intent["agent_id"]
                actual = {"balance_after": state["balances"].get(asset),
                          "exposure_after": state["exposures"].get(agent, {}).get(asset),
                          "daily_loss_after": state["daily_losses"].get(agent, {}).get(asset)}
                expected = {key: intent["expected"][key] for key in actual}
                if actual != expected:
                    raise MachineError("account postcondition differs from intent")
                valid = verifier.verify_account_snapshot(finality, state, evidence)
            if not valid:
                raise MachineError("external execution evidence not verified")
            event = {"kind": target, "receipt_hash": receipt_hash,
                     "intent_hash": intent["intent_hash"], "evidence_hash": evidence_hash,
                     "evidence": evidence}
            event_hash = append_event(db, event_id, target, digest(history[previous]),
                                      evidence_hash, event)
            if stage == "RECONCILE":
                db.execute("UPDATE reservations SET status='RECONCILED' "
                           "WHERE reservation_id=? AND status='EXECUTION_LOCKED'",
                           (receipt_hash,))
            return {"receipt_hash": receipt_hash, "stage": stage,
                    "evidence_hash": evidence_hash, "event_hash": event_hash,
                    "capital_locked": stage != "RECONCILE", "idempotent": False}

    def record_execution_exception(self, receipt_hash: str, *, evidence: dict,
                                   verifier: ExecutionEvidenceVerifier) -> dict:
        """Record a verified uncertain outcome and halt all new decisions.

        This never interprets failure as no effect. Even a post-reconciliation
        reorg re-locks the original capital and stops new decisions. Existing
        signed transactions cannot be cancelled by this local operation.
        """
        if not isinstance(verifier, ExecutionEvidenceVerifier):
            raise MachineError("external execution evidence verifier required")
        if not self.verify_receipt(receipt_hash):
            raise MachineError("decision receipt does not replay")
        require_keys(evidence, {"network", "intent_hash", "txid", "kind",
                                "observed_at", "block_height", "prior_receipt_hash",
                                "raw_evidence_hash", "evidence_ref"},
                     "execution exception evidence")
        observed_at = utc(evidence["observed_at"])
        if evidence["kind"] not in {"REVERTED", "PARTIAL_FILL", "REORG"}:
            raise MachineError("unsupported execution exception")
        ident(evidence["evidence_ref"], "exception evidence reference")
        if (type(evidence["block_height"]) is not int or evidence["block_height"] < 0
                or not isinstance(evidence["raw_evidence_hash"], str)
                or re.fullmatch(r"[0-9a-f]{64}", evidence["raw_evidence_hash"]) is None):
            raise MachineError("invalid exception block or evidence hash")
        evidence_hash = digest(evidence)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            history = self._execution_history(db, receipt_hash)
            prior = history.get("EXECUTION_EXCEPTION")
            if prior is not None:
                if prior["evidence_hash"] != evidence_hash or prior["evidence"] != evidence:
                    raise MachineError("conflicting execution exception evidence")
                return {"receipt_hash": receipt_hash, "stage": "DISPUTED",
                        "evidence_hash": evidence_hash, "capital_locked": True,
                        "runtime_halted": True, "idempotent": True}
            if "EXECUTION_SUBMIT" not in history:
                raise MachineError("execution exception requires a submitted transaction")
            receipt_row = db.execute("SELECT p.receipt_json,r.status FROM receipts p "
                                     "JOIN reservations r ON r.reservation_id=p.receipt_hash "
                                     "WHERE p.receipt_hash=?", (receipt_hash,)).fetchone()
            if receipt_row is None or receipt_row["status"] not in {
                    "EXECUTION_LOCKED", "RECONCILED"}:
                raise MachineError("exception has no execution reservation")
            receipt = json.loads(receipt_row["receipt_json"])
            intent = receipt["intent"]
            submission = history["EXECUTION_SUBMIT"]["evidence"]
            finality = (history["EXECUTION_FINALIZE"]["evidence"]
                        if "EXECUTION_FINALIZE" in history else None)
            if (evidence["network"] != intent["network"]
                    or evidence["intent_hash"] != intent["intent_hash"]
                    or evidence["txid"] != submission["txid"]
                    or observed_at < utc(submission["submitted_at"]) or (
                        finality is not None and observed_at < utc(finality["finalized_at"]))):
                raise MachineError("exception does not bind the submitted transaction")
            if evidence["kind"] == "REORG":
                if (finality is None or evidence["prior_receipt_hash"] !=
                        finality["raw_receipt_hash"] or
                        evidence["block_height"] != finality["block_height"]):
                    raise MachineError("reorg does not bind a finalized receipt")
            elif (evidence["prior_receipt_hash"] is not None
                  and (finality is None or evidence["prior_receipt_hash"] != finality["raw_receipt_hash"])):
                raise MachineError("exception receipt hash mismatch")
            if not verifier.verify_execution_exception(intent, submission,
                                                       finality, evidence):
                raise MachineError("external execution exception not verified")
            revoked = [row["reservation_id"] for row in db.execute(
                "SELECT reservation_id FROM reservations WHERE status='ACTIVE' "
                "ORDER BY reservation_id")]
            db.execute("UPDATE reservations SET status='REVOKED' WHERE status='ACTIVE'")
            db.execute("UPDATE reservations SET status='EXECUTION_LOCKED' "
                       "WHERE reservation_id=? AND status='RECONCILED'", (receipt_hash,))
            event = {"kind": "EXECUTION_EXCEPTION", "receipt_hash": receipt_hash,
                     "intent_hash": intent["intent_hash"], "evidence_hash": evidence_hash,
                     "evidence": evidence, "revoked_reservations": revoked,
                     "runtime_halted": True}
            previous = next(reversed(history))
            event_hash = append_event(db, "execution-exception:" + receipt_hash,
                                      "EXECUTION_EXCEPTION", digest(history[previous]),
                                      evidence_hash, event)
            return {"receipt_hash": receipt_hash, "stage": "DISPUTED",
                    "evidence_hash": evidence_hash, "event_hash": event_hash,
                    "capital_locked": True, "runtime_halted": True,
                    "revoked_reservations": revoked, "idempotent": False}

    def record_inference(self, draft: dict, scope: dict, *, at: str) -> dict:
        """Audit a model proposal; never register its program or reserve capital."""
        at = utc(at)
        candidate = normalize_inference_draft(draft)
        boundary = normalize_inference_scope(scope)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not verify_journal(db):
                raise MachineError("journal integrity failed; inference denied")
            state_row = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            if state_row is None:
                raise MachineError("no state installed")
            assessment = assess_inference(candidate, boundary,
                                          json.loads(state_row["state_json"]), at=at)
            prior = db.execute("SELECT candidate_hash,scope_hash,state_sequence,assessed_at,"
                               "candidate_json,scope_json,assessment_json "
                               "FROM inference_assessments WHERE assessment_hash=?",
                               (assessment["assessment_hash"],)).fetchone()
            if prior:
                if (prior["candidate_hash"] != assessment["candidate_hash"]
                        or prior["scope_hash"] != assessment["scope_hash"]
                        or prior["state_sequence"] != assessment["state_sequence"]
                        or prior["assessed_at"] != at
                        or prior["candidate_json"] != canonical(candidate).decode()
                        or prior["scope_json"] != canonical(boundary).decode()
                        or prior["assessment_json"] != canonical(assessment).decode()):
                    raise MachineError("inference assessment identity conflict")
                return {**assessment, "idempotent": True}
            db.execute("INSERT INTO inference_assessments VALUES (?,?,?,?,?,?,?,?)",
                       (assessment["assessment_hash"], assessment["candidate_hash"],
                        assessment["scope_hash"], assessment["state_sequence"], at,
                        canonical(candidate).decode(), canonical(boundary).decode(),
                        canonical(assessment).decode()))
            event = {"kind": "INFERENCE_ASSESSMENT",
                     "assessment_hash": assessment["assessment_hash"],
                     "candidate_hash": assessment["candidate_hash"],
                     "scope_hash": assessment["scope_hash"], "status": assessment["status"],
                     "state_sequence": assessment["state_sequence"]}
            append_event(db, "inference:" + assessment["assessment_hash"],
                         "INFERENCE_ASSESSMENT", assessment["candidate_hash"],
                         assessment["assessment_hash"], event)
            return {**assessment, "idempotent": False}

    def verify_inference(self, assessment_hash: str) -> bool:
        """Replay a stored inference assessment against its historical state."""
        with self.connect() as db:
            record = db.execute("SELECT * FROM inference_assessments WHERE assessment_hash=?",
                                (assessment_hash,)).fetchone()
            if record is None:
                raise MachineError("inference assessment unknown")
            state = db.execute("SELECT state_json,state_root FROM states WHERE sequence=?",
                               (record["state_sequence"],)).fetchone()
            journal_ok = verify_journal(db)
        if state is None or not journal_ok:
            return False
        try:
            candidate = json.loads(record["candidate_json"])
            boundary = json.loads(record["scope_json"])
            stored = json.loads(record["assessment_json"])
            world = json.loads(state["state_json"])
            if state_root(world) != state["state_root"]:
                return False
            replay = assess_inference(candidate, boundary, world, at=record["assessed_at"])
            return (replay == stored and assessment_hash == replay["assessment_hash"]
                    and digest(candidate) == record["candidate_hash"]
                    and digest(boundary) == record["scope_hash"])
        except (ValueError, TypeError, MachineError):
            return False

    def pending_escalations(self) -> list[dict]:
        """Read unresolved exceptions; this never calls a language model."""
        with self.connect() as db:
            if not verify_journal(db):
                raise MachineError("journal integrity failed; escalation read denied")
            expected = {}
            for event in db.execute("SELECT kind,event_json FROM events ORDER BY ordinal"):
                body = json.loads(event["event_json"])
                if event["kind"] == "ESCALATION_OPEN":
                    expected[body["program_id"]] = {
                        "version": body["version"], "reason_code": body["reason_code"],
                        "receipt_hash": body["receipt_hash"]}
                elif event["kind"] in {"ESCALATION_CLOSE", "REGISTER_PROGRAM"} or event["kind"] == "PROGRAM_CONTROL" and body["to"] == "PAUSED":
                    expected.pop(body["program_id"], None)
            rows = [dict(row) for row in db.execute(
                "SELECT program_id,version,reason_code,receipt_hash,state_root,opened_at "
                "FROM escalations WHERE status='OPEN' ORDER BY program_id")]
            if set(expected) != {row["program_id"] for row in rows}:
                raise MachineError("escalation projection differs from journal")
            for row in rows:
                event = expected[row["program_id"]]
                if any(row[key] != event[key] for key in event):
                    raise MachineError("escalation projection differs from journal")
                active = db.execute("SELECT a.version,p.state FROM active_programs a "
                                    "JOIN programs p ON p.program_id=a.program_id "
                                    "AND p.version=a.version WHERE a.program_id=?",
                                    (row["program_id"],)).fetchone()
                receipt = db.execute("SELECT receipt_json FROM receipts WHERE receipt_hash=?",
                                     (row["receipt_hash"],)).fetchone()
                if active is None or active["version"] != row["version"] or active["state"] != "ACTIVE" or receipt is None:
                    raise MachineError("escalation projection has no active receipt")
                recorded = json.loads(receipt["receipt_json"])
                if (recorded["terminal_state"] != "ESCALATED"
                        or recorded["reason_code"] != row["reason_code"]
                        or recorded["state_root"] != row["state_root"]
                        or recorded["at"] != row["opened_at"]):
                    raise MachineError("escalation projection differs from receipt")
        for row in rows:
            if not self.verify_receipt(row["receipt_hash"]):
                raise MachineError("escalation receipt replay failed")
        return rows

    def _sync_escalation(self, db: sqlite3.Connection, row: sqlite3.Row,
                         receipt: dict) -> None:
        prior = db.execute("SELECT version,status,reason_code FROM escalations "
                           "WHERE program_id=?", (row["program_id"],)).fetchone()
        opened = receipt["terminal_state"] == "ESCALATED"
        if opened and prior is not None and prior["status"] == "OPEN" and (
                prior["version"], prior["reason_code"]) == (
                    row["version"], receipt["reason_code"]):
            return  # One unresolved exception, even across repeated observations.
        if not opened and (prior is None or prior["status"] != "OPEN"):
            return
        if opened:
            db.execute("INSERT INTO escalations VALUES (?,?,?,?,?,?,?) ON CONFLICT(program_id) "
                       "DO UPDATE SET version=excluded.version,status='OPEN',"
                       "reason_code=excluded.reason_code,receipt_hash=excluded.receipt_hash,"
                       "state_root=excluded.state_root,opened_at=excluded.opened_at",
                       (row["program_id"], row["version"], "OPEN", receipt["reason_code"],
                        receipt["receipt_hash"], receipt["state_root"], receipt["at"]))
        else:
            db.execute("UPDATE escalations SET status='CLOSED' WHERE program_id=?",
                       (row["program_id"],))
        ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 AS next FROM events").fetchone()["next"]
        event = {"kind": "ESCALATION_OPEN" if opened else "ESCALATION_CLOSE",
                 "program_id": row["program_id"], "version": row["version"],
                 "reason_code": receipt["reason_code"], "receipt_hash": receipt["receipt_hash"]}
        append_event(db, f"escalation:{ordinal}", event["kind"],
                     receipt["receipt_hash"], digest(event), event)

    def _evaluate_in_tx(self, db: sqlite3.Connection, row: sqlite3.Row,
                        state: dict, at: str) -> dict:
        if row["state"] != "ACTIVE":
            raise MachineError("program is paused")
        self._verify_execution_locks(db)
        self._ensure_no_execution_dispute(db)
        locked = db.execute("SELECT 1 FROM reservations WHERE program_id=? "
                            "AND status='EXECUTION_LOCKED' LIMIT 1",
                            (row["program_id"],)).fetchone()
        if locked is not None:
            raise MachineError("program has an unresolved execution lock")
        program = json.loads(row["body_json"])
        db.execute("UPDATE reservations SET status='EXPIRED' WHERE status='ACTIVE' "
                   "AND expires_at<=?", (at,))
        pending = []
        for item in db.execute(
            "SELECT reservation_id,program_id,owner_id,agent_id,network,asset,amount,cost,"
            "expires_at,status FROM reservations WHERE status IN ('ACTIVE','EXECUTION_LOCKED') "
            "AND owner_id=? AND program_id!=? "
            "AND (status='EXECUTION_LOCKED' OR expires_at>?) ORDER BY reservation_id",
            (program["owner_id"], row["program_id"], at)):
            reservation = dict(item)
            if reservation["status"] == "ACTIVE":
                del reservation["status"]  # Preserve the v1 pending receipt hash.
            pending.append(reservation)
        receipt = self.kernel.evaluate(program, state, at=at, pending_reservations=pending)
        if receipt["terminal_state"] == "AWAITING_AUTHORIZATION":
            retired = db.execute("SELECT status FROM reservations WHERE reservation_id=?",
                                 (receipt["receipt_hash"],)).fetchone()
            if retired is not None and retired["status"] != "ACTIVE":
                raise MachineError("retired intent cannot be reactivated")
            prior = db.execute("SELECT reservation_id FROM reservations WHERE program_id=? "
                               "AND status='ACTIVE'", (row["program_id"],)).fetchone()
            if prior is None or prior["reservation_id"] != receipt["receipt_hash"]:
                db.execute("UPDATE reservations SET status='SUPERSEDED' WHERE program_id=? "
                           "AND status='ACTIVE'", (row["program_id"],))
                action = receipt["action"]
                db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(reservation_id) DO UPDATE SET status='ACTIVE'",
                           (receipt["receipt_hash"], row["program_id"], row["version"],
                            program["owner_id"], program["agent_id"], program["network"], action["asset"],
                            action["amount"], action["total_cost"],
                            receipt["intent"]["expires_at"], "ACTIVE"))
        else:
            db.execute("UPDATE reservations SET status='SUPERSEDED' WHERE program_id=? "
                       "AND status='ACTIVE'", (row["program_id"],))
        db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?,?)",
                   (receipt["receipt_hash"], row["program_id"], row["version"],
                    state["sequence"], receipt["at"], receipt["terminal_state"],
                    canonical(receipt).decode()))
        append_event(db, "evaluate:" + receipt["receipt_hash"], "EVALUATE",
                     receipt["state_root"], receipt["receipt_hash"],
                     {"program_id": row["program_id"], "version": row["version"],
                      "receipt_hash": receipt["receipt_hash"]})
        self._sync_escalation(db, row, receipt)
        return receipt

    def evaluate(self, program_id: str, *, at: str) -> dict:
        at = utc(at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            self._ensure_no_execution_dispute(db)
            row = db.execute("SELECT p.* FROM programs p JOIN active_programs a "
                             "ON a.program_id=p.program_id AND a.version=p.version "
                             "WHERE p.program_id=?", (program_id,)).fetchone()
            if row is None:
                raise MachineError("program not registered")
            if row["state"] != "ACTIVE":
                raise MachineError("program is paused")
            state_row = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            if state_row is None:
                raise MachineError("no state installed")
            return self._evaluate_in_tx(db, row, json.loads(state_row["state_json"]), at)

    def ingest(self, delta: dict, *, event_id: str) -> dict:
        """Apply one externally authenticated delta; evaluate only affected programs."""
        if not isinstance(event_id, str) or len(event_id) < 8 or len(event_id) > 128:
            raise MachineError("stable event_id required")
        if event_id.startswith(("evaluate:", "program-control:", "inference:",
                                "execution-",
                                "escalation:")):
            raise MachineError("reserved event id prefix")
        input_hash = digest(delta)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            prior = db.execute("SELECT input_hash,event_json FROM events WHERE event_id=?",
                               (event_id,)).fetchone()
            if prior:
                if prior["input_hash"] != input_hash:
                    raise MachineError("event id reused with different input")
                return {**json.loads(prior["event_json"]), "idempotent": True}
            state_row = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            if state_row is None:
                raise MachineError("no state installed")
            next_state, changes = apply_delta(json.loads(state_row["state_json"]), delta)
            root = state_root(next_state)
            db.execute("INSERT INTO states VALUES (?,?,?)",
                       (next_state["sequence"], root, canonical(next_state).decode()))
            affected = set()
            for key in changes:
                lookup = (key, "quote:*") if key.startswith("quote:") else (key,)
                for value in lookup:
                    affected.update(row["program_id"] for row in db.execute(
                        "SELECT d.program_id FROM dependencies d JOIN active_programs a "
                        "ON a.program_id=d.program_id AND a.version=d.version "
                        "JOIN programs p ON p.program_id=a.program_id AND p.version=a.version "
                        "WHERE d.state_key=? AND p.state='ACTIVE'", (value,)))
            receipts = []
            locked_programs = []
            dispute_halt = db.execute("SELECT 1 FROM events WHERE kind='EXECUTION_EXCEPTION' "
                                      "LIMIT 1").fetchone() is not None
            for program_id in sorted(affected):
                if dispute_halt:
                    continue
                if db.execute("SELECT 1 FROM reservations WHERE program_id=? "
                              "AND status='EXECUTION_LOCKED' LIMIT 1",
                              (program_id,)).fetchone() is not None:
                    locked_programs.append(program_id)
                    continue
                row = db.execute("SELECT p.* FROM programs p JOIN active_programs a "
                                 "ON a.program_id=p.program_id AND a.version=p.version "
                                 "WHERE p.program_id=? AND p.state='ACTIVE'", (program_id,)).fetchone()
                receipts.append(self._evaluate_in_tx(db, row, next_state, delta["as_of"]))
            event = {"event_id": event_id, "kind": "STATE_DELTA",
                     "sequence": next_state["sequence"], "state_root": root,
                     "semantic_changes": changes, "affected_programs": sorted(affected),
                     "execution_locked_programs": locked_programs,
                     "execution_dispute_halt": dispute_halt,
                     "receipt_hashes": [r["receipt_hash"] for r in receipts],
                     "kernel_calls": len(receipts), "llm_calls": 0}
            append_event(db, event_id, "STATE_DELTA", input_hash, root, event)
            return {**event, "idempotent": False}

    def tick(self, *, at: str) -> dict:
        """Expire only due unsent intents; ordinary ticks consume no model or kernel."""
        at = utc(at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_execution_locks(db)
            if db.execute("SELECT 1 FROM events WHERE kind='EXECUTION_EXCEPTION' "
                          "LIMIT 1").fetchone() is not None:
                return {"at": at, "due_programs": [], "kernel_calls": 0,
                        "llm_calls": 0, "receipt_hashes": [],
                        "execution_dispute_halt": True}
            due = [row["program_id"] for row in db.execute(
                "SELECT DISTINCT program_id FROM reservations WHERE status='ACTIVE' "
                "AND expires_at<=? ORDER BY program_id", (at,))]
            if not due:
                return {"at": at, "due_programs": [], "kernel_calls": 0,
                        "llm_calls": 0, "receipt_hashes": []}
            state_row = db.execute("SELECT state_json FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            if state_row is None:
                raise MachineError("no state installed")
            state = json.loads(state_row["state_json"])
            if at < state["as_of"]:
                raise MachineError("tick precedes state")
            receipts = []
            for program_id in due:
                row = db.execute("SELECT p.* FROM programs p JOIN active_programs a "
                                 "ON a.program_id=p.program_id AND a.version=p.version "
                                 "WHERE p.program_id=? AND p.state='ACTIVE'", (program_id,)).fetchone()
                if row is None:
                    db.execute("UPDATE reservations SET status='EXPIRED' "
                               "WHERE program_id=? AND status='ACTIVE'", (program_id,))
                    continue
                receipts.append(self._evaluate_in_tx(db, row, state, at))
            event = {"kind": "CLOCK_TICK", "at": at, "due_programs": due,
                     "receipt_hashes": [item["receipt_hash"] for item in receipts]}
            append_event(db, digest(event), "CLOCK_TICK", digest({"at": at}),
                         digest(event["receipt_hashes"]), event)
            return {"at": at, "due_programs": due, "kernel_calls": len(receipts),
                    "llm_calls": 0, "receipt_hashes": event["receipt_hashes"]}

    def verify_receipt(self, receipt_hash: str) -> bool:
        """Replay exact program/state/time and compare complete deterministic receipt."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM receipts WHERE receipt_hash=?", (receipt_hash,)).fetchone()
            if row is None:
                raise MachineError("receipt unknown")
            program = db.execute("SELECT body_json,program_hash FROM programs "
                                 "WHERE program_id=? AND version=?",
                                 (row["program_id"], row["version"])).fetchone()
            state = db.execute("SELECT state_json,state_root FROM states WHERE sequence=?",
                               (row["state_sequence"],)).fetchone()
            journal_ok = verify_journal(db)
        if program is None or state is None:
            return False
        if not journal_ok:
            return False
        program_body = json.loads(program["body_json"])
        state_body = json.loads(state["state_json"])
        if (program_body.get("program_hash") != program["program_hash"]
                or state_root(state_body) != state["state_root"]):
            return False
        stored = json.loads(row["receipt_json"])
        replay = self.kernel.evaluate(program_body,
                                      state_body, at=row["evaluated_at"],
                                      pending_reservations=stored["pending_reservations"])
        return (replay == stored and digest({k: v for k, v in stored.items()
                                             if k != "receipt_hash"}) == receipt_hash)

    def status(self) -> dict:
        with self.connect() as db:
            self._verify_execution_locks(db)
            state = db.execute("SELECT sequence,state_root FROM states ORDER BY sequence DESC LIMIT 1").fetchone()
            programs = db.execute("SELECT a.program_id,a.version,p.program_hash,p.state FROM active_programs a "
                                  "JOIN programs p ON p.program_id=a.program_id AND p.version=a.version "
                                  "ORDER BY a.program_id").fetchall()
            events = db.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
            receipts = db.execute("SELECT count(*) AS n FROM receipts").fetchone()["n"]
            unsent_reservations = db.execute("SELECT count(*) AS n FROM reservations "
                                             "WHERE status='ACTIVE'").fetchone()["n"]
            execution_locks = db.execute("SELECT count(*) AS n FROM reservations "
                                         "WHERE status='EXECUTION_LOCKED'").fetchone()["n"]
            execution_disputes = db.execute("SELECT count(*) AS n FROM events "
                                            "WHERE kind='EXECUTION_EXCEPTION'").fetchone()["n"]
            reconciled = db.execute("SELECT count(*) AS n FROM reservations "
                                    "WHERE status='RECONCILED'").fetchone()["n"]
            assessments = db.execute("SELECT count(*) AS n FROM inference_assessments").fetchone()["n"]
            escalations = db.execute("SELECT count(*) AS n FROM escalations e "
                                     "JOIN active_programs a ON a.program_id=e.program_id "
                                     "AND a.version=e.version JOIN programs p ON p.program_id=e.program_id "
                                     "AND p.version=e.version WHERE e.status='OPEN' "
                                     "AND p.state='ACTIVE'").fetchone()["n"]
            journal_ok = verify_journal(db)
        return {"runtime": "economic-runtime-0.5.0", "execution_port": "NOT_INSTALLED",
                "state": dict(state) if state else None,
                "active_programs": [dict(row) for row in programs],
                "events": events, "receipts": receipts,
                "inference_assessments": assessments,
                "open_escalations": escalations,
                "active_reservations": unsent_reservations + execution_locks,
                "unsent_reservations": unsent_reservations,
                "execution_locked_reservations": execution_locks,
                "execution_disputes": execution_disputes,
                "execution_halt": execution_disputes > 0,
                "reconciled_reservations": reconciled,
                "capital_encumbered_reservations": unsent_reservations + execution_locks,
                "journal_integrity": journal_ok}
