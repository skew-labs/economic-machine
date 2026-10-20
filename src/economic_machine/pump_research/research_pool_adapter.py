"""Read-only Solana planning feed and single-writer native result consumer.

The pool never opens Graph, advances a frontier cursor, or asserts a fund edge.
An accepted proposal authorizes the existing bounded source step for exactly
the retained frontier snapshot. Source retries retain that original decision.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .store import canonical
from ..muse_maker import MODEL

CHAIN = 'solana'
POOL_DEADLINE_MS = 1792351694298
FIELDS = ('id', 'root', 'wallet', 'slot', 'stamp', 'depth', 'state', 'cursor', 'source')
SOURCES = ('recover_launch', 'gsfa_history', 'portal_verify', 'portal_resolve_launch',
           'archive_transfer', 'wormhole_forward', 'portal_forward', 'local_forward',
           'indexed_forward', 'core_early', 'core_participation', 'core_forward')
VOLATILE = frozenset(('at_ms', 'updated_ms', 'checked_ms', 'created_ms', 'elapsed_ms',
                      'elapsed_ns', 'retry_at_ms', 'attempts', 'next_at', 'started',
                      'finished', 'created', 'retry_after', 'timestamp'))


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class NativePoolGuard:
    """Pin the additional operating scope without rewriting historical grants."""
    def __init__(self, queue, solana_base):
        self.queue = queue
        self.shared_base = str(Path(solana_base).resolve())
        self.path = queue.out/'policy.json'
        self.sidecar = queue.out/'policy.sha256'
        self.digest = None
        self.identity_seen = False
        self.deadline_ms = POOL_DEADLINE_MS
        self.check()

    def check(self, now=None):
        row = self.queue.db.execute('SELECT policy_hash,shared_base FROM identity WHERE id=1').fetchone()
        if row:
            pinned = row['policy_hash']
            if row['shared_base'] != self.shared_base:
                raise ValueError('SOL_POOL_POLICY_IDENTITY')
            self.identity_seen = True
        else:
            if self.identity_seen:
                raise ValueError('SOL_POOL_POLICY_IDENTITY_REMOVED')
            if self.sidecar.is_symlink() or not self.sidecar.is_file() or self.sidecar.stat().st_size > 128:
                raise ValueError('SOL_POOL_POLICY_PIN')
            pinned = self.sidecar.read_text().strip()
        if not isinstance(pinned, str) or len(pinned) != 64 or any(c not in '0123456789abcdef' for c in pinned):
            raise ValueError('SOL_POOL_POLICY_PIN')
        if self.digest is not None and pinned != self.digest:
            raise ValueError('SOL_POOL_POLICY_CHANGED')
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 32768:
            raise ValueError('SOL_POOL_POLICY_FILE')
        raw = self.path.read_bytes()
        if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != pinned:
            raise ValueError('SOL_POOL_POLICY_CHANGED')
        value = json.loads(raw)
        required = dict(schema='muse-research-pool-v1', model=MODEL, max_workers=50,
                        deadline_ms=POOL_DEADLINE_MS, shared_base=self.shared_base, monthly_limit_krw=1000000)
        if any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in required.items()):
            raise ValueError('SOL_POOL_POLICY_AUTHORITY')
        self.digest = pinned
        if (time.time_ns()//1000000 if now is None else now) >= self.deadline_ms:
            raise ValueError('SOL_POOL_DEADLINE')
        return value


class PoolBoundScope:
    """Delegate original accounting and add the pool clock to every callback."""
    def __init__(self, scope, guard):
        self.original = scope
        self.pool_guard = guard
        original = getattr(scope, 'effective_deadline_ms', None)
        # This is an additional operating clock, not a replacement for the
        # underlying continuation's authority metadata. Portal admission pins
        # the original explicit uncapped continuation via its None deadline.
        self.operating_deadline_ms = min(original, guard.deadline_ms) if type(original) is int else guard.deadline_ms

    def __getattr__(self, name):
        return getattr(self.original, name)

    def guard(self, *args, **kwargs):
        self.pool_guard.check()
        return self.original.guard(*args, **kwargs)

    def reserve(self, *args, **kwargs):
        self.pool_guard.check()
        return self.original.reserve(*args, **kwargs)


def _snapshot(row):
    return {key: row[key] for key in FIELDS}


def _semantic(value):
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _read(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-4096')
    expires = time.monotonic() + 2
    db.set_progress_handler(lambda: int(time.monotonic() > expires), 10000)
    return db


def _state(db, key, default=None):
    row = db.execute('SELECT body FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _summary(db, job):
    snapshot = _snapshot(job)
    progress = _state(db, 'research_pool_native_progress:' + job['id'])
    revision = _hash(dict(frontier=snapshot, native_progress=progress))
    source_id = 'solana:frontier:' + job['id'] + ':' + revision[:16]
    records = dict(chain='solana', frontier=dict(id=job['id'], root=job['root'],
        wallet=job['wallet'], slot=job['slot'], depth=job['depth'], source=job['source'],
        cursor_sha256=_hash(job['cursor']), cursor_bytes=len(job['cursor'].encode())),
        prior_native_progress=progress, retained_observations=[], retained_activity=[],
        complete_history=False, common_owner_proven=False, same_funds_proven=False,
        instruction='Assess the next bounded native source step. Retained observations may be provider claims; do not invent verified fund continuity.')
    ids = [source_id]
    # Both lookups use the existing edge_source / edge_target indexes. Bound
    # each side independently; no OR scan across the full retained graph.
    observations = {}
    for field in ('source', 'target'):
        for row in db.execute('SELECT * FROM edges WHERE '+field+'=? ORDER BY rowid DESC LIMIT 3', (job['wallet'],)):
            observations[row['id']] = row
    for row in observations.values():
        body = json.loads(row['body'])
        item = {key: row[key] for key in ('id', 'root', 'source', 'target', 'kind', 'slot', 'stamp')}
        item['evidence'] = {key: body[key] for key in ('signature', 'amount_lamports', 'amount',
            'mint', 'tx_order', 'ix', 'success', 'execution_state', 'finality') if key in body}
        item['claim_scope'] = 'retained_observation_not_independent_finality'
        records['retained_observations'].append(item)
        ids.append('solana:edge:' + row['id'])
    for row in db.execute('SELECT mint,action,slot,ix,tx_order,signature,finality FROM local_activity '
                          'WHERE wallet=? ORDER BY slot DESC LIMIT 3', (job['wallet'],)):
        records['retained_activity'].append(dict(row))
    anchor = db.execute('SELECT slot,tx_order,ix FROM frontier_anchors WHERE job=?', (job['id'],)).fetchone()
    if anchor:
        records['immutable_anchor'] = dict(anchor)
    if 'custody_jobs' in _tables(db) and db.execute('SELECT 1 FROM custody_jobs WHERE job=? LIMIT 1', (job['id'],)).fetchone():
        kind = 'custody'
        records['custody_gap'] = 'Private exchange customer ledgers and same-funds continuity are unavailable.'
    else:
        kind = 'bridge' if job['source'] == 'wormhole_forward' else 'graph'
    summary = dict(job_id='solana:' + job['id'] + ':' + revision[:16],
                   source_ids=ids, kind=kind, records=records)
    if len(canonical(summary).encode()) > 9000:
        records['retained_activity'] = []
        records['retained_observations'] = records['retained_observations'][:2]
        summary['source_ids'] = [source_id] + ['solana:edge:' + row['id'] for row in records['retained_observations']]
    if len(canonical(summary).encode()) > 9000:
        raise ValueError('SOL_POOL_SUMMARY_BOUND')
    metadata = dict(schema='economic-machine.solana-pool-job.v1', frontier=snapshot,
                    frontier_sha256=_hash(snapshot), native_progress=progress,
                    evidence_revision=revision)
    return summary, metadata, revision


def enqueue_jobs(queue, solana_base, max_pending=100):
    """Only read the native graph; all new state belongs to the shared queue."""
    if type(max_pending) is not int or not 1 <= max_pending <= 1000:
        raise ValueError('SOL_POOL_PENDING_BOUND')
    base = Path(solana_base)
    path = base / 'evidence/wallet-map-v1/graph.sqlite3'
    if not path.is_file():
        return dict(enqueued=0, state='WAITING_NATIVE_GRAPH')
    pending = queue.pending_count(CHAIN)
    room = max(0, max_pending-pending)
    if not room:
        return dict(enqueued=0, pending=pending, state='NATIVE_CONSUMER_BACKPRESSURE')
    added = 0
    with queue.db:
        queue.db.execute('CREATE TABLE IF NOT EXISTS solana_feed_cursors(root TEXT PRIMARY KEY,after_rowid INTEGER NOT NULL)')
    with closing(_read(path)) as db:
        tables = _tables(db)
        resumed = set()
        if 'research_pool_executions' in tables:
            resumed = {row[0] for row in db.execute("SELECT job FROM research_pool_executions WHERE state IN ('executing','resume')")}
        ready = dict(core_forward=(base/'evidence/core-trace-v1/arkm_funding.sqlite3').is_file(),
                     core_early=(base/'evidence/core-trace-v1/pump_event_default.sqlite3').is_file(),
                     core_participation=(base/'evidence/core-trace-v1/pump_event_default.sqlite3').is_file(),
                     indexed_forward=(base/'evidence/wallet-map-v1/address-index.sqlite3').is_file())
        # Per-root indexed queries keep a huge global frontier from monopolizing
        # a lane. Selection progress is durable in queue dedup, not graph state.
        roots = [row[0] for row in db.execute('SELECT mint FROM roots ORDER BY rowid LIMIT 1200')]
        root_cursor = queue.db.execute("SELECT after_rowid FROM solana_feed_cursors WHERE root='__next_root__'").fetchone()
        start = (root_cursor[0] if root_cursor else 0) % max(1, len(roots))
        started = time.monotonic()
        for offset in range(min(len(roots), 128)):
            if time.monotonic()-started > 1.5:
                break
            position = (start+offset) % len(roots)
            root = roots[position]
            with queue.db:
                queue.db.execute("INSERT INTO solana_feed_cursors VALUES('__next_root__',?) ON CONFLICT(root) DO UPDATE SET after_rowid=excluded.after_rowid", (position+1,))
            cursor = queue.db.execute('SELECT after_rowid FROM solana_feed_cursors WHERE root=?', (root,)).fetchone()
            after = cursor[0] if cursor else 0
            rows = db.execute("SELECT rowid AS feed_rowid,* FROM frontier WHERE root=? AND state='pending' "
                              "AND rowid>? ORDER BY rowid LIMIT 8", (root, after)).fetchall()
            if not rows and after:
                rows = db.execute("SELECT rowid AS feed_rowid,* FROM frontier WHERE root=? AND state='pending' "
                                  "ORDER BY rowid LIMIT 8", (root,)).fetchall()
            for row in rows:
                job = dict(row)
                with queue.db:
                    queue.db.execute('INSERT INTO solana_feed_cursors VALUES(?,?) ON CONFLICT(root) DO UPDATE SET after_rowid=excluded.after_rowid',
                                     (root, job['feed_rowid']))
                if job['id'] in resumed or job['source'] not in SOURCES or not ready.get(job['source'], True):
                    continue
                revision = _hash(dict(frontier=_snapshot(job), native_progress=_state(db, 'research_pool_native_progress:' + job['id'])))
                if queue.db.execute('SELECT 1 FROM jobs WHERE chain=? AND evidence_key=?', (CHAIN, revision)).fetchone():
                    continue
                summary, metadata, revision = _summary(db, job)
                before = queue.pending_count(CHAIN)
                queue.enqueue(CHAIN, summary, metadata, revision)
                added += int(queue.pending_count(CHAIN) > before)
                if added >= room:
                    return dict(enqueued=added, pending=queue.pending_count(CHAIN), state='QUEUED')
    return dict(enqueued=added, pending=queue.pending_count(CHAIN), state='QUEUED' if added else 'WAITING_NEW_EVIDENCE')


def initialize_consumer(graph):
    graph.db.execute('''CREATE TABLE IF NOT EXISTS research_pool_executions(
        id INTEGER PRIMARY KEY,job TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,updated_ms INTEGER NOT NULL)''')
    graph.db.execute('CREATE INDEX IF NOT EXISTS research_pool_execution_job ON research_pool_executions(job,state)')
    graph.db.commit()


def _eligible(graph, metadata):
    supplied = metadata.get('frontier', {})
    if metadata.get('schema') != 'economic-machine.solana-pool-job.v1' or metadata.get('frontier_sha256') != _hash(supplied):
        return None
    row = graph.db.execute('SELECT * FROM frontier WHERE id=?', (supplied.get('id'),)).fetchone()
    if not row or row['state'] != 'pending' or _snapshot(row) != supplied:
        return None
    progress = graph.state('research_pool_native_progress:' + row['id'])
    if metadata.get('evidence_revision') != _hash(dict(frontier=supplied, native_progress=progress)):
        return None
    return dict(row)


def drain_ready(queue, graph, limit=8, guard=None):
    """Strictly bind proposals to unchanged source jobs, on the native writer."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('SOL_POOL_CONSUMER_BOUND')
    if guard is not None:
        guard.check()
    jobs = []
    completed = queue.completed(CHAIN, limit=min(1000, limit*4))
    completed.sort(key=lambda item: (-item['proposal'].get('priority', 0), item['id']))
    for item in completed[:limit]:
        if guard is not None:
            guard.check()
        prior = graph.db.execute('SELECT * FROM research_pool_executions WHERE id=?', (item['id'],)).fetchone()
        if prior:
            body = json.loads(prior['body'])
            if prior['state'] == 'executing':
                recovered = _recover_execution(queue, graph, prior, ack_pending=True)
                if recovered:
                    jobs.append(recovered)
            else:
                # A recorded outcome precedes ack, so an interrupted cross-DB
                # acknowledgement does not rerun a completed native step.
                queue.ack(item['id'], body['outcome'])
            continue
        job = _eligible(graph, item['metadata'])
        proposal = item['proposal']
        if not job or proposal.get('job_id') != item['summary']['job_id'] or not set(proposal.get('source_ids', ())).issubset(item['summary']['source_ids']):
            queue.ack(item['id'], dict(state='STALE_OR_UNBOUND_NATIVE_JOB', executed=False))
            continue
        record = dict(schema='economic-machine.solana-pool-selection.v1', pool_job_id=item['id'],
            pool_call_id=item.get('call_id'), original_model_proposal=proposal,
            job_ids=[job['id']], scheduled_job_ids=[job['id']], engine_reserved={},
            source_snapshot_sha256=item['metadata']['frontier_sha256'],
            original_model_scope_and_journal_preserved=True)
        now = time.time_ns()//1000000
        with graph.db:
            graph.db.execute('INSERT INTO investigations(call_id,body,at) VALUES(NULL,?,?)', (canonical(record), now))
            graph.db.execute('INSERT INTO research_pool_executions VALUES(?,?,?,?,?)',
                (item['id'], job['id'], 'executing', canonical(dict(record=record, metadata=item['metadata'])), now))
        action_checks = {'fetch_blocks', 'fetch_receipts', 'fetch_traces', 'verify_source',
                         'decode_protocol', 'follow_address', 'verify_bridge'}
        if not action_checks.intersection(proposal.get('next_checks', ())):
            outcome = dict(state='MODEL_RETAINED_GAP', executed=False,
                           remaining_gaps=proposal.get('remaining_gaps', []))
            body = dict(record=record, metadata=item['metadata'], outcome=outcome)
            with graph.db:
                graph.db.execute("UPDATE research_pool_executions SET state='assessment_only',body=? WHERE id=?", (canonical(body), item['id']))
            queue.ack(item['id'], outcome)
            continue
        job['_research_pool_id'] = item['id']
        jobs.append(job)
    # Native source cooldowns retry the same accepted decision; no fresh model
    # call or invented new evidence is required just because a timer elapsed.
    now = time.time_ns()//1000000
    for prior in graph.db.execute("SELECT * FROM research_pool_executions WHERE state IN ('resume','executing') ORDER BY updated_ms LIMIT 64").fetchall():
        if guard is not None:
            guard.check()
        if len(jobs) >= limit:
            break
        if prior['id'] in {job['_research_pool_id'] for job in jobs}:
            continue
        queued = queue.db.execute('SELECT state FROM jobs WHERE id=?', (prior['id'],)).fetchone()
        if not queued or queued[0] != 'consumed':
            continue
        if prior['state'] == 'executing':
            recovered = _recover_execution(queue, graph, prior, ack_pending=False)
            if recovered:
                jobs.append(recovered)
            continue
        body = json.loads(prior['body'])
        if body.get('resume_after_ms', 0) > now:
            continue
        row = graph.db.execute('SELECT * FROM frontier WHERE id=?', (prior['job'],)).fetchone()
        if not row or row['state'] != 'pending':
            continue
        snapshot = _snapshot(row)
        expected = body['resume_frontier']
        if snapshot != expected:
            with graph.db:
                graph.db.execute("UPDATE research_pool_executions SET state='resume_stale' WHERE id=?", (prior['id'],))
            continue
        with graph.db:
            graph.db.execute("UPDATE research_pool_executions SET state='executing' WHERE id=?", (prior['id'],))
        job = dict(row)
        job['_research_pool_id'] = prior['id']
        job['_research_pool_resumed'] = True
        jobs.append(job)
    return jobs


def _recover_execution(queue, graph, prior, ack_pending):
    """The sole native writer can resume an unchanged, idempotent source step.

    The original source IO/reservation journals remain authoritative. A changed
    cursor is never rewound; it is released for a fresh evidence revision.
    """
    body = json.loads(prior['body'])
    original = body.get('resume_frontier') if not ack_pending else body['metadata']['frontier']
    row = graph.db.execute('SELECT * FROM frontier WHERE id=?', (prior['job'],)).fetchone()
    current = _snapshot(row) if row else None
    now = time.time_ns()//1000000
    if current and original and current == original and current['state'] == 'pending':
        body['recovery'] = dict(at_ms=now, unchanged_frontier=True,
                                original_model_decision_reused=True, original_source_journals_preserved=True)
        with graph.db:
            graph.db.execute('UPDATE research_pool_executions SET body=?,updated_ms=? WHERE id=?',
                             (canonical(body), now, prior['id']))
        job = dict(row)
        job['_research_pool_id'] = prior['id']
        job['_research_pool_recovered'] = True
        job['_research_pool_resumed'] = not ack_pending
        return job
    error = graph.state('source_error:' + prior['job'], {})
    blocked = bool(current and original and current['state'] == 'source_blocked' and
                   dict(current, state='pending') == dict(original, state='pending') and error.get('retryable'))
    outcome = dict(state='RECOVERED_SOURCE_COOLDOWN' if blocked else 'RECOVERED_CURSOR_CHANGED_OUTCOME_UNKNOWN',
                   native_step_outcome_known=False, frontier_after=current, cursor_rewound=False,
                   original_model_decision_reused=blocked)
    body['recovery'] = dict(outcome, at_ms=now)
    if ack_pending:
        body['outcome'] = outcome
    if blocked:
        body['resume_frontier'] = dict(current, state='pending')
        body['resume_after_ms'] = max(now+30000, int(error.get('retry_at_ms', 0)))
    with graph.db:
        graph.db.execute('UPDATE research_pool_executions SET state=?,body=?,updated_ms=? WHERE id=?',
                         ('resume' if blocked else 'recovered_cursor_advanced', canonical(body), now, prior['id']))
    if ack_pending:
        queue.ack(prior['id'], outcome)
    return None


def finish_job(queue, graph, job, outcome):
    ident = job.get('_research_pool_id')
    if ident is None:
        return
    row = graph.db.execute('SELECT body FROM research_pool_executions WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('SOL_POOL_EXECUTION_MISSING')
    body = json.loads(row[0])
    current = graph.db.execute('SELECT * FROM frontier WHERE id=?', (job['id'],)).fetchone()
    semantic = _semantic(outcome)
    previous = graph.state('research_pool_native_progress:' + job['id'])
    # Store a bounded digest of actual work, never an advancing synthetic clock.
    progress = dict(result_sha256=_hash(semantic), state=outcome.get('state'),
                    raw_results_applied=outcome.get('raw_results_applied'))
    source_wait = outcome.get('state') in ('waiting_for_source', 'source_blocked') or bool(outcome.get('waiting_for'))
    resume = bool(current and (current['state'] == 'source_blocked' and outcome.get('retryable') or
                  current['state'] == 'pending' and (source_wait or progress == previous)))
    native_outcome = dict(state='WAITING_SOURCE' if source_wait else 'NATIVE_STEP_FINISHED',
        native_result=outcome, frontier_after=_snapshot(current) if current else None,
        same_model_decision_resumed=bool(job.get('_research_pool_resumed')),
        verified_edges_claimed=False)
    body['outcome'] = native_outcome
    if resume:
        body['resume_frontier'] = dict(_snapshot(current), state='pending')
        body['resume_after_ms'] = max(time.time_ns()//1000000 + 30000, int(outcome.get('retry_at_ms', 0)))
    with graph.db:
        if not source_wait:
            graph.set('research_pool_native_progress:' + job['id'], progress)
        graph.db.execute('UPDATE research_pool_executions SET state=?,body=?,updated_ms=? WHERE id=?',
            ('resume' if resume else 'finished', canonical(body), time.time_ns()//1000000, ident))
    if not job.get('_research_pool_resumed'):
        queue.ack(ident, native_outcome)


def pool_status(queue):
    return dict(mode='external_shared_pool', configured_shared_model_workers=50,
                pending_native_approvals=queue.pending_count(CHAIN),
                source_fetch_parallelism=1, evidence_writer_parallelism=1,
                active_model_calls_are_reported_by_shared_pool=True)
