"""One bounded mailbox per agent, with durable inference admission.

This is a single-owner service. Rows isolate logical agents, not hostile OS
tenants. A dedicated OS/container boundary is required per unrelated owner.
"""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

KINDS = ("triage", "note", "remind", "signal")
IDENT = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


class Rejected(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def ident(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise Rejected("invalid identifier")
    return value


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents(
                  id TEXT PRIMARY KEY, venue TEXT NOT NULL, tools TEXT NOT NULL,
                  last_served INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(
                  id INTEGER PRIMARY KEY, agent TEXT NOT NULL REFERENCES agents(id),
                  key TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
                  payload TEXT NOT NULL, created INTEGER NOT NULL, due INTEGER NOT NULL,
                  expires INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED',
                  attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
                  UNIQUE(agent,key));
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,due,agent);
                CREATE TABLE IF NOT EXISTS reservations(
                  day INTEGER NOT NULL, agent TEXT NOT NULL, count INTEGER NOT NULL,
                  PRIMARY KEY(day,agent));
                CREATE TABLE IF NOT EXISTS cursors(
                  agent TEXT PRIMARY KEY, sequence INTEGER NOT NULL, observed INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('dispatch','0');
            ''')
            if 'finished_ns' not in {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN finished_ns INTEGER')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def provision(self, agents):
        """Operator-only call. Model-facing API intentionally cannot reach it."""
        if not 1 <= len(agents) <= 100 or len({a['id'] for a in agents}) != len(agents):
            raise Rejected("one to 100 unique agents required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for a in agents:
                ident(a['id']); ident(a['venue'])
                if not a['tools'] or not set(a['tools']) <= set(KINDS):
                    raise Rejected("unsupported tools")
                old = db.execute("SELECT * FROM agents WHERE id=?", (a['id'],)).fetchone()
                if old and (old['venue'] != a['venue'] or json.loads(old['tools']) != a['tools']):
                    raise Rejected("grant changes require a new agent identity")
                db.execute("INSERT OR IGNORE INTO agents(id,venue,tools) VALUES(?,?,?)",
                           (a['id'], a['venue'], encode(a['tools'])))
            if db.execute("SELECT count(*) FROM agents").fetchone()[0] > 100:
                raise Rejected("agent capacity")

    def submit(self, task, now=None):
        now = int(time.time()) if now is None else now
        if not isinstance(task, dict) or set(task) != {'agent', 'key', 'kind', 'payload', 'due', 'expires'}:
            raise Rejected("task fields")
        ident(task['agent']); ident(task['key'])
        kind, payload = task['kind'], task['payload']
        if kind not in KINDS or not isinstance(payload, dict):
            raise Rejected("task kind/payload")
        if type(task['due']) is not int or type(task['expires']) is not int:
            raise Rejected("integer timestamps required")
        if not 0 < task['due'] < task['expires'] or task['expires']-task['due']>86400:
            raise Rejected("task timestamp bounds")
        if kind == 'signal':
            if set(payload) != {'venue', 'sequence', 'observed', 'price', 'reference', 'threshold_bps'}:
                raise Rejected("signal fields")
            ident(payload['venue'])
            for k in ('sequence', 'observed', 'price', 'reference', 'threshold_bps'):
                if type(payload[k]) is not int or not 0 < payload[k] <= 2**63-1:
                    raise Rejected("positive bounded signal integers required")
            if payload['threshold_bps'] > 10000:
                raise Rejected("signal threshold")
            if task['expires'] > payload['observed'] + 5:
                raise Rejected("signal must expire within five seconds")
        elif set(payload) != {'text'} or not isinstance(payload['text'], str) or not 1 <= len(payload['text']) <= 512:
            raise Rejected("text must be one to 512 characters")
        raw = encode(task)
        if len(raw.encode()) > 4096:
            raise Rejected("task size")
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            a = db.execute("SELECT * FROM agents WHERE id=?", (task['agent'],)).fetchone()
            if not a or kind not in json.loads(a['tools']):
                raise Rejected("tool outside agent grant")
            prior = db.execute("SELECT id,fingerprint FROM jobs WHERE agent=? AND key=?",
                               (task['agent'], task['key'])).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise Rejected("idempotency key conflicts with previous payload")
                return prior['id']
            # Late retries resolve the original outcome before new-work expiry
            # checks. They never consume another model reservation or tool run.
            if not now-30 <= task['due'] < task['expires'] <= now+86400 or task['expires']<=now:
                raise Rejected("task expired or outside one-day horizon")
            if kind == 'signal':
                if not now-5 <= payload['observed'] <= now:
                    raise Rejected("stale/future signal")
                if payload['venue'] != a['venue']:
                    raise Rejected("venue outside agent grant")
                cursor = db.execute("SELECT * FROM cursors WHERE agent=?", (a['id'],)).fetchone()
                if cursor and (payload['sequence'] <= cursor['sequence'] or payload['observed'] < cursor['observed']):
                    raise Rejected("non-monotonic signal")
            n = db.execute("SELECT count(*) FROM jobs WHERE status IN ('QUEUED','RUNNING') AND agent=?", (a['id'],)).fetchone()[0]
            if n >= 2:
                raise Rejected("agent mailbox full; retry after reading results")
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 100000:
                raise Rejected("journal retention limit; operator archive required")
            job = db.execute("INSERT INTO jobs(agent,key,fingerprint,kind,payload,created,due,expires) VALUES(?,?,?,?,?,?,?,?)",
                             (a['id'],task['key'],fingerprint,kind,encode(payload),now,task['due'],task['expires']))
            if kind == 'signal':
                db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?)", (a['id'],payload['sequence'],payload['observed']))
            return job.lastrowid

    def claim(self, now=None, lane=None):
        now = int(time.time()) if now is None else now
        clause = {None:'', 'fast':" AND j.kind!='triage'", 'model':" AND j.kind='triage'"}[lane]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='EXPIRED',error='deadline' WHERE status='QUEUED' AND expires<=?", (now,))
            row = db.execute("""SELECT j.* FROM jobs j JOIN agents a ON a.id=j.agent
                 WHERE j.status='QUEUED' AND j.due<=?""" + clause + " ORDER BY a.last_served,j.id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            counter = int(db.execute("SELECT value FROM meta WHERE key='dispatch'").fetchone()[0])+1
            db.execute("UPDATE meta SET value=? WHERE key='dispatch'", (str(counter),))
            db.execute("UPDATE agents SET last_served=? WHERE id=?", (counter,row['agent']))
            db.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1 WHERE id=?", (row['id'],))
            return dict(row)

    def reserve_inference(self, agent, now=None):
        day = (int(time.time()) if now is None else now)//86400
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count FROM reservations WHERE day=? AND agent=?", (day,agent)).fetchone()
            total = db.execute("SELECT coalesce(sum(count),0) FROM reservations WHERE day=?", (day,)).fetchone()[0]
            if total >= 1000 or (count and count[0] >= 60):
                return False
            db.execute("INSERT INTO reservations VALUES(?,?,1) ON CONFLICT(day,agent) DO UPDATE SET count=count+1", (day,agent))
            return True

    def finish(self, job, status, result=None, error=None):
        if status not in ('DONE','PROPOSED','REJECTED','EXPIRED','FAILED','ESCALATED','BUDGET_HELD'):
            raise Rejected("invalid terminal status")
        with self.connect() as db:
            changed = db.execute("UPDATE jobs SET status=?,result=?,error=?,finished_ns=? WHERE id=? AND status='RUNNING'",
                                 (status,encode(result) if result is not None else None,error,time.time_ns(),job)).rowcount
            if changed != 1:
                raise Rejected("job no longer owned")

    def recover(self):
        """Only the process holding the exclusive daemon lock calls this.

        Replay is safe because tools have no external side effects: committing
        the result IS the tool effect. Inference reservations are not refunded.
        """
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN attempts<2 THEN 'QUEUED' ELSE 'FAILED' END,error='interrupted' WHERE status='RUNNING'")

    def results(self, agent, after=0):
        ident(agent)
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,agent,key,kind,status,result,error,finished_ns FROM jobs WHERE agent=? AND id>? ORDER BY id LIMIT 100", (agent,after))]

    def status(self):
        with self.connect() as db:
            return {'agents':db.execute('SELECT count(*) FROM agents').fetchone()[0],
                    'jobs':dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status')),
                    'inference_reserved':db.execute('SELECT coalesce(sum(count),0) FROM reservations').fetchone()[0],
                    'execution_authority':False, 'configured_model_slots':1}
