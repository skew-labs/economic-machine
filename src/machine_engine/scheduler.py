"""Restart-safe read scheduling. No inference or order submission in this loop."""

import asyncio
import secrets

from economic_machine.values import MachineError, ident

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_sync_jobs (
 connection_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, interval_seconds INTEGER NOT NULL,
 next_run INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
 last_run INTEGER, last_status TEXT);
"""


class SyncScheduler:
    def __init__(self, workspace):
        self.work = workspace
        with workspace.runtime.connect() as db:
            db.executescript(SCHEMA)

    def configure(self, cid, *, enabled, interval_seconds):
        ident(cid, "connection ID")
        if (
            type(enabled) is not bool
            or type(interval_seconds) is not int
            or not 15 <= interval_seconds <= 3600
        ):
            raise MachineError("SYNC_INTERVAL_15_TO_3600_SECONDS_REQUIRED")
        at = int(self.work.clock())
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM engine_connections WHERE id=?", (cid,)).fetchone()
            if not row or row[0] == "DISCONNECTED":
                raise MachineError("ACTIVE_CONNECTION_REQUIRED")
            db.execute(
                "INSERT INTO engine_sync_jobs(connection_id,enabled,interval_seconds,next_run) VALUES (?,?,?,?) "
                "ON CONFLICT(connection_id) DO UPDATE SET enabled=excluded.enabled,interval_seconds=excluded.interval_seconds,"
                "next_run=excluded.next_run,failures=0,generation=generation+1",
                (cid, int(enabled), interval_seconds, at),
            )
            self.work.event(
                db,
                "SYNC_SCHEDULE_CHANGED",
                {"id": cid, "enabled": enabled, "interval_seconds": interval_seconds},
            )
        return {"id": cid, "enabled": enabled, "interval_seconds": interval_seconds}

    def status(self):
        with self.work.runtime.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT connection_id,enabled,interval_seconds,next_run,failures,"
                    "last_run,last_status,lease_until FROM engine_sync_jobs ORDER BY connection_id"
                )
            ]

    def run_once(self):
        at, token = int(self.work.clock()), secrets.token_hex(16)
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Disable disconnected sources even if the process restarted.
            db.execute(
                "UPDATE engine_sync_jobs SET enabled=0 WHERE connection_id IN "
                "(SELECT id FROM engine_connections WHERE status='DISCONNECTED')"
            )
            row = db.execute(
                "SELECT * FROM engine_sync_jobs WHERE enabled=1 AND next_run<=? AND lease_until<=? "
                "ORDER BY next_run,connection_id LIMIT 1",
                (at, at),
            ).fetchone()
            if row is None:
                return False
            cid, generation = row["connection_id"], row["generation"]
            # Bounded connector call <= 3 x 10 seconds. The lease lasts longer.
            db.execute(
                "UPDATE engine_sync_jobs SET lease_token=?,lease_until=? WHERE connection_id=?",
                (token, at + 120, cid),
            )
        status = "READ_FAILED"
        try:
            status = self.work.sync(cid)["status"]
        except Exception:  # noqa: BLE001 - one broken connector must not terminate scheduling.
            status = "READ_FAILED"
        finished = int(self.work.clock())
        with self.work.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM engine_sync_jobs WHERE connection_id=? AND lease_token=?", (cid, token)
            ).fetchone()
            if current:
                failures = 0 if status == "CONNECTED" else min(current["failures"] + 1, 10)
                delay = min(3600, current["interval_seconds"] * 2**failures)
                # A concurrent policy change owns its new next_run value.
                next_run = finished + delay if current["generation"] == generation else current["next_run"]
                db.execute(
                    "UPDATE engine_sync_jobs SET lease_token=NULL,lease_until=0,next_run=?,failures=?,"
                    "last_run=?,last_status=? WHERE connection_id=? AND lease_token=?",
                    (next_run, failures, finished, status, cid, token),
                )
        return True

    async def loop(self):
        while True:
            try:
                ran = await asyncio.to_thread(self.run_once)
            except Exception:  # noqa: BLE001 - recover database/read failures on the next bounded cycle.
                ran = False
            await asyncio.sleep(1 if ran else 5)
