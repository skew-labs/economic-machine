from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.pump_research.research_workspace import Workspace
from economic_machine.pump_research.store import atomic, canonical, encode
from economic_machine.pump_research.wallet_map import (
    Graph, MODEL, UncappedMuse, prepare_jobs, select_parallel,
)


def key(number):
    return encode(number.to_bytes(32, 'big'))


def response(req, model=MODEL):
    context = json.loads(req['messages'][1]['content'])
    ids = [context['jobs'][0]['id']]
    value = dict(job_ids=ids, reason='Inspect retained public records.',
                 remaining_gaps=['Complete history and same funds are unverified.'])
    return canonical(dict(model=model, choices=[dict(message=dict(content=canonical(value)))],
                          usage=dict(cost='0.00001')))


class GraphCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'evidence').mkdir()
        self.graph = Graph(self.base)
        self.addCleanup(self.graph.close)
        self.scope = SimpleNamespace(guard=lambda *args, **kwargs: None)
        self.sequence = 0

    def root(self, number=1, recent=False):
        root = key(number)
        value = dict(mint=root, creator=key(number+1000), created_ms=1792055000000)
        if recent:
            value['cohort_id'] = 'recent-snapshot'
        with self.graph.db:
            self.graph.db.execute('INSERT INTO roots VALUES(?,?)', (root, canonical(value)))
        return root

    def job(self, root, cursor='null', source='recover_launch'):
        self.sequence += 1
        with self.graph.db:
            ident = self.graph.enqueue(root, key(100000+self.sequence), None,
                                       1792055000000+self.sequence, 0, source)
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?', (cursor, ident))
        return ident


class ParallelTests(GraphCase):
    def setUp(self):
        super().setUp()
        self.budget = UncappedMuse(self.graph.out/'test-model.sqlite3', dict(
            authorization='owner_unlimited_muse_top10_forward_wallet_research',
            maximum_usd=None, max_calls=None, model=MODEL, trade_authority=False,
            deadline_ms=time.time_ns()//1000000+600000))
        self.addCleanup(self.budget.db.close)

    def test_interactive_execution_precedes_cohort_without_changing_model_choices(self):
        cohort = self.root(1)
        wallet = self.root(2, recent=True)
        with self.graph.db:
            self.graph.db.execute('UPDATE roots SET body=? WHERE mint=?',
                (canonical(dict(mint=wallet, root_kind='wallet', cohort_kind='internal_wallet_request')), wallet))
        cohort_job = self.job(cohort)
        wallet_job = self.job(wallet, source='core_forward')
        self.job(wallet)
        def provider(req, credential):
            context = json.loads(req['messages'][1]['content'])
            choices = [job['id'] for job in context['jobs']]
            value = dict(job_ids=choices, reason='Inspect both roots.', remaining_gaps=[])
            return canonical(dict(model=MODEL, choices=[dict(message=dict(content=canonical(value)))], usage=dict(cost='0.00001')))
        with patch('economic_machine.pump_research.wallet_map.provider_call', side_effect=provider):
            jobs = select_parallel(self.graph, self.budget, 'unused', self.scope, workers=1)
        self.assertEqual(jobs[0]['root'], wallet)
        self.assertEqual(jobs[-1]['id'], cohort_job)
        record = json.loads(self.graph.db.execute('SELECT body FROM investigations').fetchone()[0])
        self.assertEqual(set(record['job_ids']), {job['id'] for job in jobs})
        self.assertEqual(self.graph.state('parallel_last_batch')['execution_order_job_ids'], [job['id'] for job in jobs])

    def test_ten_model_calls_overlap_disjoint_menus_and_durable_reservations(self):
        root = self.root()
        for _ in range(20):
            self.job(root)
        lock = threading.Lock()
        barrier = threading.Barrier(10, timeout=20)
        stats = dict(active=0, peak=0)
        requests = []

        def provider(req, credential):
            with lock:
                stats['active'] += 1
                stats['peak'] = max(stats['peak'], stats['active'])
                requests.append(req)
            try:
                barrier.wait()
                return response(req)
            finally:
                with lock:
                    stats['active'] -= 1

        with patch('economic_machine.pump_research.wallet_map.provider_call', side_effect=provider):
            jobs = select_parallel(self.graph, self.budget, 'unused-never-read', self.scope, workers=10)
        self.assertEqual(stats['peak'], 10)
        self.assertEqual(len(requests), 10)
        seen = set()
        original_choices = set()
        for req in requests:
            self.assertEqual(req['model'], MODEL)
            self.assertEqual(req['max_tokens'], 4096)
            self.assertFalse(req['provider']['allow_fallbacks'])
            self.assertLessEqual(len(canonical(req).encode()), 16000)
            context = json.loads(req['messages'][1]['content'])
            ids = {job['id'] for job in context['jobs']}
            self.assertFalse(ids.intersection(seen))
            seen.update(ids)
            original_choices.add(context['jobs'][0]['id'])
        self.assertEqual(len(seen), 20)
        self.assertEqual({job['id'] for job in jobs}, original_choices)
        totals = self.budget.totals()
        self.assertEqual(totals['calls_reserved'], 10)
        self.assertEqual(totals['accepted_responses'], 10)
        self.assertEqual(totals['unconfirmed_cost_reservations'], 0)
        records = [json.loads(row[0]) for row in self.graph.db.execute('SELECT body FROM investigations')]
        self.assertEqual({value['job_ids'][0] for value in records}, original_choices)
        self.assertTrue(all(len(value['job_ids']) == 1 for value in records))
        workers = json.loads((self.graph.out/'workers.json').read_bytes())
        self.assertEqual(workers['active'], 0)
        self.assertEqual(Counter(row['state'] for row in workers['workers']), {'planned': 10})

    def test_all_fail_keeps_unknown_cost_reservations(self):
        root = self.root()
        for _ in range(6):
            self.job(root)
        with patch('economic_machine.pump_research.wallet_map.provider_call',
                   side_effect=ValueError('provider outcome unknown')):
            with self.assertRaisesRegex(ValueError, 'MAP_MODEL_PROVIDER_TRANSIENT'):
                select_parallel(self.graph, self.budget, 'unused', self.scope, workers=3)
        totals = self.budget.totals()
        self.assertEqual(totals['calls_reserved'], 3)
        self.assertEqual(totals['unconfirmed_cost_reservations'], 3)
        self.assertEqual(totals['responses_received'], 0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 0)
        rows = self.budget.db.execute('SELECT status,actual FROM calls').fetchall()
        self.assertEqual(rows, [('reserved', None)]*3)
        workers = json.loads((self.graph.out/'workers.json').read_bytes())
        self.assertEqual(workers['active'], 0)
        self.assertEqual(len(self.graph.state('parallel_last_batch')['errors']), 3)

    def test_partial_failure_does_not_discard_other_lanes(self):
        root = self.root()
        for _ in range(6):
            self.job(root)

        def provider(req, credential):
            lane = json.loads(req['messages'][1]['content'])['planner_lane']['lane']
            if lane == 0:
                raise ValueError('provider HTTP 503')
            return response(req)

        with patch('economic_machine.pump_research.wallet_map.provider_call', side_effect=provider):
            result = select_parallel(self.graph, self.budget, 'unused', self.scope, workers=3)
        self.assertEqual(len(result), 2)
        self.assertEqual(self.budget.totals()['accepted_responses'], 2)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 2)
        self.assertEqual(len(self.graph.state('parallel_last_batch')['errors']), 1)

    def test_rejected_response_cost_differs_from_unknown_provider_outcome(self):
        root = self.root()
        self.job(root)
        with patch('economic_machine.pump_research.wallet_map.provider_call',
                   side_effect=lambda req, credential: response(req, model='wrong-model')):
            with self.assertRaisesRegex(ValueError, 'MAP_MODEL_PARALLEL_ALL_FAILED'):
                select_parallel(self.graph, self.budget, 'unused', self.scope, workers=1)
        self.assertEqual(self.budget.totals()['responses_received'], 1)
        self.assertEqual(self.budget.totals()['accepted_responses'], 0)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 0)
        self.assertEqual(self.budget.db.execute('SELECT status,actual FROM calls').fetchone(),
                         ('rejected', '0.00001'))

    def test_engine_supplement_does_not_rewrite_model_choices(self):
        original = self.job(self.root(1))
        recent = self.job(self.root(2, recent=True))
        with patch('economic_machine.pump_research.wallet_map.provider_call',
                   side_effect=lambda req, credential: response(req)):
            jobs = select_parallel(self.graph, self.budget, 'unused', self.scope, workers=1)
        record = json.loads(self.graph.db.execute('SELECT body FROM investigations').fetchone()[0])
        self.assertEqual(record['job_ids'], [original])
        self.assertEqual(set(record['scheduled_job_ids']), {original, recent})
        self.assertEqual(record['engine_reserved']['recent'], recent)
        self.assertEqual({job['id'] for job in jobs}, {original, recent})

    def test_admission_failure_still_records_already_dispatched_response(self):
        root = self.root()
        for _ in range(6):
            self.job(root)
        reserve = self.budget.reserve
        attempts = []

        def bounded_reserve(req, now):
            attempts.append(req)
            if len(attempts) == 2:
                raise ValueError('CORE_DEADLINE')
            return reserve(req, now)

        with patch.object(self.budget, 'reserve', side_effect=bounded_reserve), \
                patch('economic_machine.pump_research.wallet_map.provider_call',
                      side_effect=lambda req, credential: response(req)):
            with self.assertRaisesRegex(ValueError, 'CORE_DEADLINE'):
                select_parallel(self.graph, self.budget, 'unused', self.scope, workers=3)
        self.assertEqual(self.budget.totals()['calls_reserved'], 1)
        self.assertEqual(self.budget.totals()['accepted_responses'], 1)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 1)
        workers = json.loads((self.graph.out/'workers.json').read_bytes())
        self.assertEqual(workers['active'], 0)
        self.assertEqual(workers['workers'][0]['state'], 'planned')

    def test_large_cursor_menu_request_remains_inside_provider_bound(self):
        cursor = canonical(dict(samples=['large history \"line\" '*100]*10))
        for number in range(1, 11):
            root = self.root(number, recent=number > 5)
            self.job(root, cursor=cursor)
            self.job(root, cursor=cursor)
        prepared = prepare_jobs(self.graph, self.scope)
        self.assertEqual(len(prepared['mapping']), 20)
        self.assertLessEqual(len(canonical(prepared['req']).encode()), 16000)
        context = json.loads(prepared['req']['messages'][1]['content'])
        self.assertTrue(all(len(job['page_cursor'].encode()) <= 600 for job in context['jobs']))

    def test_worker_limit_and_empty_queue_never_call_provider(self):
        with patch('economic_machine.pump_research.wallet_map.provider_call') as provider:
            for count in (0, 101, True):
                with self.assertRaisesRegex(ValueError, 'MAP_WORKERS_BOUND'):
                    select_parallel(self.graph, self.budget, 'unused', self.scope, workers=count)
            self.assertEqual(select_parallel(self.graph, self.budget, 'unused', self.scope), [])
        provider.assert_not_called()
        self.assertEqual(self.budget.totals()['calls_reserved'], 0)

    def test_hundred_calls_overlap_with_disjoint_menus_and_coordinator_journaling(self):
        root=self.root()
        for _ in range(200):self.job(root)
        barrier=threading.Barrier(100,timeout=90)
        mutex=threading.Lock();requests=[];threads=set()
        coordinator=threading.get_ident()
        def provider(req,credential):
            with mutex:
                requests.append(req);threads.add(threading.get_ident())
            barrier.wait()
            return response(req)
        reserve=self.budget.reserve;finish=self.budget.finish
        def checked_reserve(*args,**kwargs):
            self.assertEqual(threading.get_ident(),coordinator)
            return reserve(*args,**kwargs)
        def checked_finish(*args,**kwargs):
            self.assertEqual(threading.get_ident(),coordinator)
            return finish(*args,**kwargs)
        with patch.object(self.budget,'reserve',side_effect=checked_reserve), \
                patch.object(self.budget,'finish',side_effect=checked_finish), \
                patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=provider):
            jobs=select_parallel(self.graph,self.budget,'unused',self.scope,workers=100)
        self.assertEqual(len(threads),100)
        self.assertNotIn(coordinator,threads)
        seen=set()
        for req in requests:
            self.assertEqual(req['model'],MODEL)
            ids={j['id'] for j in json.loads(req['messages'][1]['content'])['jobs']}
            self.assertFalse(seen.intersection(ids));seen.update(ids)
        self.assertEqual(len(seen),200)
        self.assertEqual(len(jobs),100)
        totals=self.budget.totals()
        self.assertEqual(totals['calls_reserved'],100)
        self.assertEqual(totals['accepted_responses'],100)
        self.assertEqual(totals['unconfirmed_cost_reservations'],0)
        batch=self.graph.state('parallel_last_batch')
        self.assertEqual(batch['configured'],100)
        self.assertEqual(batch['max_inflight'],100)
        self.assertEqual(batch['observed_peak_concurrent'],100)
        self.assertEqual(batch['dispatched'],100)
        self.assertEqual(batch['provider_calls_completed'],100)
        self.assertEqual(batch['source_fetch_parallelism'],1)
        self.assertEqual(batch['evidence_writer_parallelism'],1)
        events=dict(self.graph.db.execute('SELECT event,count(*) FROM parallel_model_events GROUP BY event'))
        self.assertEqual(events,{'reserved':100,'dispatch_submitted':100,'provider_completed':100,'result_journaled':100})
        workers=json.loads((self.graph.out/'workers.json').read_bytes())
        self.assertEqual(workers['active'],0)
        self.assertEqual(workers['observed_peak_concurrent'],100)

    def test_stop_closes_admission_but_drains_already_dispatched_calls(self):
        root=self.root()
        for _ in range(8):self.job(root)
        def allow():
            return self.graph.db.execute("SELECT count(*) FROM parallel_model_events WHERE event='dispatch_submitted'").fetchone()[0]<2
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=lambda req,key:response(req)):
            jobs=select_parallel(self.graph,self.budget,'unused',self.scope,workers=100,admission_open=allow)
        self.assertEqual(len(jobs),2)
        self.assertEqual(self.budget.totals()['accepted_responses'],2)
        self.assertEqual(self.graph.state('parallel_last_batch')['dispatched'],2)
        self.assertEqual(json.loads((self.graph.out/'workers.json').read_bytes())['active'],0)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM parallel_model_lanes WHERE state='not_dispatched'").fetchone()[0],2)

    def test_restart_preserves_unconfirmed_reservation_and_never_replays_old_call(self):
        root=self.root()
        for _ in range(4):self.job(root)
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=lambda req,key:response(req)):
            select_parallel(self.graph,self.budget,'unused',self.scope,workers=1)
        with self.graph.db:self.graph.set('planner_lane',dict(batch_ms=1,lane=99))
        prepared=prepare_jobs(self.graph,self.scope)
        call=self.budget.reserve(prepared['req'],time.time_ns()//1000000)
        old_batch=1
        body=dict(lane=0,call_id=call,state='thinking',request_sha256=hashlib.sha256(canonical(prepared['req']).encode()).hexdigest())
        with self.graph.db:
            self.graph.db.execute('INSERT INTO parallel_model_lanes VALUES(?,?,?,?,?)',(old_batch,0,call,'thinking',canonical(body)))
        prior_call=tuple(self.budget.db.execute('SELECT * FROM calls WHERE id=?',(call,)).fetchone())
        prior_requests=list(self.graph.out.glob('muse-request-*.json'))
        with patch('economic_machine.pump_research.wallet_map.provider_call') as provider:
            jobs=select_parallel(self.graph,self.budget,'unused',self.scope,workers=100,admission_open=lambda:False)
        provider.assert_not_called()
        self.assertEqual(jobs,[])
        self.assertEqual(tuple(self.budget.db.execute('SELECT * FROM calls WHERE id=?',(call,)).fetchone()),prior_call)
        self.assertEqual(list(self.graph.out.glob('muse-request-*.json')),prior_requests)
        row=self.graph.db.execute('SELECT state,body FROM parallel_model_lanes WHERE batch=?',(old_batch,)).fetchone()
        self.assertEqual(row['state'],'interrupted_outcome_unknown')
        self.assertFalse(json.loads(row['body'])['automatically_replayed'])
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'],1)
        self.assertEqual(self.graph.state('parallel_last_batch')['interrupted_lanes_preserved'],1)

    def test_provider_error_completion_events_keep_cost_unknown(self):
        root=self.root()
        for _ in range(4):self.job(root)
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=ValueError('provider HTTP 429')):
            with self.assertRaisesRegex(ValueError,'MAP_MODEL_PROVIDER_TRANSIENT'):
                select_parallel(self.graph,self.budget,'unused',self.scope,workers=2)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'],2)
        batch=self.graph.state('parallel_last_batch')
        self.assertEqual(batch['provider_calls_completed'],2)
        self.assertEqual(batch['dispatched'],2)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM parallel_model_events WHERE event='provider_completed'").fetchone()[0],2)
        self.assertTrue(all(row['provider_transient'] for row in batch['errors']))

    def test_mixed_transient_provider_failures_trigger_slower_retry_once(self):
        root=self.root()
        for _ in range(8):self.job(root)
        errors=('provider HTTP 429','provider HTTP 502','provider HTTP 503','provider outcome unknown')
        def provider(req,credential):
            lane=json.loads(req['messages'][1]['content'])['planner_lane']['lane']
            raise ValueError(errors[lane])
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=provider) as call:
            with self.assertRaisesRegex(ValueError,'^MAP_MODEL_PROVIDER_TRANSIENT$'):
                select_parallel(self.graph,self.budget,'unused',self.scope,workers=4)
        self.assertEqual(call.call_count,4)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'],4)
        self.assertEqual(self.graph.state('parallel_last_batch')['provider_calls_completed'],4)

    def test_nontransient_failure_keeps_generic_all_failed_classification(self):
        root=self.root()
        for _ in range(4):self.job(root)
        def provider(req,credential):
            lane=json.loads(req['messages'][1]['content'])['planner_lane']['lane']
            raise ValueError('provider HTTP 401' if lane==0 else 'provider HTTP 502')
        with patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=provider) as call:
            with self.assertRaisesRegex(ValueError,'^MAP_MODEL_PARALLEL_ALL_FAILED$'):
                select_parallel(self.graph,self.budget,'unused',self.scope,workers=2)
        self.assertEqual(call.call_count,2)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'],2)
        self.assertEqual(sum(row['provider_transient'] for row in self.graph.state('parallel_last_batch')['errors']),1)

    def test_queued_worker_checks_deadline_before_provider_without_shared_scope_access(self):
        root=self.root()
        for _ in range(4):self.job(root)
        deadline=self.budget.scope['deadline_ms']
        clock=[deadline-1000]
        release=threading.Event();entered=threading.Event();calls=[]
        coordinator=threading.get_ident()
        def guard():self.assertEqual(threading.get_ident(),coordinator)
        scope=SimpleNamespace(guard=guard,grant={'deadline_ms':deadline})
        class DelayedPool(ThreadPoolExecutor):
            def __init__(self,**kwargs):
                super().__init__(max_workers=1,thread_name_prefix='deadline-test')
                self.submissions=0
            def submit(self,*args,**kwargs):
                future=super().submit(*args,**kwargs);self.submissions+=1
                if self.submissions==1:
                    if not entered.wait(10):raise AssertionError('first provider did not start')
                if self.submissions==2:
                    clock[0]=deadline+1
                    release.set()
                return future
        def provider(req,key):
            calls.append(req);entered.set()
            if not release.wait(10):raise AssertionError('queued deadline test timed out')
            return response(req)
        with patch('economic_machine.pump_research.wallet_map.time.time_ns',side_effect=lambda:clock[0]*1000000), \
                patch('economic_machine.pump_research.wallet_map.ThreadPoolExecutor',DelayedPool), \
                patch('economic_machine.pump_research.wallet_map.provider_call',side_effect=provider):
            jobs=select_parallel(self.graph,self.budget,'unused',scope,workers=2)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(jobs),1)
        batch=self.graph.state('parallel_last_batch')
        self.assertEqual(batch['dispatched'],1)
        self.assertEqual(batch['provider_calls_completed'],1)
        self.assertEqual(batch['not_dispatched_before_provider'],1)
        self.assertEqual(batch['observed_peak_concurrent'],1)
        self.assertFalse(batch['source_jobs_exactly_once'])
        self.assertTrue(batch['menus_disjoint_within_batch'])
        second=self.graph.db.execute('SELECT state,body FROM parallel_model_lanes WHERE lane=1').fetchone()
        self.assertEqual(second['state'],'not_dispatched')
        self.assertEqual(json.loads(second['body'])['reason'],'deadline_before_provider_start')
        self.assertFalse(json.loads(second['body'])['provider_charge_claimed'])
        self.assertEqual(self.budget.totals()['accepted_responses'],1)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'],1)


class WorkspaceTests(GraphCase):
    def setUp(self):
        super().setUp()
        self.workspace = Workspace(self.base)
        self.addCleanup(self.workspace.close)
        with self.graph.db:
            self.graph.set('portal_finalized_head', dict(number=456000000, hash='synthetic-provider-head',
                                                       at_ms=time.time_ns()//1000000))

    def test_address_validation_and_idempotent_queue(self):
        for value in (None, '', 'not-solana', '0x1234', ['address']):
            with self.assertRaises(ValueError):
                self.workspace.submit(value)
        first = self.workspace.submit(key(1))
        second = self.workspace.submit(key(1))
        self.assertEqual(first, second)
        self.assertEqual(self.workspace.db.execute('SELECT count(*) FROM requests').fetchone()[0], 1)
        self.assertEqual(first['state'], 'queued')
        self.assertIsNone(self.workspace.report('unknown'))

    def test_admission_preserves_existing_root_and_does_not_duplicate_jobs(self):
        root = self.root()
        before = self.graph.db.execute('SELECT body FROM roots WHERE mint=?', (root,)).fetchone()[0]
        request = self.workspace.submit(root)
        self.workspace.admit(self.graph, self.scope)
        self.workspace.admit(self.graph, self.scope)
        after = self.graph.db.execute('SELECT body FROM roots WHERE mint=?', (root,)).fetchone()[0]
        self.assertEqual(before, after)
        sources = dict(self.graph.db.execute('SELECT source,count(*) FROM frontier GROUP BY source'))
        self.assertEqual(sources, dict(portal_forward=1, core_forward=1))
        self.assertEqual(self.workspace.db.execute('SELECT count(*) FROM admissions').fetchone()[0], 1)
        self.assertEqual(self.workspace.db.execute('SELECT state FROM requests WHERE id=?',
                                                   (request['id'],)).fetchone()[0], 'investigating')

    def test_report_partial_and_engine_choice_not_attributed_to_muse(self):
        wallet = key(1)
        request = self.workspace.submit(wallet)
        self.workspace.admit(self.graph, self.scope)
        job = self.graph.db.execute('SELECT id FROM frontier WHERE root=? LIMIT 1', (wallet,)).fetchone()[0]
        signature = encode(bytes([7])*64)
        with self.graph.db:
            raw_edge = self.graph.edge(wallet, wallet, key(2), 'raw_archive_transfer', 400000000, None,
                            signature=signature, source='sparse_archive', source_sha256='a'*64,
                            finality='archive_content_verified', independent_consensus_verified=False)
            self.graph.edge(wallet, wallet, key(3), 'provider_lead', 400000001, None,
                            signature=signature, source='sqd_public_portal', source_sha256='b'*64)
            self.graph.edge(wallet, wallet, key(4), 'funding_record', 400000002, None,
                            signature=signature, source='core_arkm_funding')
            self.graph.edge(wallet, wallet, key(5), 'funding_record', 400000003, None,
                            dead_branch=True)
            self.graph.db.execute('INSERT INTO investigations(call_id,body,at) VALUES(?,?,?)',
                                  (1, canonical(dict(job_ids=['other'], scheduled_job_ids=[job], reason='engine only')), 1))
            self.graph.db.execute('INSERT INTO investigations(call_id,body,at) VALUES(?,?,?)',
                                  (2, canonical(dict(job_ids=[job], scheduled_job_ids=[job], reason='model choice')), 2))
            self.graph.db.execute("UPDATE frontier SET state='source_exhausted' WHERE root=?", (wallet,))
        # This test covers presentation/planning, not raw verification. Supply
        # an explicit synthetic assessment ledger instead of trusting a label.
        from economic_machine.pump_research.commit_graph import _schema, _revision
        from economic_machine.pump_research.address_index import AddressIndex
        _schema(self.graph.db)
        index = AddressIndex(self.graph.out/'address-index.sqlite3')
        try:
            revision = _revision(index.db)[0]
        finally:
            index.close()
        original = self.graph.db.execute('SELECT rowid,body FROM edges WHERE id=?', (raw_edge,)).fetchone()
        assessment = dict(edge=raw_edge, status='committed', instruction_commit_status='committed',
            assessment_revision=revision, assessment_current=True,
            edge_body_sha256=hashlib.sha256(original['body'].encode()).hexdigest(),
            reason='synthetic_presentation_fixture', transaction_success=True, source_sha256='a'*64)
        with self.graph.db:
            self.graph.db.execute('INSERT INTO edge_commit_assessments(edge,revision,pass_id,edge_rowid,status,body) VALUES(?,?,?,?,?,?)',
                (raw_edge, revision, 1, original['rowid'], 'committed', canonical(assessment)))
            self.graph.db.execute('INSERT INTO edge_commit_checkpoints(pass_id,cursor,revision,done,body) VALUES(?,?,?,?,?)',
                (1, original['rowid'], revision, 1, '{}'))
        report = self.workspace.report(request['id'])
        self.assertEqual(report['investigation']['state'], 'report_ready_partial')
        self.assertEqual(report['analysis']['observed_relationships'], 3)
        self.assertEqual(report['analysis']['raw_relationships'], 1)
        self.assertEqual([row['reason'] for row in report['analysis']['muse']], ['model choice'])
        self.assertFalse(report['report']['complete_history'])
        self.assertFalse(report['coverage']['complete'])
        self.assertFalse(report['coverage']['current_balance_verified'])
        self.assertFalse(report['coverage']['private_ledger_available'])

    def test_report_truncation_survives_filtering_invalid_rows(self):
        wallet = key(1)
        request = self.workspace.submit(wallet)
        with self.graph.db:
            for ordinal in range(601):
                self.graph.edge(wallet, wallet, key(ordinal+2), 'provider_lead', ordinal, None,
                                dead_branch=ordinal == 600)
        report = self.workspace.report(request['id'])
        self.assertTrue(report['graph']['view_truncated'])
        self.assertLessEqual(len(report['graph']['edges']), 600)

    def test_stale_workers_are_not_shown_as_active(self):
        atomic(self.graph.out/'workers.json', dict(configured=10, active=10, workers=[],
                                                 updated_ms=time.time_ns()//1000000-91000))
        snapshot = self.workspace.snapshot()
        self.assertEqual(snapshot['workers']['active'], 0)
        self.assertTrue(snapshot['workers']['stale'])
        self.assertFalse(snapshot['coverage']['complete_solana_history'])
        self.assertFalse(snapshot['coverage']['private_cex_ledgers'])


if __name__ == '__main__':
    unittest.main()
