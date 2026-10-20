"""One shared source throttle and immutable disjoint history assignments."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .source import SourceBackoff


class SourceGate:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, expires REAL);
            CREATE TABLE IF NOT EXISTS throttle(id INTEGER PRIMARY KEY, until REAL, recover REAL);
            INSERT OR IGNORE INTO throttle VALUES(1,0,0);
            CREATE TABLE IF NOT EXISTS waiting(id INTEGER PRIMARY KEY,ordinary_until REAL);
            INSERT OR IGNORE INTO waiting VALUES(1,0);''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if 'owner' not in {r[1] for r in self.db.execute('PRAGMA table_info(leases)')}:
                self.db.execute("ALTER TABLE leases ADD COLUMN owner TEXT NOT NULL DEFAULT 'ordinary'")

    def close(self):
        self.db.close()

    def acquire(self,owner='ordinary'):
        if owner not in ('ordinary','remote'):raise ValueError('SOURCE_LEASE_OWNER')
        now = self.clock();denied=None;token=None
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM leases WHERE expires<?', (now,))
            until, recover = self.db.execute('SELECT until,recover FROM throttle WHERE id=1').fetchone()
            if until > now:denied=SourceBackoff(until-now, -1)
            count = self.db.execute('SELECT count(*) FROM leases').fetchone()[0]
            # The four remote lanes cannot occupy both slots and indefinitely
            # starve the sole genesis writer in Amsterdam.
            remote = self.db.execute("SELECT count(*) FROM leases WHERE owner='remote'").fetchone()[0]
            ordinary_until=self.db.execute('SELECT ordinary_until FROM waiting WHERE id=1').fetchone()[0]
            busy=count >= (1 if recover > now else 2)
            if denied is None and (busy or (owner=='remote' and (remote>=1 or ordinary_until>now))):
                if owner=='ordinary':self.db.execute('UPDATE waiting SET ordinary_until=? WHERE id=1',(now+5,))
                denied=SourceBackoff(2,-1)
            if denied is None:
                token = uuid.uuid4().hex
                self.db.execute('INSERT INTO leases(id,expires,owner) VALUES(?,?,?)', (token, now+600,owner))
                if owner=='ordinary':self.db.execute('UPDATE waiting SET ordinary_until=0 WHERE id=1')
        # A denial must not roll back the waiting writer's durable turn.
        if denied is not None:raise denied
        return token

    def release(self, token, backoff=None):
        now = self.clock()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM leases WHERE id=?', (token,))
            if backoff is not None:
                self.db.execute('UPDATE throttle SET until=max(until,?),recover=max(recover,?) WHERE id=1',
                    (now+max(1,backoff),now+max(300,backoff)))


class CoordinatedSource:
    def __init__(self, source, path):
        self.source, self.path = source, path

    def head(self):
        return self.source.head()

    def fetch(self, start, end):
        gate = SourceGate(self.path)
        token = None
        try:
            token = gate.acquire()
            try:
                result = self.source.fetch(start,end)
            except SourceBackoff as error:
                gate.release(token,error.retry_after_seconds)
                token = None
                raise
            return result
        finally:
            if token is not None:
                gate.release(token)
            gate.close()


def load_plan(base):
    path = Path(base)/'evidence/parallel-plan.json'
    if not path.exists():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != path.with_suffix('.sha256').read_text().strip():
        raise ValueError('PARALLEL_PLAN_HASH')
    p = json.loads(raw)
    if (p.get('chain_id') != 42161 or p.get('monthly_limit_krw') != 1000000
        or p.get('parent_sha256') != hashlib.sha256((Path(base)/'evidence/grant.json').read_bytes()).hexdigest()):
        raise ValueError('PARALLEL_PLAN_AUTHORITY')
    ranges = p['ranges']
    cursor = 0
    for i, r in enumerate(ranges):
        if (r['lane'] != i or type(r['start']) is not int or type(r['end']) is not int
            or r['start'] != cursor or r['end'] < r['start']):
            raise ValueError('PARALLEL_RANGE_GAP_OR_OVERLAP')
        cursor = r['end']+1
    if len(ranges)!=5 or cursor != p['frozen_provider_head']+1 or ranges[0]['end']!=22207817:
        raise ValueError('PARALLEL_RANGE_SCOPE')
    return p
