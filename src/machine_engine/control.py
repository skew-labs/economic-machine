"""One durable policy/agent/run ledger inside the console's engine workspace.

Agents reuse existing connections, the native interpreter and the venue planner.
No second account store or signer is introduced. A shared USDT turnover envelope
is not a valuation of USD API bills, USDC payments or a wallet's cash balance.
"""

import hashlib
import json
import re
import secrets
from decimal import localcontext

from economic_machine.values import MachineError, canonical, decimal, decstr, digest, ident, require_keys

OPERATIONS = {"SYNC_CONNECTION", "NATIVE_CANDIDATE", "PLAN_VENUE_ORDER", "ECONOMIC_DECISION"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_control_policies (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL, spent_usdt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS engine_agents (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, policy_id TEXT NOT NULL REFERENCES engine_control_policies(id),
 status TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engine_agent_runs (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
 agent_id TEXT NOT NULL REFERENCES engine_agents(id), policy_id TEXT NOT NULL REFERENCES engine_control_policies(id),
 connection_id TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
 held_usdt TEXT NOT NULL, charged_usdt TEXT NOT NULL, order_id TEXT UNIQUE,
 result TEXT, error_code TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL, lease_until INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS engine_runs_agent ON engine_agent_runs(agent_id,created);
CREATE TABLE IF NOT EXISTS engine_agent_keys (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES engine_agents(id), token_hash TEXT UNIQUE NOT NULL,
 prefix TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL, revoked INTEGER);
"""


def unique_ids(value, label, *, maximum=32, empty=False):
    if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= maximum:
        raise MachineError("BOUNDED_" + label.upper() + "_REQUIRED")
    for entry in value:
        ident(entry, label)
    if len(set(value)) != len(value):
        raise MachineError("UNIQUE_" + label.upper() + "_REQUIRED")
    return value


class AgentControl:
    def __init__(self, workspace):
        self.work = workspace
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def policy(self, raw):
        require_keys(
            raw,
            {
                "name",
                "connection_ids",
                "venue_policy_ids",
                "allowed_operations",
                "turnover_limit_usdt",
                "max_order_usdt",
                "max_active_runs",
                "expires_at",
            },
            "shared agent policy",
        )
        ident(raw["name"], "shared policy name")
        unique_ids(raw["connection_ids"], "connections")
        unique_ids(raw["venue_policy_ids"], "venue policies", empty=True)
        operations = unique_ids(raw["allowed_operations"], "operations", maximum=4)
        if not set(operations) <= OPERATIONS:
            raise MachineError("SUPPORTED_AGENT_OPERATION_REQUIRED")
        budget, maximum = decimal(raw["turnover_limit_usdt"]), decimal(raw["max_order_usdt"])
        if not 0 <= maximum <= budget <= 100000000:
            raise MachineError("BOUNDED_SHARED_TURNOVER_REQUIRED")
        if "PLAN_VENUE_ORDER" in operations and (not maximum or not raw["venue_policy_ids"]):
            raise MachineError("VENUE_LIMITS_AND_POLICIES_REQUIRED")
        at = int(self.work.clock())
        if type(raw["expires_at"]) is not int or not at < raw["expires_at"] <= at + 86400 * 30:
            raise MachineError("BOUNDED_SHARED_POLICY_EXPIRY_REQUIRED")
        if type(raw["max_active_runs"]) is not int or not 1 <= raw["max_active_runs"] <= 8:
            raise MachineError("ACTIVE_RUN_LIMIT_1_TO_8_REQUIRED")
        pid = "shared-policy-" + secrets.token_hex(12)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM engine_control_policies").fetchone()[0] >= 128:
                raise MachineError("SHARED_POLICY_LIMIT")
            for cid in raw["connection_ids"]:
                row = db.execute("SELECT status FROM engine_connections WHERE id=?", (cid,)).fetchone()
                if not row or row[0] == "DISCONNECTED":
                    raise MachineError("OWNED_ACTIVE_CONNECTION_REQUIRED")
            for venue_id in raw["venue_policy_ids"]:
                row = db.execute(
                    "SELECT body,status FROM engine_trade_policies WHERE id=?", (venue_id,)
                ).fetchone()
                if (
                    not row
                    or row["status"] != "ACTIVE"
                    or json.loads(row["body"])["connection_id"] not in raw["connection_ids"]
                ):
                    raise MachineError("OWNED_BOUND_VENUE_POLICY_REQUIRED")
            db.execute(
                "INSERT INTO engine_control_policies VALUES (?,?,'ACTIVE','0')",
                (pid, canonical(raw).decode()),
            )
            self.work.event(db, "SHARED_AGENT_POLICY_CREATED", {"id": pid, "policy_hash": digest(raw)})
        return {
            "id": pid,
            "policy": raw,
            "policy_hash": digest(raw),
            "budget_basis": "GROSS_VENUE_TURNOVER_USDT",
        }

    def register(self, raw):
        require_keys(raw, {"name", "role", "policy_id", "connection_ids", "operations"}, "agent")
        ident(raw["name"], "agent name")
        if (
            not isinstance(raw["role"], str)
            or not 1 <= len(raw["role"]) <= 120
            or any(ord(c) < 32 for c in raw["role"])
        ):
            raise MachineError("PRINTABLE_AGENT_ROLE_REQUIRED")
        unique_ids(raw["connection_ids"], "connections")
        unique_ids(raw["operations"], "operations", maximum=4)
        aid = "agent-" + secrets.token_hex(12)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = self._policy(db, raw["policy_id"])
            if not set(raw["connection_ids"]) <= set(policy["connection_ids"]) or not set(
                raw["operations"]
            ) <= set(policy["allowed_operations"]):
                raise MachineError("AGENT_MUST_NARROW_SHARED_POLICY")
            if db.execute("SELECT COUNT(*) FROM engine_agents").fetchone()[0] >= 128:
                raise MachineError("AGENT_ROSTER_LIMIT")
            db.execute(
                "INSERT INTO engine_agents VALUES (?,?,?,'ACTIVE',?)",
                (aid, canonical(raw).decode(), raw["policy_id"], int(self.work.clock())),
            )
            self.work.event(
                db, "AGENT_REGISTERED", {"id": aid, "policy_id": raw["policy_id"], "agent_hash": digest(raw)}
            )
        return self.agent(aid)

    def _policy(self, db, pid, *, active=True):
        ident(pid, "shared policy ID")
        row = db.execute("SELECT * FROM engine_control_policies WHERE id=?", (pid,)).fetchone()
        if not row:
            raise MachineError("SHARED_POLICY_NOT_FOUND")
        body = json.loads(row["body"])
        if active and (row["status"] != "ACTIVE" or body["expires_at"] <= int(self.work.clock())):
            raise MachineError("ACTIVE_SHARED_POLICY_REQUIRED")
        return body

    def _agent(self, db, aid, *, active=True):
        ident(aid, "agent ID")
        row = db.execute("SELECT * FROM engine_agents WHERE id=?", (aid,)).fetchone()
        if not row:
            raise MachineError("AGENT_NOT_FOUND")
        if active and row["status"] != "ACTIVE":
            raise MachineError("ACTIVE_AGENT_REQUIRED")
        return row, json.loads(row["body"])

    def agent(self, aid, *, active=False):
        with self.work.runtime.connect() as db:
            row, body = self._agent(db, aid, active=active)
            policy = self._policy(db, row["policy_id"], active=active)
        return {"id": aid, "agent": body, "status": row["status"], "policy_expires_at": policy["expires_at"]}

    def pause_policy(self, pid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._policy(db, pid, active=False)
            db.execute(
                "UPDATE engine_control_policies SET status='PAUSED' WHERE id=? AND status='ACTIVE'", (pid,)
            )
            self.work.event(db, "SHARED_AGENT_POLICY_PAUSED", {"id": pid})
            status = db.execute("SELECT status FROM engine_control_policies WHERE id=?", (pid,)).fetchone()[0]
        return {
            "id": pid,
            "status": status,
            "existing_holds": "RETAINED_UNTIL_WITHDRAWAL_OR_VENUE_RECONCILIATION",
        }

    def pause_agent(self, aid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._agent(db, aid, active=False)
            db.execute("UPDATE engine_agents SET status='PAUSED' WHERE id=?", (aid,))
            self.work.event(db, "AGENT_PAUSED", {"id": aid})
        return self.agent(aid)

    def issue_key(self, aid, ttl_seconds):
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 86400:
            raise MachineError("KEY_LIFETIME_60_TO_86400_REQUIRED")
        key, kid, at = (
            "agent_" + secrets.token_urlsafe(32),
            "agent-key-" + secrets.token_hex(12),
            int(self.work.clock()),
        )
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, _ = self._agent(db, aid)
            policy = self._policy(db, row["policy_id"])
            expires = min(at + ttl_seconds, policy["expires_at"])
            if expires - at < 60:
                raise MachineError("POLICY_LIFETIME_TOO_SHORT_FOR_KEY")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM engine_agent_keys WHERE revoked IS NULL AND expires>?", (at,)
                ).fetchone()[0]
                >= 128
            ):
                raise MachineError("AGENT_KEY_LIMIT")
            db.execute(
                "INSERT INTO engine_agent_keys VALUES (?,?,?,?,?,?,NULL)",
                (kid, aid, hashlib.sha256(key.encode()).hexdigest(), key[:12], at, expires),
            )
            self.work.event(db, "LOCAL_AGENT_KEY_CREATED", {"id": kid, "agent_id": aid, "expires": expires})
        return {
            "secret": key,
            "shown_once": True,
            "key": {"id": kid, "engine_agent_id": aid, "expires": expires, "scopes": ["agents:run"]},
        }

    def resolve_key(self, token):
        if not isinstance(token, str) or not token.startswith("agent_") or not 40 <= len(token) <= 80:
            raise MachineError("AGENT_KEY_REQUIRED")
        with self.work.runtime.connect() as db:
            row = db.execute(
                "SELECT * FROM engine_agent_keys WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not row or row["revoked"] is not None or row["expires"] <= int(self.work.clock()):
                raise MachineError("AGENT_KEY_EXPIRED_OR_REVOKED")
        return row["agent_id"]

    def revoke_key(self, kid):
        ident(kid, "agent key ID")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM engine_agent_keys WHERE id=?", (kid,)).fetchone():
                raise MachineError("AGENT_KEY_NOT_FOUND")
            db.execute(
                "UPDATE engine_agent_keys SET revoked=COALESCE(revoked,?) WHERE id=?",
                (int(self.work.clock()), kid),
            )
            self.work.event(db, "LOCAL_AGENT_KEY_REVOKED", {"id": kid})
        return {"id": kid, "status": "REVOKED"}

    def _authority(self, db, aid, cid, operation):
        _, agent = self._agent(db, aid)
        policy = self._policy(db, agent["policy_id"])
        if (
            cid not in agent["connection_ids"]
            or cid not in policy["connection_ids"]
            or operation not in agent["operations"]
            or operation not in policy["allowed_operations"]
        ):
            raise MachineError("RUN_OUTSIDE_AGENT_POLICY")
        row = db.execute("SELECT status FROM engine_connections WHERE id=?", (cid,)).fetchone()
        if not row or row[0] == "DISCONNECTED":
            raise MachineError("OWNED_ACTIVE_CONNECTION_REQUIRED")
        return agent, policy

    @staticmethod
    def _held(db, pid):
        return sum(
            (
                decimal(r[0])
                for r in db.execute("SELECT held_usdt FROM engine_agent_runs WHERE policy_id=?", (pid,))
            ),
            decimal("0"),
        )

    @staticmethod
    def venue_request(rid):
        return "agent-run-" + rid.removeprefix("run-")

    def run(self, aid, raw):
        require_keys(raw, {"request_id", "operation", "connection_id", "payload"}, "agent run")
        ident(raw["request_id"], "agent request ID")
        ident(raw["connection_id"], "connection ID")
        if (
            not isinstance(raw["operation"], str)
            or raw["operation"] not in OPERATIONS
            or not isinstance(raw["payload"], dict)
        ):
            raise MachineError("BOUNDED_AGENT_TASK_REQUIRED")
        if len(canonical(raw)) > 32000:
            raise MachineError("AGENT_TASK_SIZE_LIMIT")
        fingerprint, rid, at = (
            digest({"agent_id": aid, "request": raw}),
            "run-" + secrets.token_hex(12),
            int(self.work.clock()),
        )
        held = decimal("0")
        with self.work.runtime.connect() as db, localcontext() as context:
            context.prec = 180
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM engine_agent_runs WHERE request_id=?", (raw["request_id"],)
            ).fetchone()
            if old:
                if old["input_hash"] != fingerprint:
                    raise MachineError("AGENT_RUN_IDEMPOTENCY_CONFLICT")
                return self._public_run(db, old)
            agent, policy = self._authority(db, aid, raw["connection_id"], raw["operation"])
            if (
                db.execute(
                    "SELECT COUNT(*) FROM engine_agent_runs WHERE status='RUNNING' AND policy_id=?",
                    (agent["policy_id"],),
                ).fetchone()[0]
                >= policy["max_active_runs"]
            ):
                raise MachineError("SHARED_ACTIVE_RUN_LIMIT")
            if raw["operation"] == "SYNC_CONNECTION":
                require_keys(raw["payload"], set(), "sync task")
            elif raw["operation"] == "NATIVE_CANDIDATE":
                self.work.native_program.compile(raw["payload"].get("program"))
            elif raw["operation"] == "ECONOMIC_DECISION":
                self.work.economics.validate_request(raw["payload"])
            else:
                require_keys(
                    raw["payload"],
                    {"policy_id", "symbol", "side", "quantity", "price", "time_in_force", "reduce_only"},
                    "agent venue plan",
                )
                if raw["payload"]["policy_id"] not in policy["venue_policy_ids"]:
                    raise MachineError("VENUE_POLICY_OUTSIDE_SHARED_RULES")
                venue = db.execute(
                    "SELECT body FROM engine_trade_policies WHERE id=?", (raw["payload"]["policy_id"],)
                ).fetchone()
                if not venue or json.loads(venue[0])["connection_id"] != raw["connection_id"]:
                    raise MachineError("VENUE_POLICY_CONNECTION_MISMATCH")
                held = decimal(raw["payload"]["quantity"]) * decimal(raw["payload"]["price"])
                spent = decimal(
                    db.execute(
                        "SELECT spent_usdt FROM engine_control_policies WHERE id=?", (agent["policy_id"],)
                    ).fetchone()[0]
                )
                if not 0 < held <= decimal(policy["max_order_usdt"]) or spent + self._held(
                    db, agent["policy_id"]
                ) + held > decimal(policy["turnover_limit_usdt"]):
                    raise MachineError("SHARED_AGENT_BUDGET_EXCEEDED")
            db.execute(
                "INSERT INTO engine_agent_runs VALUES (?,?,?,?,?,?,?,'RUNNING',?,'0',NULL,NULL,NULL,?,?,?)",
                (
                    rid,
                    raw["request_id"],
                    fingerprint,
                    aid,
                    agent["policy_id"],
                    raw["connection_id"],
                    raw["operation"],
                    decstr(held),
                    at,
                    at,
                    at + 120,
                ),
            )
            self.work.event(
                db,
                "AGENT_RUN_STARTED",
                {
                    "id": rid,
                    "agent_id": aid,
                    "policy_id": agent["policy_id"],
                    "connection_id": raw["connection_id"],
                    "operation": raw["operation"],
                    "input_hash": fingerprint,
                    "held_usdt": decstr(held),
                },
            )
        try:
            if raw["operation"] == "SYNC_CONNECTION":
                result = self.work.sync(raw["connection_id"])
                status, error = (
                    ("SUCCEEDED", None) if result["new_snapshot"] else ("FAILED", result["error_code"])
                )
            elif raw["operation"] == "NATIVE_CANDIDATE":
                result = self.work.native_program.evaluate(raw["payload"])
                result["input_assurance"] = "CALLER_SUPPLIED_NUMERIC_STATE_NOT_AN_ACCOUNT_ATTESTATION"
                status, error = ("SUCCEEDED", None) if result["accepted"] else ("ABSTAINED", result["code"])
            elif raw["operation"] == "ECONOMIC_DECISION":
                result = self.work.economics.evaluate(raw["payload"])
                status, error = ("SUCCEEDED", None) if result["computed"] else ("ABSTAINED", result["reason"])
            else:
                result = self.work.trading.plan(
                    raw["payload"] | {"request_id": self.venue_request(rid)}, control_run_id=rid
                )
                return self.get_run(aid, rid)
        except Exception as exc:  # noqa: BLE001 - no credential-bearing vendor exception is persisted.
            status, result = "FAILED", None
            error = (
                str(exc)
                if isinstance(exc, MachineError) and re.fullmatch(r"[A-Z_0-9]{1,80}", str(exc))
                else "AGENT_OPERATION_FAILED"
            )
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM engine_agent_runs WHERE id=?", (rid,)).fetchone()
            if row["status"] == "RUNNING":
                db.execute(
                    "UPDATE engine_agent_runs SET status=?,held_usdt='0',result=?,error_code=?,updated=? WHERE id=?",
                    (
                        status,
                        canonical(result).decode() if result is not None else None,
                        error,
                        int(self.work.clock()),
                        rid,
                    ),
                )
                self.work.event(
                    db,
                    "AGENT_RUN_COMPLETED",
                    {
                        "id": rid,
                        "agent_id": aid,
                        "status": status,
                        "result_hash": digest(result),
                        "error_code": error,
                    },
                )
        return self.get_run(aid, rid)

    def before_plan_commit(self, db, rid, notional):
        row = db.execute("SELECT * FROM engine_agent_runs WHERE id=?", (rid,)).fetchone()
        if (
            not row
            or row["status"] != "RUNNING"
            or row["lease_until"] <= int(self.work.clock())
            or decimal(row["held_usdt"]) != decimal(notional)
        ):
            raise MachineError("ACTIVE_AGENT_PLAN_RESERVATION_REQUIRED")
        self._authority(db, row["agent_id"], row["connection_id"], row["operation"])

    def attach_plan(self, db, rid, oid):
        db.execute(
            "UPDATE engine_agent_runs SET order_id=?,status='AWAITING_APPROVAL',updated=? WHERE id=?",
            (oid, int(self.work.clock()), rid),
        )
        self.work.event(db, "AGENT_VENUE_PLAN_ATTACHED", {"run_id": rid, "order_id": oid})

    def authorize_order(self, db, oid):
        row = db.execute("SELECT * FROM engine_agent_runs WHERE order_id=?", (oid,)).fetchone()
        if row:
            self._authority(db, row["agent_id"], row["connection_id"], row["operation"])
            if row["status"] not in {"AWAITING_APPROVAL", "APPROVED"} or decimal(row["held_usdt"]) <= 0:
                raise MachineError("ACTIVE_SHARED_AGENT_HOLD_REQUIRED")

    def observe_order(self, db, oid, status, quote="0"):
        from .trading import TERMINAL

        row = db.execute("SELECT * FROM engine_agent_runs WHERE order_id=?", (oid,)).fetchone()
        if not row:
            return
        with localcontext() as context:
            context.prec = 180
            actual = decimal(quote)
            held, charged = decimal(row["held_usdt"]), decimal(row["charged_usdt"])
            if status in TERMINAL:
                if charged != 0 or held == 0:
                    return
                policy_row = db.execute(
                    "SELECT * FROM engine_control_policies WHERE id=?", (row["policy_id"],)
                ).fetchone()
                spent = decimal(policy_row["spent_usdt"]) + actual
                db.execute(
                    "UPDATE engine_control_policies SET spent_usdt=? WHERE id=?",
                    (decstr(spent), row["policy_id"]),
                )
                held, charged = decimal("0"), actual
            elif actual > held:
                held = actual
            db.execute(
                "UPDATE engine_agent_runs SET status=?,held_usdt=?,charged_usdt=?,updated=? WHERE id=?",
                (status, decstr(held), decstr(charged), int(self.work.clock()), row["id"]),
            )
            policy = self._policy(db, row["policy_id"], active=False)
            spent = decimal(
                db.execute(
                    "SELECT spent_usdt FROM engine_control_policies WHERE id=?", (row["policy_id"],)
                ).fetchone()[0]
            )
            if spent + self._held(db, row["policy_id"]) > decimal(policy["turnover_limit_usdt"]):
                db.execute(
                    "UPDATE engine_control_policies SET status='BREACHED' WHERE id=?", (row["policy_id"],)
                )
            self.work.event(
                db,
                "AGENT_VENUE_STATE_OBSERVED",
                {
                    "run_id": row["id"],
                    "order_id": oid,
                    "status": status,
                    "held_usdt": decstr(held),
                    "charged_usdt": decstr(charged),
                },
            )

    def withdraw(self, aid, rid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._run(db, aid, rid)
            order = db.execute("SELECT * FROM engine_trade_orders WHERE id=?", (row["order_id"],)).fetchone()
            if (
                not order
                or order["status"] not in {"AWAITING_APPROVAL", "APPROVED"}
                or order["reserved"] != "0"
            ):
                raise MachineError("ONLY_UNSENT_AGENT_PLAN_CAN_BE_WITHDRAWN")
            db.execute(
                "UPDATE engine_trade_orders SET status='WITHDRAWN_UNSENT',updated=? WHERE id=?",
                (int(self.work.clock()), order["id"]),
            )
            db.execute(
                "UPDATE engine_agent_runs SET status='WITHDRAWN_UNSENT',held_usdt='0',updated=? WHERE id=?",
                (int(self.work.clock()), rid),
            )
            self.work.event(db, "AGENT_UNSENT_PLAN_WITHDRAWN", {"run_id": rid, "order_id": order["id"]})
        return self.get_run(aid, rid)

    def recover_once(self):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM engine_agent_runs WHERE status='RUNNING' AND lease_until<=? LIMIT 32",
                (int(self.work.clock()),),
            ).fetchall()
            for row in rows:
                # A venue plan and its link commit in one SQLite transaction.
                # An unlinked expired run can never dispatch and is not retried.
                db.execute(
                    "UPDATE engine_agent_runs SET status='INTERRUPTED',held_usdt='0',error_code='RUN_LEASE_EXPIRED',updated=? WHERE id=?",
                    (int(self.work.clock()), row["id"]),
                )
                self.work.event(
                    db, "AGENT_RUN_INTERRUPTED", {"run_id": row["id"], "operation": row["operation"]}
                )
        return len(rows)

    def _run(self, db, aid, rid):
        ident(rid, "run ID")
        row = db.execute("SELECT * FROM engine_agent_runs WHERE id=? AND agent_id=?", (rid, aid)).fetchone()
        if not row:
            raise MachineError("OWNED_AGENT_RUN_REQUIRED")
        return row

    def _public_run(self, db, row):
        result = {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "policy_id": row["policy_id"],
            "connection_id": row["connection_id"],
            "operation": row["operation"],
            "status": row["status"],
            "held_usdt": row["held_usdt"],
            "charged_usdt": row["charged_usdt"],
            "order_id": row["order_id"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error_code": row["error_code"],
            "created_at": row["created"],
            "updated_at": row["updated"],
            "input_hash": row["input_hash"],
            "dispatch_authority": "OWNER_PER_ORDER_ONLY",
        }
        if row["order_id"]:
            order = self.work.trading.public_order(db, self.work.trading.get(db, row["order_id"]))
            result["result"], result["status"] = order, order["status"]
        return result

    def get_run(self, aid, rid):
        with self.work.runtime.connect() as db:
            return self._public_run(db, self._run(db, aid, rid))

    def status(self):
        with self.work.runtime.connect() as db, localcontext() as context:
            context.prec = 180
            policies = []
            for row in db.execute("SELECT * FROM engine_control_policies ORDER BY rowid"):
                body = json.loads(row["body"])
                held, spent = self._held(db, row["id"]), decimal(row["spent_usdt"])
                policies.append(
                    {
                        "id": row["id"],
                        "policy": body,
                        "status": "EXPIRED"
                        if row["status"] == "ACTIVE" and body["expires_at"] <= int(self.work.clock())
                        else row["status"],
                        "spent_usdt": decstr(spent),
                        "held_usdt": decstr(held),
                        "remaining_usdt": decstr(
                            max(decimal("0"), decimal(body["turnover_limit_usdt"]) - held - spent)
                        ),
                        "budget_basis": "GROSS_VENUE_TURNOVER_USDT_NOT_WALLET_CASH",
                    }
                )
            agents = [
                {"id": r["id"], "agent": json.loads(r["body"]), "status": r["status"]}
                for r in db.execute("SELECT * FROM engine_agents ORDER BY created,id")
            ]
            runs = [
                self._public_run(db, r)
                for r in db.execute("SELECT * FROM engine_agent_runs ORDER BY rowid DESC LIMIT 100")
            ]
            keys = [
                {
                    "id": r["id"],
                    "agent_id": r["agent_id"],
                    "prefix": r["prefix"],
                    "expires": r["expires"],
                    "status": "REVOKED"
                    if r["revoked"] is not None
                    else "EXPIRED"
                    if r["expires"] <= int(self.work.clock())
                    else "ACTIVE",
                }
                for r in db.execute("SELECT * FROM engine_agent_keys ORDER BY created,id")
            ]
        return {
            "policies": policies,
            "agents": agents,
            "runs": runs,
            "local_keys": keys,
            "workspace_store": "SAME_ENGINE_DATABASE",
            "payments_scope": "EXTERNAL_SERVICES_ONLY",
            "budget_units": "USDT_TURNOVER_SEPARATE_FROM_USD_API_COST_AND_USDC_PAYMENTS",
        }
