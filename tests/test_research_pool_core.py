from historical_scope import HistoricalScopeTestCase
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.muse import MODEL, _request, canonical, validate_investigation
from economic_machine.research_pool.model import DEADLINE, Model, Policy, request_for
from economic_machine.research_pool.queue import Queue
from economic_machine.research_pool.transport import ProviderError, retry_after


def summary(n=0):
    return dict(job_id='test:' + str(n), source_ids=['page:' + str(n)], kind='coverage', records={'block': n, 'source_verified': True})


def response(request, *, model=MODEL, cost='0.00001'):
    evidence = json.loads(request['messages'][1]['content'])['evidence']
    proposal = dict(job_id=evidence['job_id'], source_ids=evidence['source_ids'], assessment='coverage_gap',
                    priority=50, hypothesis='A retained source can be compared with its indexed records.',
                    next_checks=['verify_source'], remaining_gaps=['Independent finality remains unverified.'])
    return canonical(dict(model=model, choices=[dict(finish_reason='stop', message={'content': canonical(proposal)})], usage={'cost': cost}))


class FakeSpend:
    def __init__(self, base):
        self.db = sqlite3.connect(Path(base) / 'fake-spend.sqlite3', timeout=30)
        self.db.execute('CREATE TABLE IF NOT EXISTS operations(kind TEXT,key TEXT PRIMARY KEY,usd TEXT)')
        self.grant = {'monthly_limit': 1000000}

    def reserve(self, kind, key, usd):
        with self.db:
            self.db.execute('INSERT INTO operations VALUES(?,?,?)', (kind, key, str(usd)))

    def model_finish(self, key, actual):
        if actual is not None:
            with self.db:
                self.db.execute('UPDATE operations SET usd=? WHERE key=?', (str(actual), key))

    def close(self):
        self.db.close()


class PoolTest(HistoricalScopeTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / 'policy.json'
        value = dict(schema='muse-research-pool-v1', model=MODEL, max_workers=50, deadline_ms=DEADLINE,
                     shared_base=str(self.base), monthly_limit_krw=1000000)
        self.path.write_text(canonical(value))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.policy = Policy(self.path, self.digest, self.base)
        self.queue = Queue(self.base)
        self.addCleanup(self.queue.close)

    def job(self, n=0, chain='solana'):
        self.queue.enqueue(chain, summary(n), {'native_job': n}, 'evidence:' + str(n))
        return self.queue.claim(chain)

    def model(self, transport=None, spend_factory=FakeSpend):
        return Model(self.base, self.base, self.base / 'unused-credential', self.policy,
                     transport=transport or (lambda req, _: response(req)), spend_factory=spend_factory)

    def test_same_evidence_not_duplicated_with_new_job_name(self):
        ident = self.queue.enqueue('solana', summary(), {}, 'one')
        renamed = summary()
        renamed['job_id'] = 'different-name'
        self.assertEqual(ident, self.queue.enqueue('solana', renamed, {}, 'two'))
        self.assertEqual(1, self.queue.counts()['total'])

    def test_changed_evidence_key_rejected(self):
        self.queue.enqueue('solana', summary(), {}, 'one')
        with self.assertRaisesRegex(ValueError, 'POOL_EVIDENCE_KEY_CHANGED'):
            self.queue.enqueue('solana', summary(1), {}, 'one')

    def test_cross_chain_evidence_separate(self):
        self.queue.enqueue('42161', summary(), {}, 'one')
        self.queue.enqueue('4663', summary(), {}, 'one')
        self.assertEqual(2, self.queue.counts()['total'])

    def test_valid_result_waits_native_ack(self):
        job = self.job()
        self.assertEqual('valid', self.model().call(job, 0))
        self.assertEqual(1, self.queue.pending_count('solana'))
        self.assertEqual(1, len(self.queue.completed('solana')))
        self.assertEqual(1, len(self.queue.completed('solana')))
        self.queue.ack(job['id'], {'native_evidence_read': True})
        self.assertEqual([], self.queue.completed('solana'))
        self.assertEqual(0, self.queue.pending_count('solana'))
        self.queue.ack(job['id'], {'native_evidence_read': True})
        with self.assertRaisesRegex(ValueError, 'POOL_ACK_IDENTITY'):
            self.queue.ack(job['id'], {'native_evidence_read': False})

    def test_policy_is_pinned_each_admission(self):
        model = self.model()
        job = self.job()
        self.path.write_text(self.path.read_text() + ' ')
        self.assertEqual('worker_error', model.call(job, 0))
        self.assertEqual(0, self.queue.db.execute('SELECT count(*) FROM calls').fetchone()[0])

    def test_deadline(self):
        with self.assertRaisesRegex(ValueError, 'POOL_DEADLINE'):
            self.policy.check(DEADLINE)

    def test_truthful_chain_prompts(self):
        sol = request_for('solana', summary(), 'nonce')['messages'][0]['content']
        rh = request_for('4663', summary(), 'nonce')['messages'][0]['content']
        self.assertIn('Solana', sol)
        self.assertNotIn('Arbitrum', sol)
        self.assertIn('4663', rh)

    def test_all_chain_request_schemas_preserve_accepted_grammar(self):
        for chain in ('solana', '42161', '4663'):
            with self.subTest(chain=chain):
                request = request_for(chain, summary(), 'nonce')
                schema = request['response_format']['json_schema']['schema']
                accepted = _request('investigator', summary(), 'nonce', 4663 if chain == '4663' else 42161)
                self.assertEqual(accepted['response_format']['json_schema']['schema'], schema)
                for keyword in ('minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems'):
                    self.assertNotIn(keyword, canonical(schema))
                self.assertIn('450 characters', request['messages'][0]['content'])
                self.assertIn('180 characters', request['messages'][0]['content'])
                self.assertEqual(1024, request['max_tokens'])
                self.assertEqual(MODEL, request['model'])

    def test_local_response_bounds_still_reject_oversized_prose(self):
        request = request_for('solana', summary(), 'nonce')
        proposal = json.loads(json.loads(response(request))['choices'][0]['message']['content'])
        proposal['hypothesis'] = 'x' * 601
        with self.assertRaisesRegex(ValueError, 'HYPOTHESIS'):
            validate_investigation(proposal, summary())
        proposal['hypothesis'] = 'x' * 600
        proposal['remaining_gaps'] = ['x' * 301]
        with self.assertRaisesRegex(ValueError, 'GAPS'):
            validate_investigation(proposal, summary())

    def test_credentials_rejected_before_queue(self):
        value = summary()
        value['records'] = {'authorization': 'secret'}
        with self.assertRaisesRegex(ValueError, 'SENSITIVE'):
            self.queue.enqueue('solana', value, {}, 'bad')

    def test_unknown_provider_outcome_retains_reservation(self):
        def fail(*_):
            raise ProviderError('PROVIDER_OUTCOME_UNKNOWN')
        self.assertEqual('error_unknown_cost', self.model(fail).call(self.job(), 0))
        totals = self.queue.totals()
        self.assertEqual(1, totals['unknown_cost_calls'])
        self.assertGreater(Decimal(totals['unknown_cost_reserved_usd']), 0)
        self.assertEqual('0', totals['actual_reported_cost_usd'])

    def test_reported_cost_is_settled(self):
        self.model().call(self.job(), 0)
        self.assertEqual('0.00001', self.queue.totals()['actual_reported_cost_usd'])
        self.assertEqual(0, self.queue.totals()['unknown_cost_calls'])

    def test_wrong_model_no_valid_proposal(self):
        result = self.model(lambda req, _: response(req, model='another-model')).call(self.job(), 0)
        self.assertEqual('rejected', result)
        self.assertEqual([], self.queue.completed('solana'))

    def test_invalid_negative_cost_remains_reserved(self):
        result = self.model(lambda req, _: response(req, cost='-1')).call(self.job(), 0)
        self.assertEqual('rejected', result)
        self.assertEqual(1, self.queue.totals()['unknown_cost_calls'])

    def test_cost_breach_blocks_future_admission(self):
        model = self.model(lambda req, _: response(req, cost='100'))
        self.assertEqual('cost_breach', model.call(self.job(), 0))
        self.assertEqual('worker_error', model.call(self.job(1), 1))
        self.assertEqual(1, self.queue.db.execute('SELECT count(*) FROM calls').fetchone()[0])

    def test_429_cooldown_is_shared_and_persistent(self):
        def fail(*_):
            raise ProviderError('PROVIDER_HTTP_429', 300000)
        self.assertEqual('error_unknown_cost', self.model(fail).call(self.job(), 0))
        job = self.job(1)
        self.assertEqual('provider_backoff', self.model().call(job, 1))
        self.assertEqual(1, self.queue.db.execute('SELECT count(*) FROM calls').fetchone()[0])
        self.assertEqual('queued', self.queue.db.execute('SELECT state FROM jobs WHERE id=?', (job['id'],)).fetchone()[0])

    def test_budget_limit_blocks_provider(self):
        class BlockedSpend(FakeSpend):
            def reserve(self, *_args, **_kwargs):
                raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
        self.assertEqual('budget_blocked', self.model(spend_factory=BlockedSpend).call(self.job(), 0))
        self.assertEqual(0, self.queue.totals()['unknown_cost_calls'])

    def test_auth_failure_pauses_all_admission(self):
        def fail(*_):
            raise ProviderError('PROVIDER_HTTP_401')
        self.assertEqual('provider_fatal', self.model(fail).call(self.job(), 0))
        self.assertEqual(DEADLINE, self.queue.db.execute('SELECT not_before_ms FROM provider_gate').fetchone()[0])
        self.assertEqual('provider_backoff', self.model().call(self.job(1), 1))

    def test_failed_admission_after_reserve_is_not_free(self):
        class InterruptedSpend(FakeSpend):
            def reserve(self, *args, **kwargs):
                super().reserve(*args, **kwargs)
                raise RuntimeError('after commit')
        self.assertEqual('reserved_unknown_recovery', self.model(spend_factory=InterruptedSpend).call(self.job(), 0))
        self.assertEqual(1, self.queue.totals()['unknown_cost_calls'])

    def test_recovery_never_replays_sent_call(self):
        job = self.job()
        spend = FakeSpend(self.base)
        spend.reserve('model', 'pool:interrupted', Decimal('.01'))
        spend.close()
        with self.queue.db:
            self.queue.db.execute("INSERT INTO calls(id,job_id,chain,status,reserved) VALUES('interrupted',?,'solana','sent','.01')", (job['id'],))
            self.queue.db.execute("UPDATE jobs SET call_id='interrupted' WHERE id=?", (job['id'],))
        self.model().recover()
        self.assertEqual('reserved_unknown_recovery', self.queue.db.execute('SELECT status FROM calls').fetchone()[0])
        self.assertIsNone(self.queue.claim())
        self.assertEqual(1, self.queue.totals()['unknown_cost_calls'])

    def test_50_real_mock_request_intervals_overlap(self):
        barrier = threading.Barrier(50, timeout=30)
        def overlap(req, _):
            barrier.wait()
            time.sleep(.05)
            return response(req)
        model = self.model(overlap)
        jobs = [self.job(n, ('solana', '42161', '4663')[n % 3]) for n in range(50)]
        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(lambda item: model.call(item[1], item[0]), enumerate(jobs)))
        self.assertEqual(['valid'] * 50, results)
        self.assertEqual(50, model.current()['observed_peak_provider_calls'])
        rows = self.queue.db.execute('SELECT sent_ms,finished_ms FROM calls').fetchall()
        self.assertLessEqual(max(r[0] for r in rows), min(r[1] for r in rows))
        self.assertEqual(0, model.current()['actual_inflight'])

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(120000, retry_after('120'))
        self.assertEqual(30000, retry_after('Thu, 01 Jan 1970 00:01:00 GMT', now=30))
        self.assertEqual(0, retry_after('invalid'))


if __name__ == '__main__':
    unittest.main()
