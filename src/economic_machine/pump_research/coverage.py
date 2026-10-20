"""Durable, source-scoped public wallet coverage; never a lifetime certificate.

The caller supplies its existing storage/CPU guard. The original deadline
remains binding unless that guard owns a validated continuation scope.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .store import address, canonical


DEADLINE_MS = 1792078753482
_IDENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_STATES = {'pending', 'scanning', 'waiting', 'gapped', 'failed', 'exhausted_source'}


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise ValueError('COVERAGE_'+name)
    return value


def _identifier(value):
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise ValueError('COVERAGE_IDENTIFIER')
    return value


def _wallet(value):
    if not address(value):
        raise ValueError('COVERAGE_ADDRESS')
    return value


def _refs(values, required=True):
    if not isinstance(values, (list, tuple)) or len(values) > 16:
        raise ValueError('COVERAGE_EVIDENCE_REFS')
    for value in values:
        if (not isinstance(value, str) or not value or len(value.encode()) > 256 or
                '://' in value or '?' in value or '\x00' in value):
            raise ValueError('COVERAGE_EVIDENCE_REFS')
    result = sorted(set(values))
    if required and not result:
        raise ValueError('COVERAGE_EVIDENCE_REQUIRED')
    return result


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _cursor(value):
    encoded = canonical(value)
    if len(encoded.encode()) > 4096:
        raise ValueError('COVERAGE_CURSOR_BOUND')
    return encoded


def _compact_cursor(encoded):
    raw = encoded.encode()
    if len(raw) <= 600:
        return json.loads(encoded)
    return dict(truncated=True, sha256=hashlib.sha256(raw).hexdigest(),
                preview=raw[:400].decode('utf-8', errors='ignore'))


class Coverage:
    def __init__(self, path, *, guard=None, read_only=False):
        self.path = Path(path)
        self.guard = guard
        self.read_only = read_only
        if read_only:
            self.db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
        else:
            self._guard()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=5)
            self.db.executescript('''
                PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS coverage_sources(
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coverage_frontiers(
                    wallet TEXT NOT NULL, source TEXT NOT NULL REFERENCES coverage_sources(id),
                    state TEXT NOT NULL, cursor TEXT NOT NULL, revision INTEGER NOT NULL,
                    latest_slot INTEGER, latest_refs TEXT NOT NULL, updated_ms INTEGER NOT NULL,
                    PRIMARY KEY(wallet,source));
                CREATE TABLE IF NOT EXISTS coverage_spans(
                    id TEXT PRIMARY KEY, wallet TEXT NOT NULL, source TEXT NOT NULL,
                    kind TEXT NOT NULL, start_slot INTEGER NOT NULL, end_slot INTEGER NOT NULL,
                    body TEXT NOT NULL, at_ms INTEGER NOT NULL,
                    FOREIGN KEY(wallet,source) REFERENCES coverage_frontiers(wallet,source));
                CREATE INDEX IF NOT EXISTS coverage_span_wallet ON coverage_spans(wallet,source,kind,start_slot);
                CREATE TABLE IF NOT EXISTS coverage_transitions(
                    id TEXT PRIMARY KEY, wallet TEXT NOT NULL, source TEXT NOT NULL,
                    body TEXT NOT NULL, at_ms INTEGER NOT NULL,
                    FOREIGN KEY(wallet,source) REFERENCES coverage_frontiers(wallet,source));
                CREATE INDEX IF NOT EXISTS coverage_transition_wallet ON coverage_transitions(wallet,source);
                CREATE TABLE IF NOT EXISTS coverage_links(
                    id TEXT PRIMARY KEY, wallet TEXT NOT NULL, source TEXT NOT NULL,
                    direction TEXT NOT NULL, slot INTEGER NOT NULL, body TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    FOREIGN KEY(wallet,source) REFERENCES coverage_frontiers(wallet,source));
                CREATE INDEX IF NOT EXISTS coverage_link_wallet ON coverage_links(wallet,direction,slot);
            ''')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only='+('ON' if read_only else 'OFF'))

    def close(self):
        self.db.close()

    def _guard(self):
        if self.read_only:
            raise ValueError('COVERAGE_READ_ONLY')
        if not callable(self.guard):
            raise ValueError('COVERAGE_SCOPE_GUARD_REQUIRED')
        if time.time_ns()//1000000 >= DEADLINE_MS:
            from .research_continuation import ContinuationScope
            owner=getattr(self.guard,'__self__',None)
            continued=owner if isinstance(owner,ContinuationScope) else getattr(owner,'continuation',None)
            if not isinstance(continued,ContinuationScope) or continued.effective_deadline_ms is not None:
                raise ValueError('COVERAGE_DEADLINE')
        self.guard()

    @contextmanager
    def _write(self):
        self._guard()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def _source(self, source):
        _identifier(source)
        row = self.db.execute('SELECT body FROM coverage_sources WHERE id=?', (source,)).fetchone()
        if not row:
            raise ValueError('COVERAGE_SOURCE_UNREGISTERED')
        return json.loads(row['body'])

    def _frontier(self, wallet, source):
        _wallet(wallet); _identifier(source)
        row = self.db.execute('SELECT * FROM coverage_frontiers WHERE wallet=? AND source=?', (wallet, source)).fetchone()
        if not row:
            raise ValueError('COVERAGE_WALLET_SOURCE_UNREGISTERED')
        return row

    def _range(self, source, start, end):
        _integer(start, 'SLOT'); _integer(end, 'SLOT')
        scope = self._source(source)
        if not scope['start_slot'] <= start <= end <= scope['end_slot']:
            raise ValueError('COVERAGE_OUTSIDE_SOURCE_SCOPE')
        return scope

    def register_source(self, source, *, kind, start_slot, end_slot, evidence_refs, epoch=None):
        """Pin one immutable source snapshot and its inclusive slot interval."""
        _identifier(source); _identifier(kind)
        _integer(start_slot, 'SLOT'); _integer(end_slot, 'SLOT')
        if start_slot > end_slot:
            raise ValueError('COVERAGE_SOURCE_RANGE')
        if epoch is not None:
            _integer(epoch, 'EPOCH')
        body = dict(chain='solana', kind=kind, epoch=epoch, start_slot=start_slot,
                    end_slot=end_slot, evidence_refs=_refs(evidence_refs),
                    independent_consensus_verified=False)
        encoded = canonical(body)
        with self._write():
            old = self.db.execute('SELECT body FROM coverage_sources WHERE id=?', (source,)).fetchone()
            if old and old['body'] != encoded:
                raise ValueError('COVERAGE_SOURCE_SCOPE_CHANGED')
            self.db.execute('INSERT OR IGNORE INTO coverage_sources VALUES(?,?,?)', (source, encoded, _digest(body)))
        return dict(id=source, **body)

    def register_wallet(self, wallet, source):
        _wallet(wallet); self._source(source)
        with self._write():
            self.db.execute('INSERT OR IGNORE INTO coverage_frontiers VALUES(?,?,?,?,?,?,?,?)',
                (wallet, source, 'pending', 'null', 0, None, '[]', time.time_ns()//1000000))
        return self.frontier(wallet, source)

    def frontier(self, wallet, source):
        row = dict(self._frontier(wallet, source))
        row['cursor'] = json.loads(row['cursor'])
        row['latest_refs'] = json.loads(row['latest_refs'])
        return row

    def record_span(self, wallet, source, kind, start_slot, end_slot, *, method, evidence_refs, reason=None):
        """Record available, inspected, or missing source ranges separately.

        Inspected means this method checked this source; it does not mean every
        canonical transaction in the interval was recovered. Gaps are append-only.
        """
        if kind not in ('available', 'inspected', 'gap'):
            raise ValueError('COVERAGE_SPAN_KIND')
        self._range(source, start_slot, end_slot); _identifier(method)
        if reason is not None and (not isinstance(reason, str) or len(reason.encode()) > 1024):
            raise ValueError('COVERAGE_REASON_BOUND')
        if kind == 'gap' and not reason:
            raise ValueError('COVERAGE_GAP_REASON')
        body = dict(wallet=wallet, source=source, kind=kind, start_slot=start_slot,
                    end_slot=end_slot, method=method, evidence_refs=_refs(evidence_refs), reason=reason)
        ident = _digest(body)
        with self._write():
            self._frontier(wallet, source)
            self.db.execute('INSERT OR IGNORE INTO coverage_spans VALUES(?,?,?,?,?,?,?,?)',
                (ident, wallet, source, kind, start_slot, end_slot, canonical(body), time.time_ns()//1000000))
        return ident

    def _latest(self, wallet, source, slot, refs):
        if slot is None:
            return
        row = self._frontier(wallet, source)
        if row['latest_slot'] is None or slot > row['latest_slot']:
            self.db.execute('UPDATE coverage_frontiers SET latest_slot=?,latest_refs=? WHERE wallet=? AND source=?',
                (slot, canonical(refs), wallet, source))
        elif slot == row['latest_slot']:
            merged = sorted(set(json.loads(row['latest_refs'])) | set(refs))[:16]
            self.db.execute('UPDATE coverage_frontiers SET latest_refs=? WHERE wallet=? AND source=?',
                (canonical(merged), wallet, source))

    def advance(self, wallet, source, *, cursor, state, expected_revision, evidence_refs,
                latest_observed_slot=None, reason=None):
        """CAS a durable cursor; repeat identical transitions safely after a crash."""
        _integer(expected_revision, 'REVISION')
        if state not in _STATES:
            raise ValueError('COVERAGE_FRONTIER_STATE')
        encoded_cursor = _cursor(cursor)
        refs = _refs(evidence_refs)
        if reason is not None and (not isinstance(reason, str) or len(reason.encode()) > 1024):
            raise ValueError('COVERAGE_REASON_BOUND')
        if latest_observed_slot is not None:
            self._range(source, latest_observed_slot, latest_observed_slot)
        body = dict(wallet=wallet, source=source, cursor=cursor, state=state,
                    previous_revision=expected_revision, revision=expected_revision+1,
                    evidence_refs=refs, latest_observed_slot=latest_observed_slot, reason=reason)
        ident = _digest(body)
        with self._write():
            old = self.db.execute('SELECT body FROM coverage_transitions WHERE id=?', (ident,)).fetchone()
            if old:
                return dict(json.loads(old['body']), transition_id=ident, replayed=True)
            row = self._frontier(wallet, source)
            if row['revision'] != expected_revision:
                raise ValueError('COVERAGE_STALE_REVISION')
            if row['state'] == 'exhausted_source':
                raise ValueError('COVERAGE_SOURCE_EXHAUSTED_REGISTER_NEW_SNAPSHOT')
            if cursor is None and row['cursor'] != 'null':
                raise ValueError('COVERAGE_CURSOR_RESET')
            self.db.execute('UPDATE coverage_frontiers SET state=?,cursor=?,revision=?,updated_ms=? WHERE wallet=? AND source=?',
                (state, encoded_cursor, expected_revision+1, time.time_ns()//1000000, wallet, source))
            self._latest(wallet, source, latest_observed_slot, refs)
            self.db.execute('INSERT INTO coverage_transitions VALUES(?,?,?,?,?)',
                (ident, wallet, source, canonical(body), time.time_ns()//1000000))
        return dict(body, transition_id=ident, replayed=False)

    def record_link(self, wallet, source, counterparty, direction, slot, *, evidence_refs,
                    relation='transfer', parent_evidence_refs=()):
        """Record one observed upstream/downstream relation, without fund tracing inference."""
        _wallet(wallet); _wallet(counterparty)
        if direction not in ('incoming', 'outgoing', 'self') or (wallet == counterparty) != (direction == 'self'):
            raise ValueError('COVERAGE_LINK_DIRECTION')
        if relation not in ('transfer', 'account_creation_funding', 'observed_participation', 'provider_lead'):
            raise ValueError('COVERAGE_LINK_RELATION')
        self._range(source, slot, slot)
        refs = _refs(evidence_refs)
        body = dict(wallet=wallet, source=source, counterparty=counterparty, direction=direction,
                    slot=slot, relation=relation, evidence_refs=refs,
                    parent_evidence_refs=_refs(parent_evidence_refs, required=False),
                    same_funds_continuity_proven=False, common_owner_proven=False,
                    independent_consensus_verified=False)
        ident = _digest(body)
        with self._write():
            self._frontier(wallet, source)
            self.db.execute('INSERT OR IGNORE INTO coverage_links VALUES(?,?,?,?,?,?,?)',
                (ident, wallet, source, direction, slot, canonical(body), time.time_ns()//1000000))
            self._latest(wallet, source, slot, refs)
        return ident

    def report(self, wallet, *, limit=64):
        """Bounded read-only projection; no query, retry, model call or cursor mutation."""
        _wallet(wallet)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError('COVERAGE_REPORT_BOUND')
        frontiers = self.db.execute('SELECT f.*,s.body AS source_body,s.sha256 AS source_sha256 FROM coverage_frontiers f JOIN coverage_sources s ON s.id=f.source WHERE wallet=? ORDER BY source LIMIT ?', (wallet, limit+1)).fetchall()
        sources = []
        for row in frontiers[:limit]:
            sources.append(dict(source=row['source'],scope=json.loads(row['source_body']),
                scope_sha256=row['source_sha256'],state=row['state'],cursor=_compact_cursor(row['cursor']),
                revision=row['revision'],latest_observed_slot=row['latest_slot'],
                latest_observation_refs=json.loads(row['latest_refs']),updated_ms=row['updated_ms'],
                entire_source_inspected=False))
        spans = self.db.execute('SELECT id,body FROM coverage_spans WHERE wallet=? ORDER BY start_slot,source,id LIMIT ?', (wallet, limit+1)).fetchall()
        links = self.db.execute('SELECT id,body FROM coverage_links WHERE wallet=? ORDER BY slot DESC,id LIMIT ?', (wallet, limit+1)).fetchall()
        latest = self.db.execute('SELECT source,latest_slot,latest_refs FROM coverage_frontiers WHERE wallet=? AND latest_slot IS NOT NULL ORDER BY latest_slot DESC,source LIMIT 1', (wallet,)).fetchone()
        span_rows = [dict(json.loads(row['body']), id=row['id']) for row in spans[:limit]]
        link_rows = [dict(json.loads(row['body']), id=row['id']) for row in links[:limit]]
        return dict(schema='economic-machine.wallet-coverage.v1',wallet=wallet,
            sources=sources,source_list_truncated=len(frontiers)>limit,
            spans={kind:[v for v in span_rows if v['kind']==kind] for kind in ('available','inspected','gap')},
            span_list_truncated=len(spans)>limit,
            trace_evidence={direction:[v for v in link_rows if v['direction']==direction] for direction in ('incoming','outgoing','self')},
            trace_list_truncated=len(links)>limit,
            latest_observed_slot=latest['latest_slot'] if latest else None,
            latest_observation_source=latest['source'] if latest else None,
            latest_observation_refs=json.loads(latest['latest_refs']) if latest else [],
            coverage_complete=False,entire_wallet_lifetime_verified=False,current_balance_verified=False,
            independent_consensus_verified=False,same_funds_continuity_proven=False,
            limitations=['Availability is a source claim, not an inspection.',
                'Inspection and cursor exhaustion apply only to the registered source snapshot and method.',
                'Ranges outside registered sources and unrecorded gaps remain unknown; gaps are never silently cleared.',
                'The latest observed slot is evidenced history, not a current balance or latest chain activity.',
                'Upstream/downstream links do not prove common ownership or continuity of the same funds.'])
