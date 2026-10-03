"""Frozen work purchases: owner approval, durable uncertainty and verified delivery."""

import csv
import io
import json
import secrets
import uuid

from economic_machine.values import MachineError, canonical, digest, require_keys

from .paypal import PayPalSandbox, bound_order, provider_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_task_purchases (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
 input_hash TEXT NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL,
 create_key TEXT NOT NULL, capture_key TEXT NOT NULL, order_id TEXT UNIQUE,
 capture_id TEXT UNIQUE, result TEXT, result_hash TEXT, created INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS task_single_active_purchase
 ON engine_task_purchases(task_id) WHERE status!='REVOKED';
"""


def clean_csv(raw, columns, output_format):
    require_keys(raw, {"csv"}, "CSV cleanup input")
    source = raw["csv"]
    if (
        not isinstance(source, str)
        or not 1 <= len(source.encode()) <= 20000
        or "\x00" in source
        or output_format not in {"csv", "json"}
    ):
        raise MachineError("BOUNDED_CSV_INPUT_REQUIRED")
    try:
        rows = list(csv.reader(io.StringIO(source), strict=True))
    except csv.Error as exc:
        raise MachineError("INVALID_CSV_INPUT") from exc
    if (
        not rows
        or rows[0] != columns
        or not 1 <= len(columns) <= 32
        or len(rows) > 2001
        or any(len(row) != len(columns) for row in rows[1:])
        or len(rows) < 2
    ):
        raise MachineError("CSV_COLUMNS_OR_ROW_SHAPE_MISMATCH")
    cleaned = [[cell.strip() for cell in row] for row in rows[1:]]
    if output_format == "json":
        body = json.dumps([dict(zip(columns, row, strict=True)) for row in cleaned], ensure_ascii=False)
        mime = "application/json"
    else:
        # Escape spreadsheet formulas in headers AND values, preserving original data in JSON.
        def safe(cell):
            return "'" + cell if cell.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else cell

        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerows([[safe(cell) for cell in row] for row in [columns] + cleaned])
        body, mime = stream.getvalue(), "text/csv"
    return {
        "content": body,
        "content_type": mime,
        "filename": "cleaned." + output_format,
        "rows": len(cleaned),
        "columns": columns,
        "transformation": "TRIM_AND_FORMULA_SAFE_EXPORT",
        "data_truth_verified": False,
    }


def task_purchase_guard(db, tid):
    """Editing an unsent plan revokes it. Any provider intent locks the task."""
    locked = db.execute(
        "SELECT 1 FROM engine_task_purchases WHERE task_id=? AND status NOT IN ('PLANNED','REVOKED')", (tid,)
    ).fetchone()
    if locked:
        raise MachineError("TASK_PURCHASE_IN_FLIGHT_OR_PAID")
    db.execute(
        "UPDATE engine_task_purchases SET status='REVOKED' WHERE task_id=? AND status='PLANNED'", (tid,)
    )


class TaskCheckout:
    def __init__(self, workspace, *, provider=None):
        self.work = workspace
        self.provider = provider if provider is not None else PayPalSandbox.configured()
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def _provider(self):
        if self.provider is None or not self.provider.credentials_available():
            raise MachineError("PAYPAL_SANDBOX_NOT_CONFIGURED")
        return self.provider

    def _row(self, db, pid):
        row = db.execute("SELECT * FROM engine_task_purchases WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise MachineError("TASK_PURCHASE_NOT_FOUND")
        self._plan(row)
        return row

    @staticmethod
    def _plan(row):
        plan = json.loads(row["plan"])
        if (
            plan["id"] != row["id"]
            or plan["task_id"] != row["task_id"]
            or digest({k: v for k, v in plan.items() if k != "hash"}) != plan["hash"]
        ):
            raise MachineError("TASK_PURCHASE_INTEGRITY_FAILED")
        return plan

    def _public(self, row):
        plan = self._plan(row)
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "status": row["status"],
            "plan": {k: v for k, v in plan.items() if k != "input"},
            "order_id": row["order_id"],
            "capture_id": row["capture_id"],
            "approval_url": (
                "https://www.sandbox.paypal.com/checkoutnow?token=" + provider_id(row["order_id"])
            )
            if row["order_id"] and row["status"] == "AWAITING_APPROVAL"
            else None,
            "entitled": row["status"] in {"PAID", "DELIVERED"},
            "result_hash": row["result_hash"],
            "environment": "PAYPAL_SANDBOX",
            "real_money": False,
        }

    def status(self):
        provider = self.provider
        with self.work.runtime.connect() as db:
            rows = db.execute(
                "SELECT * FROM engine_task_purchases ORDER BY created DESC,rowid DESC"
            ).fetchall()
            return {
                "configured": provider is not None and provider.credentials_available(),
                "environment": "PAYPAL_SANDBOX",
                "services": list(provider.services.values()) if provider else [],
                "purchases": [self._public(row) for row in rows],
            }

    def get(self, pid):
        with self.work.runtime.connect() as db:
            return self._public(self._row(db, pid))

    def _event(self, db, row, event, **details):
        self.work.event(
            db,
            event,
            {
                "purchase_id": row["id"],
                "plan_hash": self._plan(row)["hash"],
                "status": row["status"],
                "order_id": row["order_id"],
                "capture_id": row["capture_id"],
                "result_hash": row["result_hash"],
                **details,
            },
        )

    def plan(self, tid, raw):
        require_keys(raw, {"request_id", "expected_revision", "sku", "input"}, "task purchase plan")
        key = raw["request_id"]
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            raise MachineError("PURCHASE_REQUEST_ID_REQUIRED")
        if not isinstance(raw["sku"], str):
            raise MachineError("ADMITTED_SERVICE_REQUIRED")
        fingerprint = digest({"task_id": tid, "input": raw})
        provider = self._provider()
        service = provider.services.get(raw["sku"])
        if service is None:
            raise MachineError("ADMITTED_SERVICE_REQUIRED")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM engine_task_purchases WHERE request_id=?", (key,)).fetchone()
            if previous:
                if previous["input_hash"] != fingerprint:
                    raise MachineError("PURCHASE_REQUEST_ID_CONFLICT")
                return self._public(previous)
            if db.execute("SELECT COUNT(*) FROM engine_task_purchases").fetchone()[0] >= 512:
                raise MachineError("PURCHASE_WORKSPACE_CAPACITY_REACHED")
            task = self.work.tasks._public(db, self.work.tasks._row(db, tid))
            if (
                type(raw["expected_revision"]) is not int
                or task["revision"] != raw["expected_revision"]
                or task["status"] != "READY_TO_PLAN"
            ):
                raise MachineError("FRESH_COMPLETE_TASK_REQUIRED")
            if service["kind"] != task["brief"]["kind"]:
                raise MachineError("SERVICE_TASK_KIND_MISMATCH")
            if service["price_cents"] > task["brief"]["budget"]["cents"]:
                raise MachineError("PURCHASE_EXCEEDS_TASK_BUDGET")
            if db.execute(
                "SELECT 1 FROM engine_task_purchases WHERE task_id=? AND status!='REVOKED'", (tid,)
            ).fetchone():
                raise MachineError("TASK_ALREADY_HAS_PURCHASE")
            cond = task["brief"]["constraints"]
            # PR2 has one actual deterministic worker, not fabricated AI services.
            if service["worker"] != "csv_cleanup_v1":
                raise MachineError("UNIMPLEMENTED_SERVICE_WORKER")
            clean_csv(raw["input"], cond["required_fields"], cond["output_format"])
            pid, at = "purchase-" + secrets.token_hex(12), int(self.work.clock())
            cents = service["price_cents"]
            plan = {
                "id": pid,
                "task_id": tid,
                "revision": task["revision"],
                "brief_hash": task["brief_hash"],
                "service": service,
                "merchant_id": provider.merchant,
                "currency": "USD",
                "amount": f"{cents // 100}.{cents % 100:02d}",
                "price_cents": cents,
                "expires_at": min(at + 900, task["brief"]["deadline_at"] or at + 900),
                "input": raw["input"],
                "input_hash": digest(raw["input"]),
                "columns": cond["required_fields"],
                "output_format": cond["output_format"],
                "environment": "PAYPAL_SANDBOX",
            }
            plan["hash"] = digest(plan)
            db.execute(
                "INSERT INTO engine_task_purchases VALUES (?,?,?,?,?,'PLANNED',?,?,NULL,NULL,NULL,NULL,?)",
                (
                    pid,
                    tid,
                    key,
                    fingerprint,
                    canonical(plan).decode(),
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    at,
                ),
            )
            row = self._row(db, pid)
            self._event(db, row, "TASK_PURCHASE_PLANNED")
            return self._public(row)

    def _fresh(self, db, row, approval_hash):
        plan, provider = self._plan(row), self._provider()
        task = self.work.tasks._public(db, self.work.tasks._row(db, row["task_id"]))
        if (
            approval_hash != plan["hash"]
            or plan["expires_at"] <= int(self.work.clock())
            or task["status"] != "READY_TO_PLAN"
            or task["revision"] != plan["revision"]
            or task["brief_hash"] != plan["brief_hash"]
            or provider.merchant != plan["merchant_id"]
            or provider.services.get(plan["service"]["sku"]) != plan["service"]
        ):
            raise MachineError("FROZEN_APPROVAL_OR_POLICY_INVALID")
        return plan

    def approve(self, pid, raw):
        require_keys(raw, {"plan_hash"}, "owner purchase approval")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, pid)
            if row["status"] != "PLANNED":
                if raw["plan_hash"] != self._plan(row)["hash"]:
                    raise MachineError("FROZEN_APPROVAL_OR_POLICY_INVALID")
                return self._public(row)
            plan = self._fresh(db, row, raw["plan_hash"])
            db.execute("UPDATE engine_task_purchases SET status='CREATING' WHERE id=?", (pid,))
            self._event(db, self._row(db, pid), "TASK_PURCHASE_CREATE_INTENT")
        # Exactly one outbound attempt; timeout remains reserved, never silently retried.
        try:
            order = self._provider().create(plan, row["create_key"])
            oid = bound_order(order, plan, plan["merchant_id"])
        except Exception:  # noqa: BLE001 - any adapter failure preserves the durable hold.
            with self.work.runtime.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE engine_task_purchases SET status='CREATE_UNKNOWN' WHERE id=? AND status='CREATING'",
                    (pid,),
                )
                self._event(db, self._row(db, pid), "TASK_PURCHASE_CREATE_UNKNOWN")
            return self.get(pid)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE engine_task_purchases SET status='AWAITING_APPROVAL',order_id=? WHERE id=? AND status='CREATING'",
                (oid, pid),
            )
            self._event(db, self._row(db, pid), "TASK_PURCHASE_PROVIDER_ORDER_CREATED")
        return self.get(pid)

    def capture(self, pid, raw):
        require_keys(raw, {"plan_hash"}, "owner sandbox capture")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, pid)
            plan = self._plan(row)
            if raw["plan_hash"] != plan["hash"]:
                raise MachineError("FROZEN_APPROVAL_OR_POLICY_INVALID")
            if row["status"] != "AWAITING_APPROVAL":
                return self._public(row)
            self._fresh(db, row, raw["plan_hash"])
        order = self._provider().read(row["order_id"])
        bound_order(order, plan, plan["merchant_id"], row["order_id"])
        if order["status"] == "COMPLETED":
            return self.reconcile(pid)
        if order["status"] != "APPROVED":
            raise MachineError("PAYPAL_BUYER_APPROVAL_REQUIRED")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._row(db, pid)
            if current["status"] != "AWAITING_APPROVAL":
                return self._public(current)
            self._fresh(db, current, raw["plan_hash"])
            db.execute("UPDATE engine_task_purchases SET status='CAPTURING' WHERE id=?", (pid,))
            self._event(db, self._row(db, pid), "TASK_PURCHASE_CAPTURE_INTENT")
        response_confirmed = True
        try:
            self._provider().capture(row["order_id"], row["capture_key"])
        except Exception:  # noqa: BLE001 - fresh GET resolves any lost capture response.
            response_confirmed = False
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE engine_task_purchases SET status='CAPTURE_UNKNOWN' WHERE id=? AND status='CAPTURING'",
                (pid,),
            )
            self._event(
                db,
                self._row(db, pid),
                "TASK_PURCHASE_CAPTURE_SUBMITTED",
                response_confirmed=response_confirmed,
            )
        return self.reconcile(pid)

    def reconcile(self, pid):
        with self.work.runtime.connect() as db:
            row = self._row(db, pid)
            if row["status"] in {"PAID", "DELIVERED", "PLANNED", "REVOKED", "CREATING", "CREATE_UNKNOWN"}:
                return self._public(row)
        plan = self._plan(row)
        try:
            order = self._provider().read(row["order_id"])
            capture_id = bound_order(order, plan, plan["merchant_id"], row["order_id"], paid=True)
        except Exception:  # noqa: BLE001 - no provider error can mint an entitlement.
            return self.get(pid)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._row(db, pid)
            if current["status"] not in {"PAID", "DELIVERED"}:
                db.execute(
                    "UPDATE engine_task_purchases SET status='PAID',capture_id=? WHERE id=?",
                    (capture_id, pid),
                )
                self._event(db, self._row(db, pid), "TASK_PURCHASE_PAYMENT_VERIFIED")
        return self.get(pid)

    def fulfill(self, pid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, pid)
            if row["status"] == "DELIVERED":
                return self._public(row)
            if row["status"] != "PAID" or not row["capture_id"]:
                raise MachineError("VERIFIED_PAYMENT_REQUIRED")
            plan = self._plan(row)
            result = clean_csv(plan["input"], plan["columns"], plan["output_format"])
            result.update({"purchase_id": pid, "input_hash": plan["input_hash"]})
            db.execute(
                "UPDATE engine_task_purchases SET status='DELIVERED',result=?,result_hash=? WHERE id=?",
                (canonical(result).decode(), digest(result), pid),
            )
            self._event(db, self._row(db, pid), "TASK_PURCHASE_DELIVERED")
            return self._public(self._row(db, pid))

    def delivery(self, pid):
        with self.work.runtime.connect() as db:
            row = self._row(db, pid)
            if row["status"] != "DELIVERED" or not row["capture_id"]:
                raise MachineError("DELIVERED_ENTITLEMENT_REQUIRED")
            result = json.loads(row["result"])
            if digest(result) != row["result_hash"]:
                raise MachineError("DELIVERY_INTEGRITY_FAILED")
            return result | {"result_hash": row["result_hash"]}
