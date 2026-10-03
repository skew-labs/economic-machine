"""Owner-scoped work briefs and confirmed preferences, without payment authority.

The same workspace DB/journal stores tasks, immutable revisions and context.
Typed fields are explicitly supplied; free text is not silently LLM-parsed here.
"""

import json
import os
import re
import secrets
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, require_keys

KINDS = {
    "vendor_comparison": {
        "name": "Compare vendors",
        "formats": ["table", "document"],
        "required": ["output_language", "output_format", "comparison_fields"],
        "example": "Compare three software vendors by price, delivery time and support.",
    },
    "research_brief": {
        "name": "Research a topic",
        "formats": ["document", "slides"],
        "required": ["output_language", "output_format"],
        "example": "Prepare a sourced competitor brief for our sales meeting.",
    },
    "document_draft": {
        "name": "Draft a document",
        "formats": ["document", "slides"],
        "required": ["output_language", "output_format"],
        "example": "Turn the project notes into a customer proposal.",
    },
    "data_cleanup": {
        "name": "Clean a dataset",
        "formats": ["csv", "json"],
        "required": ["output_format", "required_fields"],
        "example": "Normalize the customer list and flag duplicate email addresses.",
    },
    "content_localization": {
        "name": "Translate and adapt",
        "formats": ["document", "json"],
        "required": ["source_language", "output_language", "output_format"],
        "example": "Adapt our product descriptions for Korean customers.",
    },
}
FIELDS = {
    "regions",
    "source_language",
    "output_language",
    "output_format",
    "tone",
    "excluded_providers",
    "comparison_fields",
    "required_fields",
}
INPUTS = {
    "kind",
    "title",
    "instructions",
    "budget",
    "deadline_at",
    "constraints",
    "preference_id",
    "connection_ids",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_tasks (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
 created INTEGER NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engine_task_revisions (
 task_id TEXT NOT NULL REFERENCES engine_tasks(id), revision INTEGER NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL, created INTEGER NOT NULL,
 PRIMARY KEY(task_id,revision));
CREATE TABLE IF NOT EXISTS engine_task_preferences (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL, expires INTEGER NOT NULL,
 status TEXT NOT NULL, created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS task_preference_kind ON engine_task_preferences(kind,created);
CREATE TABLE IF NOT EXISTS engine_task_requests (
 request_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, result TEXT NOT NULL);
"""


def text(value, maximum, *, multiline=False):
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= maximum
        or any(ord(c) < 32 and not (multiline and c in "\n\r\t") for c in value)
        or re.search(r"-----BEGIN .*PRIVATE KEY-----|sk-(?:proj-|ant-|bk-)[A-Za-z0-9_-]{20,}", value)
    ):
        raise MachineError("BOUNDED_TASK_TEXT_WITHOUT_CREDENTIALS_REQUIRED")
    return value.strip()


def usd(raw, field):
    require_keys(raw, {"currency", field}, "USD amount")
    amount = raw[field]
    if (
        raw["currency"] != "USD"
        or not isinstance(amount, str)
        or re.fullmatch(r"(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,2})?", amount) is None
    ):
        raise MachineError("EXPLICIT_USD_DECIMAL_REQUIRED")
    dollars, _, fraction = amount.partition(".")
    cents = int(dollars) * 100 + int((fraction + "00")[:2])
    if cents > 1000000:
        raise MachineError("TASK_USD_LIMIT_EXCEEDED")
    return {"currency": "USD", field: f"{cents // 100}.{cents % 100:02d}", "cents": cents}


def constraints(raw):
    if not isinstance(raw, dict) or not set(raw) <= FIELDS:
        raise MachineError("TASK_PREFERENCES_CANNOT_CONTAIN_BUDGET_OR_AUTHORITY")
    result = {}
    for key, value in raw.items():
        if key in {"source_language", "output_language"}:
            if not isinstance(value, str) or re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", value) is None:
                raise MachineError("EXPLICIT_LANGUAGE_CODE_REQUIRED")
            result[key] = value
        elif key == "output_format":
            if not isinstance(value, str) or value not in {"table", "document", "slides", "csv", "json"}:
                raise MachineError("SUPPORTED_TASK_OUTPUT_FORMAT_REQUIRED")
            result[key] = value
        elif key == "tone":
            if not isinstance(value, str) or value not in {"plain", "formal", "friendly"}:
                raise MachineError("SUPPORTED_TASK_TONE_REQUIRED")
            result[key] = value
        else:
            if not isinstance(value, list) or not 1 <= len(value) <= 16:
                raise MachineError("BOUNDED_TASK_CONDITION_LIST_REQUIRED")
            checked = [text(item, 100) for item in value]
            if len(set(checked)) != len(checked):
                raise MachineError("UNIQUE_TASK_CONDITIONS_REQUIRED")
            if key == "regions" and any(
                re.fullmatch(r"[A-Z][A-Z0-9-]{1,15}", item) is None for item in checked
            ):
                raise MachineError("EXPLICIT_REGION_CODES_REQUIRED")
            result[key] = checked
    return result


def checked_body(row):
    body = json.loads(row["body"])
    if digest(body) != row["body_hash"]:
        raise MachineError("TASK_RECORD_INTEGRITY_FAILED")
    return body


class TaskServices:
    """Operator-declared SKU metadata. No PayPal merchant/fulfillment admission."""

    def __init__(self, entries=None):
        if entries is None:
            reference = os.environ.get("ENGINE_TASK_SERVICES_FILE")
            if reference:
                path = Path(reference)
                if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 50000:
                    raise MachineError("OPERATOR_TASK_CATALOGUE_REQUIRED")
                entries = json.loads(path.read_bytes())
            else:
                entries = []
        if not isinstance(entries, list) or len(entries) > 64:
            raise MachineError("BOUNDED_TASK_SERVICE_CATALOGUE_REQUIRED")
        self.entries = []
        for raw in entries:
            require_keys(raw, {"sku", "merchant_id", "name", "kind", "price", "version"}, "task service SKU")
            if (
                not isinstance(raw["kind"], str)
                or raw["kind"] not in KINDS
                or type(raw["version"]) is not int
                or not 1 <= raw["version"] <= 100000
            ):
                raise MachineError("VERSIONED_TASK_SERVICE_REQUIRED")
            for key in ["sku", "merchant_id"]:
                if not isinstance(raw[key], str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", raw[key]) is None:
                    raise MachineError("EXPLICIT_SKU_AND_MERCHANT_REQUIRED")
            self.entries.append(
                raw
                | {
                    "name": text(raw["name"], 100),
                    "price": usd(raw["price"], "amount"),
                    "assurance": "OPERATOR_DECLARED_PRICE_NOT_PROVIDER_QUOTE",
                    "purchase_enabled": False,
                    "fulfillment_connected": False,
                }
            )
        if len({row["sku"] for row in self.entries}) != len(self.entries):
            raise MachineError("UNIQUE_TASK_SKU_REQUIRED")
        self.hash = digest(self.entries)

    def catalogue(self):
        return {
            "templates": [dict(kind=key, **value) for key, value in KINDS.items()],
            "services": self.entries,
            "catalogue_hash": self.hash,
            "payment_authority": "NONE",
        }


class Tasks:
    def __init__(self, workspace, *, services=None):
        self.work, self.services = workspace, services or TaskServices()
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def _replay(self, db, key, fingerprint):
        text(key, 100)
        row = db.execute("SELECT * FROM engine_task_requests WHERE request_id=?", (key,)).fetchone()
        if row:
            if row["input_hash"] != fingerprint:
                raise MachineError("TASK_REQUEST_ID_REUSED_WITH_DIFFERENT_INPUT")
            return json.loads(row["result"])
        return None

    def _receipt(self, db, key, fingerprint, result, event):
        db.execute(
            "INSERT INTO engine_task_requests VALUES (?,?,?)", (key, fingerprint, canonical(result).decode())
        )
        self.work.event(
            db,
            event,
            {
                "request_id": key,
                "input_hash": fingerprint,
                "result_hash": digest(result),
                "payment_authority": "NONE",
            },
        )
        return result

    def _draft(self, db, raw):
        require_keys(raw, INPUTS, "task brief")
        if not isinstance(raw["kind"], str) or raw["kind"] not in KINDS:
            raise MachineError("SUPPORTED_WORK_TASK_REQUIRED")
        at = int(self.work.clock())
        deadline = raw["deadline_at"]
        if deadline is not None and (type(deadline) is not int or not at < deadline <= at + 31536000):
            raise MachineError("FUTURE_TASK_DEADLINE_REQUIRED")
        refs = raw["connection_ids"]
        if (
            not isinstance(refs, list)
            or len(refs) > 16
            or any(not isinstance(cid, str) or len(cid) > 100 for cid in refs)
            or len(set(refs)) != len(refs)
        ):
            raise MachineError("BOUNDED_TASK_CONNECTIONS_REQUIRED")
        for cid in refs:
            row = db.execute("SELECT status FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if not row or row[0] == "DISCONNECTED":
                raise MachineError("TASK_CONNECTION_MUST_BELONG_TO_ACTIVE_WORKSPACE")
        explicit = constraints(raw["constraints"])
        inherited, provenance, missing = {}, {}, []
        pid = raw["preference_id"]
        if pid is not None:
            text(pid, 100)
            if pid == "last":
                row = db.execute(
                    "SELECT * FROM engine_task_preferences WHERE kind=? AND status='CONFIRMED' AND expires>? "
                    "ORDER BY created DESC,rowid DESC LIMIT 1",
                    (raw["kind"], at),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM engine_task_preferences WHERE id=? AND kind=? AND status='CONFIRMED' AND expires>?",
                    (pid, raw["kind"], at),
                ).fetchone()
            if not row:
                missing.append("confirmed_preferences")
            else:
                inherited = checked_body(row)
                provenance = {"id": row["id"], "version": row["version"], "hash": row["body_hash"]}
        effective = inherited | explicit
        missing.extend(field for field in KINDS[raw["kind"]]["required"] if field not in effective)
        if effective.get("output_format") and effective["output_format"] not in KINDS[raw["kind"]]["formats"]:
            raise MachineError("OUTPUT_FORMAT_INCOMPATIBLE_WITH_TASK")
        return {
            "kind": raw["kind"],
            "title": text(raw["title"], 160),
            "instructions": text(raw["instructions"], 12000, multiline=True),
            "budget": usd(raw["budget"], "maximum"),
            "deadline_at": deadline,
            "constraints": effective,
            "explicit_constraints": explicit,
            "preference_id": pid,
            "inherited_preference": provenance,
            "inherited_fields": sorted(set(inherited) - set(explicit)),
            "connection_ids": refs,
            "missing_information": missing,
            "interpretation": "EXPLICIT_TYPED_FIELDS_NOT_LLM_INFERENCE",
            "payment_authority": "NONE",
            "automatic_execution": False,
        }

    def _row(self, db, tid):
        row = db.execute("SELECT * FROM engine_tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise MachineError("TASK_NOT_FOUND")
        return row

    def _public(self, db, row):
        revision = db.execute(
            "SELECT * FROM engine_task_revisions WHERE task_id=? AND revision=?", (row["id"], row["revision"])
        ).fetchone()
        brief = checked_body(revision)
        missing = list(brief["missing_information"])
        source = brief["inherited_preference"]
        if source and brief["inherited_fields"]:
            pref = db.execute("SELECT * FROM engine_task_preferences WHERE id=?", (source["id"],)).fetchone()
            if (
                not pref
                or pref["status"] != "CONFIRMED"
                or pref["expires"] <= int(self.work.clock())
                or pref["body_hash"] != source["hash"]
                or digest(checked_body(pref)) != source["hash"]
            ):
                missing.append("confirmed_preferences")
        for cid in brief["connection_ids"]:
            connection = db.execute("SELECT status FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if not connection or connection[0] == "DISCONNECTED":
                missing.append("active_connections")
        status = "NEEDS_INPUT" if missing else "READY_TO_PLAN"
        if brief["deadline_at"] and brief["deadline_at"] <= int(self.work.clock()):
            status = "EXPIRED"
        if row["status"] == "CANCELLED_UNSENT":
            status = row["status"]
        return {
            "id": row["id"],
            "revision": row["revision"],
            "status": status,
            "created_at": row["created"],
            "updated_at": row["updated"],
            "brief_hash": revision["body_hash"],
            "brief": brief,
            "missing_information": sorted(set(missing)),
            "payment_authority": "NONE",
        }

    def create(self, raw):
        require_keys(raw, INPUTS | {"request_id"}, "new task")
        fingerprint = digest({"operation": "CREATE", "input": raw})
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, raw["request_id"], fingerprint)
            if replay is not None:
                return replay
            if db.execute("SELECT COUNT(*) FROM engine_tasks").fetchone()[0] >= 256:
                raise MachineError("TASK_WORKSPACE_CAPACITY_REACHED")
            brief = self._draft(db, {k: raw[k] for k in INPUTS})
            at, tid = int(self.work.clock()), "task-" + secrets.token_hex(12)
            db.execute("INSERT INTO engine_tasks VALUES (?,1,'ACTIVE',?,?)", (tid, at, at))
            db.execute(
                "INSERT INTO engine_task_revisions VALUES (?,1,?,?,?)",
                (tid, canonical(brief).decode(), digest(brief), at),
            )
            return self._receipt(
                db, raw["request_id"], fingerprint, self._public(db, self._row(db, tid)), "WORK_TASK_CREATED"
            )

    def revise(self, tid, raw):
        require_keys(raw, {"request_id", "expected_revision", "draft"}, "task revision")
        fingerprint = digest({"operation": "REVISE", "task_id": tid, "input": raw})
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, raw["request_id"], fingerprint)
            if replay is not None:
                return replay
            row = self._row(db, tid)
            self._revision_guard(row, raw["expected_revision"])
            if row["revision"] >= 64:
                raise MachineError("TASK_REVISION_LIMIT_REACHED")
            from .task_checkout import task_purchase_guard
            task_purchase_guard(db, tid)
            brief, at, version = self._draft(db, raw["draft"]), int(self.work.clock()), row["revision"] + 1
            db.execute(
                "INSERT INTO engine_task_revisions VALUES (?,?,?,?,?)",
                (tid, version, canonical(brief).decode(), digest(brief), at),
            )
            db.execute("UPDATE engine_tasks SET revision=?,updated=? WHERE id=?", (version, at, tid))
            return self._receipt(
                db, raw["request_id"], fingerprint, self._public(db, self._row(db, tid)), "WORK_TASK_REVISED"
            )

    @staticmethod
    def _revision_guard(row, expected):
        if type(expected) is not int or row["revision"] != expected:
            raise MachineError("TASK_CHANGED_REFRESH_BEFORE_EDITING")
        if row["status"] != "ACTIVE":
            raise MachineError("TASK_ALREADY_CANCELLED_UNSENT")

    def cancel(self, tid, raw):
        require_keys(raw, {"request_id", "expected_revision"}, "unsent task cancellation")
        fingerprint = digest({"operation": "CANCEL", "task_id": tid, "input": raw})
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, raw["request_id"], fingerprint)
            if replay is not None:
                return replay
            row = self._row(db, tid)
            self._revision_guard(row, raw["expected_revision"])
            from .task_checkout import task_purchase_guard
            task_purchase_guard(db, tid)
            db.execute(
                "UPDATE engine_tasks SET status='CANCELLED_UNSENT',updated=? WHERE id=?",
                (int(self.work.clock()), tid),
            )
            return self._receipt(
                db,
                raw["request_id"],
                fingerprint,
                self._public(db, self._row(db, tid)),
                "WORK_TASK_CANCELLED_UNSENT",
            )

    def remember(self, tid, raw):
        require_keys(
            raw,
            {"request_id", "expected_revision", "name", "fields", "expires_at"},
            "confirmed task preferences",
        )
        fingerprint = digest({"operation": "REMEMBER", "task_id": tid, "input": raw})
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, raw["request_id"], fingerprint)
            if replay is not None:
                return replay
            row = self._row(db, tid)
            self._revision_guard(row, raw["expected_revision"])
            task = self._public(db, row)
            if task["status"] != "READY_TO_PLAN":
                raise MachineError("RESOLVED_TASK_REQUIRED_BEFORE_REMEMBERING")
            fields = raw["fields"]
            if (
                not isinstance(fields, list)
                or not fields
                or any(not isinstance(k, str) or k not in task["brief"]["constraints"] for k in fields)
                or len(set(fields)) != len(fields)
            ):
                raise MachineError("SELECT_ONLY_CONFIRMED_PREFERENCE_FIELDS")
            at, expires = int(self.work.clock()), raw["expires_at"]
            if type(expires) is not int or not at < expires <= at + 31536000:
                raise MachineError("BOUNDED_PREFERENCE_EXPIRY_REQUIRED")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM engine_task_preferences WHERE status='CONFIRMED' AND expires>?",
                    (at,),
                ).fetchone()[0]
                >= 128
            ):
                raise MachineError("REVOKE_AN_UNUSED_PREFERENCE_FIRST")
            name, kind = text(raw["name"], 100), task["brief"]["kind"]
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM engine_task_preferences WHERE kind=? AND name=?",
                (kind, name),
            ).fetchone()[0]
            body = {key: task["brief"]["constraints"][key] for key in fields}
            pid = "preference-" + secrets.token_hex(12)
            db.execute(
                "INSERT INTO engine_task_preferences VALUES (?,?,?,?,?,?,?,'CONFIRMED',?)",
                (pid, kind, name, version, canonical(body).decode(), digest(body), expires, at),
            )
            return self._receipt(
                db,
                raw["request_id"],
                fingerprint,
                {
                    "id": pid,
                    "name": name,
                    "kind": kind,
                    "version": version,
                    "values": body,
                    "expires_at": expires,
                    "status": "CONFIRMED",
                    "payment_authority": "NONE",
                },
                "WORK_PREFERENCES_CONFIRMED",
            )

    def revoke(self, pid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,status FROM engine_task_preferences WHERE id=?", (pid,)).fetchone()
            if not row:
                raise MachineError("CONFIRMED_PREFERENCE_NOT_FOUND")
            if row["status"] != "REVOKED":
                db.execute("UPDATE engine_task_preferences SET status='REVOKED' WHERE id=?", (pid,))
                self.work.event(db, "WORK_PREFERENCES_REVOKED", {"id": pid})
        return {"id": pid, "status": "REVOKED", "payment_authority": "NONE"}

    def get(self, tid):
        with self.work.runtime.connect() as db:
            result = self._public(db, self._row(db, tid))
            result["history"] = [
                dict(row)
                for row in db.execute(
                    "SELECT revision,body_hash,created FROM engine_task_revisions WHERE task_id=? ORDER BY revision",
                    (tid,),
                )
            ]
        return result

    def status(self):
        with self.work.runtime.connect() as db:
            tasks = [
                self._public(db, row)
                for row in db.execute("SELECT * FROM engine_tasks ORDER BY updated DESC,rowid DESC LIMIT 50")
            ]
            prefs = [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "name": row["name"],
                    "version": row["version"],
                    "values": checked_body(row),
                    "expires_at": row["expires"],
                    "status": "EXPIRED"
                    if row["status"] == "CONFIRMED" and row["expires"] <= int(self.work.clock())
                    else row["status"],
                }
                for row in db.execute(
                    "SELECT * FROM engine_task_preferences ORDER BY created DESC,rowid DESC LIMIT 128"
                )
            ]
        return self.services.catalogue() | {
            "tasks": tasks,
            "preferences": prefs,
            "automatic_execution": False,
        }
