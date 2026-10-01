"""Append-only local event hash chain. Integrity, not remote attestation."""

import json
import sqlite3

from .values import MachineError, canonical, digest


GENESIS = "0" * 64


def append_event(db: sqlite3.Connection, event_id: str, kind: str,
                 input_hash: str, output_hash: str, event: dict) -> str:
    prior = db.execute("SELECT input_hash,output_hash,event_hash FROM events WHERE event_id=?",
                       (event_id,)).fetchone()
    if prior:
        if prior["input_hash"] != input_hash or prior["output_hash"] != output_hash:
            raise MachineError("event id reused with different evidence")
        return prior["event_hash"]
    last = db.execute("SELECT ordinal,event_hash FROM events ORDER BY ordinal DESC LIMIT 1").fetchone()
    ordinal = last["ordinal"] + 1 if last else 1
    previous_hash = last["event_hash"] if last else GENESIS
    payload = {"ordinal": ordinal, "event_id": event_id, "kind": kind,
               "input_hash": input_hash, "output_hash": output_hash,
               "event": event, "previous_hash": previous_hash}
    event_hash = digest(payload)
    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
               (ordinal, event_id, kind, input_hash, output_hash,
                canonical(event).decode(), previous_hash, event_hash))
    return event_hash


def verify_journal(db: sqlite3.Connection) -> bool:
    previous = GENESIS
    expected_ordinal = 1
    for row in db.execute("SELECT * FROM events ORDER BY ordinal"):
        try:
            event = json.loads(row["event_json"])
            payload = {"ordinal": row["ordinal"], "event_id": row["event_id"],
                       "kind": row["kind"], "input_hash": row["input_hash"],
                       "output_hash": row["output_hash"], "event": event,
                       "previous_hash": row["previous_hash"]}
            if (row["ordinal"] != expected_ordinal or row["previous_hash"] != previous
                    or canonical(event).decode() != row["event_json"]
                    or digest(payload) != row["event_hash"]):
                return False
        except (ValueError, TypeError, MachineError):
            return False
        previous = row["event_hash"]
        expected_ordinal += 1
    return True
