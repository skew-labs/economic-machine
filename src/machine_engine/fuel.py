"""Wallet-owned fuel policies and resumption barriers in the engine journal.

One USDC envelope covers the gas-acquisition principal and the intended purchase.
This is not a USD valuation of unrelated USDT turnover or API billing ledgers.
"""
import json
import re
import secrets

from economic_machine.values import MachineError, canonical, digest, ident, require_keys
from machine_commerce.datapass import address
from machine_commerce.gas_portal import SwapStore
from machine_commerce.gas_router import CHAIN, USDC, GasRouter

# JSON integer precision and SQLite accounting representation, not a USD policy.
MAX_LEDGER_ATOMS = (1 << 53) - 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_fuel_policies (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL, spent_atoms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS engine_fuel_requests (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
 policy_id TEXT NOT NULL, body TEXT NOT NULL, swap_id TEXT UNIQUE NOT NULL,
 status TEXT NOT NULL, held_atoms INTEGER NOT NULL, charged_atoms INTEGER NOT NULL,
 receipt TEXT, created INTEGER NOT NULL);
"""


def atoms(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise MachineError("BOUNDED_INTEGER_ATOMS_REQUIRED")
    return value


class Fuel:
    def __init__(self, workspace, router=None):
        self.work = workspace
        self.router = router or GasRouter(clock=workspace.clock)
        self.swaps = SwapStore(workspace.runtime.db_path, self.router, self._guard)
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def _guard(self, db, intent):
        # SwapStore uses a tuple connection; use a cursor-local row factory here.
        import sqlite3
        cursor = db.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute("SELECT body,policy_id FROM engine_fuel_requests WHERE swap_id=?", (intent["id"],)).fetchone()
        if not row:
            raise MachineError("BOUND_FUEL_REQUEST_REQUIRED")
        # The helper queries must also preserve named-row semantics.
        previous = db.row_factory
        db.row_factory = sqlite3.Row
        try:
            _, policy = self._policy(db, row["policy_id"])
            self._agent_wallet(db, json.loads(row["body"])["agent_id"], policy["owner"])
        finally:
            db.row_factory = previous

    def policy(self, raw):
        require_keys(raw, {"owner", "budget_atoms", "max_fuel_atoms", "max_purchase_atoms", "agent_ids", "expires_at"}, "fuel policy")
        owner = address(raw["owner"])
        budget = atoms(raw["budget_atoms"], MAX_LEDGER_ATOMS)
        maximum = atoms(raw["max_fuel_atoms"], budget)
        purchase = atoms(raw["max_purchase_atoms"], budget)
        if maximum < 1 or maximum + purchase > budget:
            raise MachineError("FUEL_AND_PURCHASE_MUST_FIT_SHARED_BUDGET")
        if type(raw["expires_at"]) is not int or not self.work.clock() + 600 < raw["expires_at"] <= self.work.clock() + 86400:
            raise MachineError("FUEL_POLICY_LIFETIME_REQUIRED")
        agents = raw["agent_ids"]
        if not isinstance(agents, list) or not 1 <= len(agents) <= 16 or any(not isinstance(x, str) for x in agents) or len(set(agents)) != len(agents):
            raise MachineError("BOUNDED_UNIQUE_AGENTS_REQUIRED")
        body = {**raw, "owner": owner, "chain_id": CHAIN, "asset": USDC}
        pid = "fuel-policy-" + secrets.token_hex(12)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM engine_fuel_policies").fetchone()[0] >= 128:
                raise MachineError("FUEL_POLICY_CAPACITY")
            for aid in agents:
                self._agent_wallet(db, aid, owner)
            db.execute("INSERT INTO engine_fuel_policies VALUES (?,?,'ACTIVE',0)", (pid, canonical(body).decode()))
            self.work.event(db, "FUEL_POLICY_CREATED", {"id": pid, "policy_hash": digest(body)})
        return {"id": pid, "policy": body, "approval": "WALLET_SIGNATURE_PER_SWAP"}

    def _agent_wallet(self, db, aid, owner):
        row, agent = self.work.control._agent(db, aid)
        self.work.control._policy(db, row["policy_id"])
        for cid in agent["connection_ids"]:
            connection = db.execute("SELECT body,status FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if connection and connection["status"] != "DISCONNECTED":
                body = json.loads(connection["body"])
                if body["profile"] == "arbitrum-one-wallet" and body["config"]["address"].lower() == owner.lower():
                    return
        raise MachineError("AGENT_CONNECTED_ARBITRUM_WALLET_REQUIRED")

    def _policy(self, db, pid, *, active=True):
        row = db.execute("SELECT * FROM engine_fuel_policies WHERE id=?", (pid,)).fetchone()
        if not row:
            raise MachineError("FUEL_POLICY_NOT_FOUND")
        body = json.loads(row["body"])
        if active and (row["status"] != "ACTIVE" or body["expires_at"] <= self.work.clock()):
            raise MachineError("ACTIVE_FUEL_POLICY_REQUIRED")
        return row, body

    def pause(self, pid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._policy(db, pid, active=False)
            db.execute("UPDATE engine_fuel_policies SET status='PAUSED' WHERE id=?", (pid,))
            self.work.event(db, "FUEL_POLICY_PAUSED", {"id": pid})
        return {"status": "PAUSED", "existing_reservations": "RETAINED"}

    def propose(self, raw):
        require_keys(raw, {"request_id", "policy_id", "agent_id", "parent_action_hash", "purchase_atoms", "fuel_atoms", "required_eth_wei"}, "fuel proposal")
        for key in ("request_id", "policy_id", "agent_id"):
            ident(raw[key], key)
        if not isinstance(raw["parent_action_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["parent_action_hash"]):
            raise MachineError("PARENT_ACTION_HASH_REQUIRED")
        atoms(raw["purchase_atoms"], MAX_LEDGER_ATOMS)
        atoms(raw["fuel_atoms"], MAX_LEDGER_ATOMS)
        if not 0 < atoms(raw["required_eth_wei"], 10**16):
            raise MachineError("BOUNDED_PARENT_GAS_REQUIREMENT")
        with self.work.runtime.connect() as db:
            old = db.execute("SELECT id,request_hash FROM engine_fuel_requests WHERE request_id=?", (raw["request_id"],)).fetchone()
            if old:
                if old["request_hash"] != digest(raw):
                    raise MachineError("FUEL_IDEMPOTENCY_CONFLICT")
                return self.get(old["id"])
            _, policy = self._policy(db, raw["policy_id"])
            self._agent_wallet(db, raw["agent_id"], policy["owner"])
            if raw["agent_id"] not in policy["agent_ids"]:
                raise MachineError("FUEL_AGENT_NOT_AUTHORIZED")
            if not 1 <= raw["fuel_atoms"] <= policy["max_fuel_atoms"] or raw["purchase_atoms"] > policy["max_purchase_atoms"]:
                raise MachineError("FUEL_POLICY_AMOUNT_EXCEEDED")
        # No lock is held over external calls. Every authority/budget is rechecked below.
        prepared = self.router.prepare(policy["owner"], raw["fuel_atoms"])
        if prepared["valid_to"] > policy["expires_at"]:
            raise MachineError("SWAP_OUTLIVES_POLICY")
        if min(int(r["eth_wei"]) for r in prepared["observations"]) >= raw["required_eth_wei"]:
            return {"status": "ALREADY_FUNDED_REPLAN_PARENT", "parent_action_hash": raw["parent_action_hash"],
                    "swap_requested": False, "execution_authority": "NONE"}
        if int(prepared["preview_minimum_wei"]) < raw["required_eth_wei"]:
            raise MachineError("FUEL_QUOTE_DOES_NOT_COVER_PARENT_GAS")
        fid, sid = "fuel-"+secrets.token_hex(12), secrets.token_hex(24)
        prepared["id"] = sid
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, fresh = self._policy(db, raw["policy_id"])
            self._agent_wallet(db, raw["agent_id"], fresh["owner"])
            old = db.execute("SELECT id,request_hash FROM engine_fuel_requests WHERE request_id=?", (raw["request_id"],)).fetchone()
            if old:
                if old["request_hash"] != digest(raw):
                    raise MachineError("FUEL_IDEMPOTENCY_CONFLICT")
                return {"id": old["id"], "status": "EXISTING_REQUEST", "idempotent": True}
            held = db.execute("SELECT COALESCE(SUM(held_atoms),0) FROM engine_fuel_requests WHERE policy_id=?", (raw["policy_id"],)).fetchone()[0]
            total = raw["fuel_atoms"] + raw["purchase_atoms"]
            if row["spent_atoms"] + held + total > fresh["budget_atoms"]:
                raise MachineError("SHARED_FUEL_BUDGET_EXHAUSTED")
            wallet_held = db.execute("""SELECT COALESCE(SUM(r.held_atoms),0) FROM engine_fuel_requests r
                JOIN engine_fuel_policies p ON p.id=r.policy_id
                WHERE lower(json_extract(p.body,'$.owner'))=lower(?)""", (fresh["owner"],)).fetchone()[0]
            if wallet_held + total > min(int(x["usdc_atoms"]) for x in prepared["observations"]):
                raise MachineError("WALLET_USDC_ALREADY_RESERVED_OR_INSUFFICIENT")
            # One wallet cannot have two unconsumed permits/orders fighting over its nonce.
            if db.execute("SELECT 1 FROM swaps WHERE lower(owner)=lower(?) AND state NOT IN ('FILLED_FINALIZED','EXPIRED_UNFILLED')", (fresh["owner"],)).fetchone():
                raise MachineError("WALLET_FUEL_ALREADY_PENDING")
            db.execute("INSERT INTO swaps VALUES (?,?,?,?,?)", (sid, fresh["owner"], prepared["status"], json.dumps(prepared), int(self.work.clock())))
            db.execute("INSERT INTO engine_fuel_requests VALUES (?,?,?,?,?,?,'AWAITING_WALLET',?,0,NULL,?)",
                (fid, raw["request_id"], digest(raw), raw["policy_id"], canonical(raw).decode(), sid, total, int(self.work.clock())))
            self.work.event(db, "FUEL_AND_PURCHASE_RESERVED", {"id": fid, "atoms": total, "parent": raw["parent_action_hash"]})
        return self.get(fid)

    def get(self, fid):
        with self.work.runtime.connect() as db:
            row = db.execute("SELECT * FROM engine_fuel_requests WHERE id=?", (fid,)).fetchone()
            if not row:
                raise MachineError("FUEL_REQUEST_NOT_FOUND")
            swap = db.execute("SELECT payload,state FROM swaps WHERE id=?", (row["swap_id"],)).fetchone()
            return {"id": fid, "status": row["status"], "request": json.loads(row["body"]),
                    "held_atoms": row["held_atoms"], "charged_atoms": row["charged_atoms"],
                    "swap": {**json.loads(swap[0]), "status": swap[1]}, "execution_authority": "NONE"}

    def wallet_step(self, fid, step, signature):
        request = self.get(fid)
        with self.work.runtime.connect() as db:
            _, policy = self._policy(db, request["request"]["policy_id"])
            self._agent_wallet(db, request["request"]["agent_id"], policy["owner"])
        if step not in {"order", "submit"}:
            raise MachineError("FUEL_WALLET_STEP_REQUIRED")
        # Same implementation as the manual Fuel screen; signatures remain bounded.
        return self.swaps.call(step, {"id": request["swap"]["id"], "signature": signature})

    def reconcile(self, fid):
        current = self.get(fid)
        result = self.swaps.call("status", {"id": current["swap"]["id"]})
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM engine_fuel_requests WHERE id=?", (fid,)).fetchone()
            raw = json.loads(row["body"])
            if row["status"] == "AWAITING_WALLET" and result["status"] == "FILLED_FINALIZED" and result.get("chain_verified") is True:
                charged = raw["fuel_atoms"]
                db.execute("UPDATE engine_fuel_policies SET spent_atoms=spent_atoms+? WHERE id=?", (charged, row["policy_id"]))
                db.execute("UPDATE engine_fuel_requests SET status='PARENT_REPLAN_REQUIRED',held_atoms=?,charged_atoms=?,receipt=? WHERE id=?",
                    (raw["purchase_atoms"], charged, canonical(result).decode(), fid))
                self.work.event(db, "FUEL_SETTLEMENT_VERIFIED", {"id": fid, "receipt_hash": digest(result), "spent_atoms": charged})
            elif row["status"] == "AWAITING_WALLET" and result["status"] == "EXPIRED_UNFILLED" and result.get("safe_to_retry"):
                db.execute("UPDATE engine_fuel_requests SET status='EXPIRED_UNFILLED',held_atoms=0,receipt=? WHERE id=?", (canonical(result).decode(), fid))
                self.work.event(db, "FUEL_EXPIRED_WITHOUT_FILL", {"id": fid})
        return {**self.get(fid), "settlement": result}

    def resume_review(self, fid, parent_hash):
        current = self.get(fid)
        if current["status"] != "PARENT_REPLAN_REQUIRED" or parent_hash != current["request"]["parent_action_hash"]:
            raise MachineError("VERIFIED_FUEL_AND_MATCHING_PARENT_REQUIRED")
        readings = self.router.balances(current["swap"]["owner"])
        if min(int(r["eth_wei"]) for r in readings) < current["request"]["required_eth_wei"]:
            raise MachineError("NATIVE_BALANCE_CHANGED_REPLAN_REQUIRED")
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, policy = self._policy(db, current["request"]["policy_id"])
            self._agent_wallet(db, current["request"]["agent_id"], policy["owner"])
            if db.execute("UPDATE engine_fuel_requests SET status='PARENT_REVIEW_READY' WHERE id=? AND status='PARENT_REPLAN_REQUIRED'", (fid,)).rowcount != 1:
                raise MachineError("PARENT_REVIEW_ALREADY_ISSUED")
            self.work.event(db, "FUEL_PARENT_REVIEW_READY", {"id": fid, "parent_action_hash": parent_hash})
        return {"status": "PARENT_REVIEW_READY", "parent_action_hash": parent_hash,
                "parent_reservation_atoms": current["request"]["purchase_atoms"],
                "requires_fresh_parent_simulation": True, "requires_parent_approval": True,
                "execution_authority": "NONE"}

    def cancel_parent(self, fid):
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM engine_fuel_requests WHERE id=?", (fid,)).fetchone()
            if not row or row["status"] != "PARENT_REPLAN_REQUIRED":
                raise MachineError("UNEXECUTED_PARENT_REVIEW_REQUIRED")
            db.execute("UPDATE engine_fuel_requests SET status='PARENT_CANCELED',held_atoms=0 WHERE id=?", (fid,))
            self.work.event(db, "FUEL_PARENT_RESERVATION_CANCELED", {"id": fid, "spent_fuel_retained": row["charged_atoms"]})
        return self.get(fid)
