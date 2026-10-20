from historical_scope import HistoricalScopeTestCase
import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from economic_machine.pump_research import research_pool_adapter as adapter
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph
from economic_machine.research_pool.queue import Queue


def key(number):
    return encode(number.to_bytes(32, 'big'))


class SolanaPoolTests(HistoricalScopeTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)/'solana'
        (self.base/'evidence').mkdir(parents=True)
        self.graph = Graph(self.base)
        self.addCleanup(self.graph.close)
        self.queue = Queue(Path(self.temp.name)/'pool')
        self.addCleanup(self.queue.close)
        adapter.initialize_consumer(self.graph)
        self.sequence = 0

    def job(self, root=1, source='recover_launch'):
        self.sequence += 1
        mint = key(root)
        with self.graph.db:
            self.graph.db.execute('INSERT OR IGNORE INTO roots VALUES(?,?)', (mint, canonical(dict(mint=mint))))
            ident = self.graph.enqueue(mint, key(10000+self.sequence), 100, None, 0, source)
        return ident

    def valid(self, ident=None):
        row = self.queue.db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone() if ident else self.queue.db.execute('SELECT * FROM jobs ORDER BY id LIMIT 1').fetchone()
        summary = json.loads(row['summary_json'])
        proposal = dict(job_id=summary['job_id'], source_ids=summary['source_ids'], assessment='hypothesis',
                        priority=1, hypothesis='The retained source needs a bounded verification step.',
                        next_checks=['verify_source'], remaining_gaps=['Full history remains unavailable.'])
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET state='valid',proposal_json=?,call_id='test-call' WHERE id=?", (canonical(proposal), row['id']))
        return row['id']

    def policy(self, **updates):
        value = dict(schema='muse-research-pool-v1', model=adapter.MODEL, max_workers=50,
                     deadline_ms=adapter.POOL_DEADLINE_MS, shared_base=str(self.base.resolve()), monthly_limit_krw=1000000)
        value.update(updates)
        raw = canonical(value).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (self.queue.out/'policy.json').write_bytes(raw)
        (self.queue.out/'policy.sha256').write_text(digest+'\n')
        return digest

    def test_feeder_does_not_write_graph_and_deduplicates(self):
        ident = self.job()
        before = tuple(self.graph.db.execute('SELECT * FROM frontier WHERE id=?', (ident,)).fetchone())
        with self.graph.db:
            self.graph.edge(key(1), key(10001), key(99), 'transfer', 100, None, amount_lamports=12, signature='public-signature')
        result = adapter.enqueue_jobs(self.queue, self.base)
        self.assertEqual(result['enqueued'], 1)
        self.assertEqual(tuple(self.graph.db.execute('SELECT * FROM frontier WHERE id=?', (ident,)).fetchone()), before)
        summary = json.loads(self.queue.db.execute('SELECT summary_json FROM jobs').fetchone()[0])
        self.assertEqual(summary['records']['retained_observations'][0]['evidence']['amount_lamports'], 12)
        self.assertFalse(summary['records']['same_funds_proven'])
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 0)

    def test_unchanged_key_does_not_mutate_when_other_edges_arrive(self):
        self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        with self.graph.db:
            self.graph.edge(key(1), key(10001), key(99), 'transfer', 100, None, amount_lamports=12)
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 0)

    def test_backpressure_and_root_rotation(self):
        for root in range(1, 21):
            for _ in range(4):
                self.job(root)
        first = adapter.enqueue_jobs(self.queue, self.base, max_pending=10)
        self.assertEqual(first['enqueued'], 10)
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base, max_pending=10)['state'], 'NATIVE_CONSUMER_BACKPRESSURE')
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET state='consumed'")
        adapter.enqueue_jobs(self.queue, self.base, max_pending=10)
        roots = {json.loads(row[0])['frontier']['root'] for row in self.queue.db.execute('SELECT metadata_json FROM jobs')}
        self.assertGreater(len(roots), 3)

    def test_cursor_progress_and_frontier_eligibility(self):
        ident = self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        with self.graph.db:
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?', ('[100,1,1]', ident))
        self.assertEqual(adapter.drain_ready(self.queue, self.graph), [])
        self.assertEqual(self.queue.counts()['by_state']['consumed'], 1)
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 1)

    def test_model_links_retained_and_ack_follows_actual_native_outcome(self):
        ident = self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        pool_id = self.valid()
        jobs = adapter.drain_ready(self.queue, self.graph)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.queue.db.execute('SELECT state FROM jobs').fetchone()[0], 'valid')
        recorded = json.loads(self.graph.db.execute('SELECT body FROM investigations').fetchone()[0])
        self.assertEqual(recorded['job_ids'], [ident])
        self.assertEqual(recorded['pool_job_id'], pool_id)
        self.assertEqual(recorded['pool_call_id'], 'test-call')
        with self.graph.db:
            self.graph.db.execute("UPDATE frontier SET state='observed' WHERE id=?", (ident,))
        adapter.finish_job(self.queue, self.graph, jobs[0], dict(job_id=ident, state='observed', records=1))
        row = self.queue.db.execute('SELECT state,outcome_json FROM jobs').fetchone()
        self.assertEqual(row[0], 'consumed')
        self.assertEqual(json.loads(row[1])['native_result']['records'], 1)

    def test_invalid_metadata_rejected(self):
        self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        with self.queue.db:
            metadata = json.loads(self.queue.db.execute('SELECT metadata_json FROM jobs').fetchone()[0])
            metadata['frontier_sha256'] = '0'*64
            self.queue.db.execute('UPDATE jobs SET metadata_json=?', (canonical(metadata),))
        self.assertEqual(adapter.drain_ready(self.queue, self.graph), [])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 0)

    def test_retain_gap_only_never_fetches_or_advances_frontier(self):
        ident = self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        with self.queue.db:
            value = json.loads(self.queue.db.execute('SELECT proposal_json FROM jobs').fetchone()[0])
            value['next_checks'] = ['retain_gap']
            self.queue.db.execute('UPDATE jobs SET proposal_json=?', (canonical(value),))
        self.assertEqual(adapter.drain_ready(self.queue, self.graph), [])
        self.assertEqual(self.graph.db.execute('SELECT state FROM frontier WHERE id=?', (ident,)).fetchone()[0], 'pending')
        outcome = json.loads(self.queue.db.execute('SELECT outcome_json FROM jobs').fetchone()[0])
        self.assertEqual(outcome['state'], 'MODEL_RETAINED_GAP')
        self.assertFalse(outcome['executed'])
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 0)

    def test_unstarted_tail_recovers_same_approved_cursor_without_model_call(self):
        self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        original = adapter.drain_ready(self.queue, self.graph)[0]
        recovered = adapter.drain_ready(self.queue, self.graph)[0]
        self.assertEqual(recovered['_research_pool_id'], original['_research_pool_id'])
        self.assertTrue(recovered['_research_pool_recovered'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 1)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_interrupted_advanced_cursor_is_not_rewound_or_excluded(self):
        ident = self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        adapter.drain_ready(self.queue, self.graph)
        with self.graph.db:
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?', ('[100,2,0]', ident))
        self.assertEqual(adapter.drain_ready(self.queue, self.graph), [])
        outcome = json.loads(self.queue.db.execute('SELECT outcome_json FROM jobs').fetchone()[0])
        self.assertEqual(outcome['state'], 'RECOVERED_CURSOR_CHANGED_OUTCOME_UNKNOWN')
        self.assertFalse(outcome['native_step_outcome_known'])
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 1)

    def test_native_result_saved_before_ack_recovers_without_reexecution(self):
        ident = self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        job = adapter.drain_ready(self.queue, self.graph)[0]
        ack = self.queue.ack
        def interrupted(*args):
            raise InterruptedError('test_ack_boundary')
        self.queue.ack = interrupted
        with self.assertRaises(InterruptedError):
            adapter.finish_job(self.queue, self.graph, job, dict(job_id=ident, state='pending', records=1))
        self.queue.ack = ack
        self.assertEqual(adapter.drain_ready(self.queue, self.graph), [])
        self.assertEqual(self.queue.db.execute('SELECT state FROM jobs').fetchone()[0], 'consumed')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 1)

    def test_source_cooldown_reuses_accepted_decision_without_new_model(self):
        ident = self.job(source='gsfa_history')
        adapter.enqueue_jobs(self.queue, self.base)
        pool_id = self.valid()
        job = adapter.drain_ready(self.queue, self.graph)[0]
        with self.graph.db:
            self.graph.db.execute("UPDATE frontier SET state='source_blocked' WHERE id=?", (ident,))
        adapter.finish_job(self.queue, self.graph, job, dict(job_id=ident, state='waiting_for_source', retryable=True,
                                                           retry_at_ms=1, no_request=True, attempts=1))
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 0)
        with self.graph.db:
            body = json.loads(self.graph.db.execute('SELECT body FROM research_pool_executions WHERE id=?', (pool_id,)).fetchone()[0])
            body['resume_after_ms'] = 0
            self.graph.db.execute('UPDATE research_pool_executions SET body=? WHERE id=?', (canonical(body), pool_id))
            self.graph.db.execute("UPDATE frontier SET state='pending' WHERE id=?", (ident,))
        resumed = adapter.drain_ready(self.queue, self.graph)
        self.assertEqual(len(resumed), 1)
        self.assertTrue(resumed[0]['_research_pool_resumed'])
        adapter.finish_job(self.queue, self.graph, resumed[0], dict(job_id=ident, state='pending', raw_results_applied=1))
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 1)

    def test_native_semantic_progress_ignores_timing(self):
        self.assertEqual(adapter._semantic(dict(state='pending', updated_ms=1, history=dict(pages=1, elapsed_ms=20))),
                         adapter._semantic(dict(state='pending', updated_ms=10, history=dict(pages=1, elapsed_ms=40))))

    def test_unavailable_source_inputs_not_admitted(self):
        self.job(source='core_forward')
        self.job(source='indexed_forward')
        self.assertEqual(adapter.enqueue_jobs(self.queue, self.base)['enqueued'], 0)

    def test_native_policy_sidecar_then_journal_pin(self):
        digest = self.policy()
        guard = adapter.NativePoolGuard(self.queue, self.base)
        self.assertEqual(guard.digest, digest)
        with self.queue.db:
            self.queue.db.execute('INSERT INTO identity VALUES(1,?,?)', (digest, str(self.base.resolve())))
        self.assertEqual(guard.check()['max_workers'], 50)
        with self.queue.db:
            self.queue.db.execute('DELETE FROM identity')
        with self.assertRaisesRegex(ValueError, 'IDENTITY_REMOVED'):
            guard.check()

    def test_native_policy_changed_bytes_fail_closed(self):
        self.policy()
        guard = adapter.NativePoolGuard(self.queue, self.base)
        with (self.queue.out/'policy.json').open('ab') as f:
            f.write(b' ')
        with self.assertRaisesRegex(ValueError, 'POLICY_CHANGED'):
            guard.check()

    def test_native_policy_identity_and_deadline_cannot_expand(self):
        self.policy(deadline_ms=adapter.POOL_DEADLINE_MS+1)
        with self.assertRaisesRegex(ValueError, 'POLICY_AUTHORITY'):
            adapter.NativePoolGuard(self.queue, self.base)
        digest = self.policy()
        with self.queue.db:
            self.queue.db.execute('INSERT INTO identity VALUES(1,?,?)', (digest, '/different-base'))
        with self.assertRaisesRegex(ValueError, 'POLICY_IDENTITY'):
            adapter.NativePoolGuard(self.queue, self.base)

    def test_native_deadline_prevents_decision_consumption(self):
        self.policy()
        guard = adapter.NativePoolGuard(self.queue, self.base)
        self.job()
        adapter.enqueue_jobs(self.queue, self.base)
        self.valid()
        with patch.object(adapter.time, 'time_ns', return_value=adapter.POOL_DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError, 'SOL_POOL_DEADLINE'):
                adapter.drain_ready(self.queue, self.graph, guard=guard)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM research_pool_executions').fetchone()[0], 0)
        self.assertEqual(self.queue.db.execute('SELECT state FROM jobs').fetchone()[0], 'valid')

    def test_bound_scope_preserves_legacy_grant_and_guards_nested_reservations(self):
        self.policy()
        guard = adapter.NativePoolGuard(self.queue, self.base)
        calls = []
        original = SimpleNamespace(effective_deadline_ms=None, grant={'deadline_ms':1},
                                   guard=lambda *a, **k: calls.append(('guard',a,k)),
                                   reserve=lambda *a, **k: calls.append(('reserve',a,k)))
        wrapped = adapter.PoolBoundScope(original, guard)
        wrapped.guard(12, force=True)
        wrapped.reserve(10, 'test')
        self.assertIs(wrapped.grant, original.grant)
        self.assertIsNone(original.effective_deadline_ms)
        self.assertIsNone(wrapped.effective_deadline_ms)
        self.assertEqual(wrapped.operating_deadline_ms, adapter.POOL_DEADLINE_MS)
        self.assertEqual(len(calls), 2)
        with patch.object(adapter.time, 'time_ns', return_value=adapter.POOL_DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError, 'SOL_POOL_DEADLINE'):
                wrapped.reserve(10, 'must-not-execute')
        self.assertEqual(len(calls), 2)

    def test_native_operating_clock_preserves_portal_continuation_identity(self):
        from economic_machine.pump_research.portal_history import continuation_scope
        from economic_machine.pump_research.gsfa_index import Budget as IndexBudget
        self.policy()
        guard = adapter.NativePoolGuard(self.queue, self.base)
        continued = SimpleNamespace(effective_deadline_ms=None, hash='a'*64,
                                    assert_slot=lambda slot: None, inventory_epochs=[685])
        original = SimpleNamespace(effective_deadline_ms=None, continuation=continued,
                                   guard=lambda *a, **k: None, reserve=lambda *a, **k: None)
        wrapped = adapter.PoolBoundScope(original, guard)
        self.assertIs(continuation_scope(wrapped), continued)
        index_budget = IndexBudget(wrapped.effective_deadline_ms, 1024*1024, wrapped.guard)
        self.assertEqual(wrapped.operating_deadline_ms, adapter.POOL_DEADLINE_MS)
        with patch.object(adapter.time, 'time_ns', return_value=adapter.POOL_DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError, 'SOL_POOL_DEADLINE'):
                wrapped.guard()
            with self.assertRaisesRegex(ValueError, 'SOL_POOL_DEADLINE'):
                wrapped.reserve(1, 'must-not-admit')
            with self.assertRaisesRegex(ValueError, 'SOL_POOL_DEADLINE'):
                index_budget.check()

    def test_hundred_native_decisions_bound_preserves_per_job_records(self):
        for i in range(1, 14):
            for _ in range(8):
                self.job(root=i)
        adapter.enqueue_jobs(self.queue, self.base, max_pending=100)
        for row in self.queue.db.execute('SELECT id FROM jobs').fetchall():
            self.valid(row[0])
        jobs = adapter.drain_ready(self.queue, self.graph, limit=100)
        self.assertEqual(len(jobs), 100)
        self.assertEqual(len({j['_research_pool_id'] for j in jobs}), 100)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 100)


if __name__ == '__main__':
    unittest.main()
