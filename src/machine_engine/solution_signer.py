"""Separate owner signer with persisted nonce/cost reservations and no automatic retry.

The C++ worker never receives this object or its key. Signing is an owner-side
operation. Broadcast requires approval of the exact persisted transaction hash.
This module deliberately offers no wallet creation, central key storage or relay.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from economic_machine.values import MachineError, digest

from .solution_client import binding

METHODS = {
    keccak(text="commit(uint256,uint8,bytes32)")[:4]: ("commit", ["uint256", "uint8", "bytes32"]),
    keccak(text="reveal(uint256,uint8,uint32,bytes32)")[:4]: (
        "reveal",
        ["uint256", "uint8", "uint32", "bytes32"],
    ),
    keccak(text="claim(uint256,uint8)")[:4]: ("claim", ["uint256", "uint8"]),
    keccak(text="finalize(uint256)")[:4]: ("finalize", ["uint256"]),
}


def validate_intent(payload, reader, *, now=None):
    """Re-read chain/code/phase immediately before signing, never trust a UI flag."""
    now = int(time.time()) if now is None else now
    tx = payload.get("transaction", {})
    if set(tx) != {"to", "from", "chainId", "value", "data"} or tx["value"] != "0x0":
        raise MachineError("SOLUTION_SIGNER_ZERO_VALUE_INTENT_REQUIRED")
    if tx["chainId"] != reader.chain_id or tx["to"].lower() != reader.contract.lower():
        raise MachineError("SOLUTION_SIGNER_CHAIN_CONTRACT_MISMATCH")
    try:
        data = bytes.fromhex(tx["data"].removeprefix("0x"))
        method, types = METHODS[data[:4]]
        values = decode(types, data[4:])
        if encode(types, values) != data[4:]:
            raise ValueError("Noncanonical ABI")
    except (ValueError, KeyError, TypeError, OverflowError, DecodingError) as error:
        raise MachineError("SOLUTION_SIGNER_CALL_NOT_ALLOWED") from error
    round_id = values[0]
    binding(tx["to"], tx["chainId"], round_id, tx["from"])
    if method != "finalize" and not 0 <= values[1] < 16:
        raise MachineError("SOLUTION_SIGNER_PROBLEM_BOUND")
    if method == "reveal" and values[2] & 1:
        raise MachineError("SOLUTION_SIGNER_NONCANONICAL_BITS")
    snapshot = reader.snapshot(round_id=round_id, finalized=method == "claim")
    age = now - snapshot["anchor"]["timestamp"]
    if age < 0 or age > (3600 if method == "claim" else 120):
        raise MachineError("SOLUTION_SIGNER_STALE_STATE")
    anchor = snapshot["anchor"]
    miner = to_checksum_address(tx["from"])
    if method == "commit":
        if (
            snapshot["state"] != 2
            or snapshot["admission_paused"]
            or now + 60 >= snapshot["commit_end"]
            or values[2] == bytes(32)
        ):
            raise MachineError("SOLUTION_SIGNER_COMMIT_PHASE")
        _, ordinal, _ = reader.call(
            anchor,
            "submissions(uint256,uint8,address)",
            ["uint256", "uint8", "address"],
            [round_id, values[1], miner],
            ["bytes32", "uint256", "bool"],
        )
        if ordinal:
            raise MachineError("SOLUTION_SIGNER_COMMIT_ALREADY_PRESENT")
    elif method == "reveal":
        if snapshot["state"] != 2 or now < snapshot["commit_end"] or now + 60 >= snapshot["reveal_end"]:
            raise MachineError("SOLUTION_SIGNER_REVEAL_PHASE")
        fingerprint, ordinal, revealed = reader.call(
            anchor,
            "submissions(uint256,uint8,address)",
            ["uint256", "uint8", "address"],
            [round_id, values[1], miner],
            ["bytes32", "uint256", "bool"],
        )
        expected = keccak(
            encode(
                ["address", "uint256", "uint256", "uint8", "address", "uint32", "bytes32"],
                [reader.contract, reader.chain_id, round_id, values[1], miner, values[2], values[3]],
            )
        )
        if not ordinal or revealed or fingerprint != expected:
            raise MachineError("SOLUTION_SIGNER_CANONICAL_COMMIT_REQUIRED")
        cut, total = reader.call(
            anchor,
            "score(uint256,uint8,uint32)",
            ["uint256", "uint8", "uint32"],
            list(values[:3]),
            ["uint32", "uint32"],
        )
        if cut * 10000 < total * snapshot["threshold"]:
            raise MachineError("SOLUTION_SIGNER_SCORE_BELOW_THRESHOLD")
    elif method == "claim":
        winner = reader.winner(snapshot, values[1])
        if snapshot["state"] != 3 or winner["claimed"] or winner["miner"].lower() != miner.lower():
            raise MachineError("SOLUTION_SIGNER_UNCLAIMED_WINNER_REQUIRED")
        reward = reader.rewards(anchor, miner)
        expected = {"token": reward["token"], "round": round_id, "problem": values[1], "amount": str(10**18)}
        if payload.get("expected_reward") != expected:
            raise MachineError("SOLUTION_SIGNER_REWARD_RECEIPT_REQUIRED")
    elif snapshot["state"] != 2 or now < snapshot["reveal_end"]:
        raise MachineError("SOLUTION_SIGNER_FINALIZE_PHASE")
    reader.check_anchor(anchor)
    return method


class OwnerOutbox:
    """Bounded nonce progression on canonical receipts; uncertainty freezes admission."""

    def __init__(self, directory, reader, owner, *, per_tx_limit, daily_limit):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise MachineError("SOLUTION_SIGNER_PRIVATE_DIRECTORY")
        _, _, _, self.owner = binding(reader.contract, reader.chain_id, 1, owner)
        if (
            type(per_tx_limit) is not int
            or type(daily_limit) is not int
            or not 0 < per_tx_limit <= daily_limit < 2**63
        ):
            raise MachineError("SOLUTION_SIGNER_COST_LIMIT")
        self.reader = reader
        self.per_tx_limit, self.daily_limit = per_tx_limit, daily_limit
        path = self.directory / "signer.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        if path.stat().st_mode & 0o077:
            raise MachineError("SOLUTION_SIGNER_PRIVATE_DATABASE")
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS spend (day INTEGER PRIMARY KEY, reserved INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS signed (id TEXT PRIMARY KEY, nonce INTEGER UNIQUE NOT NULL,
              payload TEXT NOT NULL, raw BLOB NOT NULL, tx_hash TEXT UNIQUE NOT NULL, state TEXT NOT NULL, outcome TEXT);
        """)
        identity = json.dumps(
            [reader.chain_id, reader.contract, reader.expected_code, self.owner, per_tx_limit, daily_limit]
        )
        self.db.execute("INSERT OR IGNORE INTO config VALUES (1,?)", (identity,))
        if self.db.execute("SELECT identity FROM config").fetchone()[0] != identity:
            self.db.close()
            raise MachineError("SOLUTION_SIGNER_CONFIG_CHANGED")
        parent = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def close(self):
        self.db.close()

    def sign(self, payload, fees, private_key, *, now=None):
        now = int(time.time()) if now is None else now
        if (
            Account.from_key(private_key).address != self.owner
            or payload.get("transaction", {}).get("from", "").lower() != self.owner.lower()
        ):
            raise MachineError("SOLUTION_SIGNER_OWNER_MISMATCH")
        validate_intent(payload, self.reader, now=now)
        fields = {"nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas"}
        if (
            set(fees) != fields
            or any(type(v) is not int or v < 0 for v in fees.values())
            or fees["nonce"] >= 2**63
            or not 21000 <= fees["gas"] <= 4000000
            or not 0 <= fees["maxPriorityFeePerGas"] <= fees["maxFeePerGas"] < 2**128
        ):
            raise MachineError("SOLUTION_SIGNER_EIP1559_BOUNDS")
        cost = fees["gas"] * fees["maxFeePerGas"]
        if cost > self.per_tx_limit:
            raise MachineError("SOLUTION_SIGNER_TRANSACTION_COST")
        identifier = digest({"payload": payload, "fees": fees})
        day = now // 86400
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT tx_hash,state FROM signed WHERE id=?", (identifier,)).fetchone()
            if old:
                raise MachineError("SOLUTION_SIGNER_ALREADY_RESERVED")
            previous = self.db.execute("SELECT MAX(nonce) FROM signed").fetchone()[0]
            if previous is not None and fees["nonce"] != previous + 1:
                raise MachineError("SOLUTION_SIGNER_NONCE_PROGRESSION")
            pending = self.db.execute(
                "SELECT id,tx_hash,payload,state FROM signed WHERE state NOT IN ('CONFIRMED','REVERTED')"
            ).fetchall()
            if len(pending) >= 32:
                raise MachineError("SOLUTION_SIGNER_OUTSTANDING_LIMIT")
            working = self.reader.anchor(working=True) if pending else None
            for prior_id, prior_hash, prior_payload, state in pending:
                if state in {"SIGNED", "HELD", "ORPHANED"}:
                    raise MachineError("SOLUTION_SIGNER_UNSETTLED_NONCE")
                outcome = self.reader.outcome(prior_hash, json.loads(prior_payload))
                if (
                    outcome["state"] not in {"CONFIRMED", "REVERTED", "PENDING_FINALITY", "PENDING_REVERT"}
                    or outcome["block"] > working["number"]
                ):
                    raise MachineError("SOLUTION_SIGNER_UNSETTLED_NONCE")
                self.db.execute(
                    "UPDATE signed SET state=?,outcome=? WHERE id=?",
                    (outcome["state"], json.dumps(outcome), prior_id),
                )
            if working is not None:
                self.reader.check_anchor(working)
            latest_day = self.db.execute("SELECT MAX(day) FROM spend").fetchone()[0]
            if latest_day is not None and day < latest_day:
                raise MachineError("SOLUTION_SIGNER_CLOCK_ROLLBACK")
            row = self.db.execute("SELECT reserved FROM spend WHERE day=?", (day,)).fetchone()
            reserved = row[0] if row else 0
            if reserved + cost > self.daily_limit:
                raise MachineError("SOLUTION_SIGNER_DAILY_COST")
            tx = payload["transaction"]
            signed = Account.sign_transaction(
                {
                    "type": 2,
                    "chainId": self.reader.chain_id,
                    "to": self.reader.contract,
                    "value": 0,
                    "data": tx["data"],
                    **fees,
                },
                private_key,
            )
            tx_hash = "0x" + signed.hash.hex()
            self.db.execute(
                "INSERT INTO signed VALUES (?,?,?,?,?,?,NULL)",
                (
                    identifier,
                    fees["nonce"],
                    json.dumps(payload, sort_keys=True),
                    bytes(signed.raw_transaction),
                    tx_hash,
                    "SIGNED",
                ),
            )
            self.db.execute(
                "INSERT INTO spend VALUES (?,?) ON CONFLICT(day) DO UPDATE SET reserved=excluded.reserved",
                (day, reserved + cost),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        # Only the hash leaves this boundary. Reveal calldata/salts and signed bytes stay private.
        return {"id": identifier, "tx_hash": tx_hash, "state": "SIGNED", "reserved_max_cost_wei": str(cost)}

    def broadcast(self, identifier, send_raw, *, approved_hash):
        row = self.db.execute(
            "SELECT tx_hash,raw,payload,state FROM signed WHERE id=?", (identifier,)
        ).fetchone()
        if not row or approved_hash != row[0] or row[3] != "SIGNED":
            raise MachineError("SOLUTION_SIGNER_EXACT_HASH_APPROVAL_REQUIRED")
        validate_intent(json.loads(row[2]), self.reader)
        # Persist UNKNOWN *before* the network call. A crash/timeout can never enable a retry.
        changed = self.db.execute(
            "UPDATE signed SET state='UNKNOWN' WHERE id=? AND state='SIGNED'", (identifier,)
        ).rowcount
        if changed != 1:
            raise MachineError("SOLUTION_SIGNER_ALREADY_SUBMITTED")
        try:
            returned = send_raw("0x" + bytes(row[1]).hex())
        except Exception:  # noqa: BLE001 - Every transport failure leaves submission uncertain.
            return {"state": "UNKNOWN", "tx_hash": row[0], "automatic_retry": False}
        if returned != row[0]:
            raise MachineError("SOLUTION_SIGNER_RPC_HASH_MISMATCH")
        return {"state": "SUBMITTED_NOT_FINALIZED", "tx_hash": row[0], "automatic_retry": False}

    def reconcile(self, identifier):
        row = self.db.execute("SELECT tx_hash,payload,state FROM signed WHERE id=?", (identifier,)).fetchone()
        if not row or row[2] == "SIGNED":
            raise MachineError("SOLUTION_SIGNER_SUBMISSION_REQUIRED")
        try:
            outcome = self.reader.outcome(row[0], json.loads(row[1]))
        except MachineError as error:
            outcome = {"state": "HELD", "reason": str(error)}
        self.db.execute(
            "UPDATE signed SET state=?,outcome=? WHERE id=?",
            (outcome["state"], json.dumps(outcome), identifier),
        )
        return outcome
