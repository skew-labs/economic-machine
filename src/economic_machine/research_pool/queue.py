"""One SQLite queue; independent connections for each feeder and provider lane."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

CHAINS = ('solana', '42161', '4663')
PENDING = ('queued', 'claimed', 'valid')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def validate_summary(value):
    """Dependency-neutral admission mirror; provider reuses the EVM validator.

    The Solana graph writer imports this queue without loading an EVM runtime.
    Actual dispatch calls arbitrum_research.muse.validate_summary again.
    """
    if type(value) is not dict or value.keys() != {'job_id', 'source_ids', 'kind', 'records'}:
        raise ValueError('POOL_SUMMARY_FIELDS')
    valid_id = lambda item: type(item) is str and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', item)
    if not valid_id(value['job_id']) or value['kind'] not in ('coverage', 'protocol', 'graph', 'custody', 'bridge'):
        raise ValueError('POOL_SUMMARY_ID')
    ids = value['source_ids']
    if type(ids) is not list or not 1 <= len(ids) <= 32 or any(not valid_id(x) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('POOL_SUMMARY_SOURCES')
    if type(value['records']) not in (dict, list) or not value['records']:
        raise ValueError('POOL_SUMMARY_RECORDS')
    body = canonical(value)
    if len(body.encode()) > 9000:
        raise ValueError('POOL_SUMMARY_SIZE')
    if re.search(r'(?i)sk-or-|sk-proj-|private[_ -]?key|api[_ -]?key|authorization|bearer |password|seed[_ -]?phrase|recovery[_ -]?phrase', body):
        raise ValueError('POOL_SENSITIVE_CONTEXT')
    return json.loads(body)


def now_ms():
    return time.time_ns() // 1000000


class Queue:
    def __init__(self, root_path):
        self.root = Path(root_path)
        self.out = self.root / 'evidence/muse-pool-v1'
        self.out.mkdir(parents=True, exist_ok=True)
        self.path = self.out / 'queue.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY,policy_hash TEXT,shared_base TEXT);
          CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY,chain TEXT NOT NULL,evidence_key TEXT NOT NULL,
            summary_json TEXT NOT NULL,metadata_json TEXT NOT NULL,evidence_hash TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',created_ms INTEGER NOT NULL,claimed_ms INTEGER,
            finished_ms INTEGER,call_id TEXT,proposal_json TEXT,outcome_json TEXT,
            UNIQUE(chain,evidence_key),UNIQUE(chain,evidence_hash));
          CREATE INDEX IF NOT EXISTS job_ready ON jobs(state,chain,id);
          CREATE TABLE IF NOT EXISTS calls(
            id TEXT PRIMARY KEY,job_id INTEGER UNIQUE,chain TEXT,lane INTEGER,
            status TEXT,created_ms INTEGER,sent_ms INTEGER,finished_ms INTEGER,
            reserved TEXT,actual TEXT,request_hash TEXT,request_json TEXT,
            response_hash TEXT,response_json TEXT,proposal_json TEXT,error_code TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(id));
          CREATE INDEX IF NOT EXISTS call_status ON calls(status,sent_ms);
          CREATE INDEX IF NOT EXISTS call_sent ON calls(sent_ms);
          CREATE INDEX IF NOT EXISTS call_finished ON calls(finished_ms);
          CREATE TABLE IF NOT EXISTS provider_gate(
            id INTEGER PRIMARY KEY,not_before_ms INTEGER NOT NULL,failure_streak INTEGER NOT NULL,
            last_error TEXT,updated_ms INTEGER NOT NULL);
          INSERT OR IGNORE INTO provider_gate VALUES(1,0,0,NULL,0);
          CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY,at_ms INTEGER,kind TEXT,body TEXT);
        ''')

    def close(self):
        self.db.close()

    def enqueue(self, chain, summary, metadata, evidence_key):
        chain = str(chain)
        if chain not in CHAINS or type(evidence_key) is not str or not 1 <= len(evidence_key) <= 512:
            raise ValueError('POOL_JOB_IDENTITY')
        summary = validate_summary(summary)
        meta = canonical(metadata)
        if len(meta.encode()) > 32768:
            raise ValueError('POOL_METADATA_BOUND')
        # A job name alone cannot turn identical evidence into fresh research.
        evidence = dict(summary)
        evidence.pop('job_id')
        digest = hashlib.sha256(canonical(evidence).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO jobs(chain,evidence_key,summary_json,metadata_json,evidence_hash,created_ms) '
                            'VALUES(?,?,?,?,?,?)',
                            (chain, evidence_key, canonical(summary), meta, digest, now_ms()))
            row = self.db.execute('SELECT * FROM jobs WHERE chain=? AND (evidence_key=? OR evidence_hash=?) ORDER BY id LIMIT 1',
                                  (chain, evidence_key, digest)).fetchone()
            if row['evidence_key'] == evidence_key and (row['evidence_hash'] != digest or row['metadata_json'] != meta):
                raise ValueError('POOL_EVIDENCE_KEY_CHANGED')
        return row['id']

    @staticmethod
    def _row(row):
        result = dict(row)
        for name in ('summary', 'metadata', 'proposal', 'outcome'):
            raw = result.pop(name + '_json', None)
            result[name] = json.loads(raw) if raw is not None else None
        return result

    def completed(self, chain, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('POOL_RESULT_LIMIT')
        return [self._row(row) for row in self.db.execute(
            "SELECT * FROM jobs WHERE chain=? AND state='valid' ORDER BY id LIMIT ?", (str(chain), limit))]

    def ack(self, ident, outcome):
        body = canonical(outcome)
        if len(body.encode()) > 32768:
            raise ValueError('POOL_OUTCOME_BOUND')
        with self.db:
            row = self.db.execute('SELECT state,outcome_json FROM jobs WHERE id=?', (ident,)).fetchone()
            if not row or (row['state'] == 'consumed' and row['outcome_json'] != body):
                raise ValueError('POOL_ACK_IDENTITY')
            if row['state'] == 'consumed':
                return
            if row['state'] != 'valid':
                raise ValueError('POOL_ACK_NOT_VALID')
            self.db.execute("UPDATE jobs SET state='consumed',outcome_json=? WHERE id=?", (body, ident))

    def pending_count(self, chain):
        return self.db.execute("SELECT count(*) FROM jobs WHERE chain=? AND state IN ('queued','claimed','valid')", (str(chain),)).fetchone()[0]

    def active_evidence_keys(self, chain):
        return {row[0] for row in self.db.execute(
            "SELECT evidence_key FROM jobs WHERE chain=? AND state IN ('queued','claimed','valid')", (str(chain),))}

    def counts(self):
        states = {r[0]: r[1] for r in self.db.execute('SELECT state,count(*) FROM jobs GROUP BY state')}
        chains = {chain: {} for chain in CHAINS}
        for row in self.db.execute('SELECT chain,state,count(*) FROM jobs GROUP BY chain,state'):
            chains[row[0]][row[1]] = row[2]
        return dict(total=sum(states.values()), by_state=states, by_chain=chains)

    def claim(self, preferred_chain=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = None
            if preferred_chain is not None:
                row = self.db.execute("SELECT * FROM jobs WHERE state='queued' AND chain=? ORDER BY id LIMIT 1", (str(preferred_chain),)).fetchone()
            if row is None:
                row = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET state='claimed',claimed_ms=? WHERE id=? AND state='queued'", (now_ms(), row['id']))
            return self._row(row)

    def event(self, kind, body):
        with self.db:
            self.db.execute('INSERT INTO events(at_ms,kind,body) VALUES(?,?,?)', (now_ms(), kind, canonical(body)))

    def totals(self):
        rows = self.db.execute('SELECT status,reserved,actual,response_hash,sent_ms,finished_ms FROM calls').fetchall()
        reserved = [r for r in rows if r['reserved'] is not None and r['status'] not in ('budget_blocked', 'admission_error')]
        actual = sum((Decimal(r['actual']) for r in rows if r['actual'] is not None), Decimal(0))
        unknown = [r for r in reserved if r['actual'] is None]
        return dict(calls_recorded=len(rows), responses_received=sum(bool(r['response_hash']) for r in rows),
                    valid_responses=sum(r['status'] == 'valid' for r in rows),
                    errors=sum(r['status'] in ('error_unknown_cost', 'rejected', 'cost_breach') for r in rows),
                    actual_reported_cost_usd=str(actual), unknown_cost_calls=len(unknown),
                    unknown_cost_reserved_usd=str(sum((Decimal(r['reserved']) for r in unknown), Decimal(0))),
                    last_sent_ms=max((r['sent_ms'] or 0 for r in rows), default=0),
                    last_finished_ms=max((r['finished_ms'] or 0 for r in rows), default=0),
                    cost_breach=any(r['status'] == 'cost_breach' for r in rows))
