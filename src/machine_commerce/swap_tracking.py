"""Durable, read-only tracking. Never signs, submits or replaces an order."""
import json
import re
import secrets
import sqlite3

from economic_machine.values import MachineError


class SwapTracker:
    def __init__(self, store, reconcile=None):
        self.store = store
        self.clock = store.router.clock
        self.reconcile = reconcile or (lambda sid: store.call('status', {'id': sid}))
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS swap_tracking (
                id TEXT PRIMARY KEY, due INTEGER NOT NULL, lease TEXT,
                lease_until INTEGER NOT NULL DEFAULT 0, checks INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
                checked INTEGER, result TEXT, error TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS swap_tracking_due ON swap_tracking(done,due,lease_until)')

    def connect(self):
        db = sqlite3.connect(self.store.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def adopt(self):
        # Includes orders submitted before this worker was installed or restarted.
        # The submitter commits its uncertain state before contacting the exchange.
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO swap_tracking(id,due)
                SELECT id,? FROM swaps WHERE state IN
                ('ORDER_SIGNATURE_REQUIRED','SUBMITTED','UNKNOWN_RECONCILE_ONLY','FILLED_FINALIZED')''', (int(self.clock()),))

    def watch(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{48}', sid):
            raise MachineError('SWAP_ID_REQUIRED')
        with self.connect() as db:
            swap = db.execute('SELECT state FROM swaps WHERE id=?', (sid,)).fetchone()
            if not swap:
                raise MachineError('SWAP_NOT_FOUND')
            row = db.execute('SELECT * FROM swap_tracking WHERE id=?', (sid,)).fetchone()
        result = json.loads(row['result']) if row and row['result'] else {
            'status': swap['state'], 'chain_verified': False, 'safe_to_retry': False}
        return {**result, 'tracking': {'active': not bool(row and row['done']),
            'checks': row['checks'] if row else 0, 'last_checked': row['checked'] if row else None,
            'next_check': row['due'] if row and not row['done'] else None,
            'source_error': row['error'] if row else None, 'authority': 'READ_ONLY'}}

    def tick(self, limit=2):
        if type(limit) is not int or not 1 <= limit <= 4:
            raise MachineError('BOUNDED_TRACKING_BATCH_REQUIRED')
        self.adopt()
        processed = 0
        for _ in range(limit):
            now, lease = int(self.clock()), secrets.token_hex(16)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('''SELECT * FROM swap_tracking WHERE done=0
                    AND due<=? AND lease_until<=? ORDER BY due,id LIMIT 1''', (now, now)).fetchone()
                if not row:
                    break
                db.execute('UPDATE swap_tracking SET lease=?,lease_until=? WHERE id=?',
                           (lease, now + 180, row['id']))
            try:
                result = self.reconcile(row['id'])
                done = (result.get('status') == 'FILLED_FINALIZED' and result.get('chain_verified') is True
                        or result.get('status') == 'EXPIRED_UNFILLED' and result.get('safe_to_retry') is True)
                delay = 30 if result.get('status') == 'AWAITING_FINALITY' else 10
                payload, error, errors = json.dumps(result), None, 0
            except Exception:
                # Retain the last verified observation and reservation. No raw
                # endpoint error, signature or secret is persisted or logged.
                done, payload, error = False, row['result'], 'TRACKING_SOURCE_UNAVAILABLE'
                errors = min(row['errors'] + 1, 6)
                delay = min(300, 5 * 2**errors)
            finished = int(self.clock())
            with self.connect() as db:
                db.execute('''UPDATE swap_tracking SET due=?,lease=NULL,lease_until=0,
                    checks=checks+1,errors=?,done=?,checked=?,result=?,error=?
                    WHERE id=? AND lease=?''',
                    (finished + delay, errors, int(done), finished, payload, error, row['id'], lease))
            processed += 1
        return processed
