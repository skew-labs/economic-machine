import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

from economic_machine.arbitrum_research.graph import Graph
from economic_machine.arbitrum_research.research_pool_adapter import enqueue_jobs, consume_results, _read_db
from economic_machine.arbitrum_research.runtime import Runtime
from test_arbitrum_runtime import page, measured


class Queue:
    def __init__(self): self.jobs = []; self.keys = set(); self.outcomes = {}
    def enqueue(self, chain, summary, metadata, key):
        if key in self.keys: return None
        self.keys.add(key)
        self.jobs.append(dict(id=len(self.jobs) + 1, chain=chain, summary=summary, metadata=metadata, proposal=None))
        return len(self.jobs)
    def pending_count(self, chain):
        return sum(j['chain'] == chain and j['id'] not in self.outcomes for j in self.jobs)
    def active_evidence_keys(self, chain):
        return {j['metadata']['evidence_key'] for j in self.jobs if j['chain'] == chain and j['id'] not in self.outcomes}
    def completed(self, chain, limit):
        return [j for j in self.jobs if j['chain'] == chain and j['proposal'] and j['id'] not in self.outcomes][:limit]
    def ack(self, job_id, outcome): self.outcomes[job_id] = outcome
    def finish(self, checks):
        for job in self.jobs:
            summary = job['summary']
            job['proposal'] = dict(job_id=summary['job_id'], source_ids=summary['source_ids'], assessment='coverage_gap',
                priority=60, hypothesis='Check the retained records without assuming complete history.',
                next_checks=checks, remaining_gaps=['Independent complete history remains unavailable.'])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arb = Path(self.tmp.name) / 'arb'; self.rh = Path(self.tmp.name) / 'rh'
        self.queue = Queue()
        for base, chain in ((self.arb, 42161), (self.rh, 4663)):
            (base / 'evidence').mkdir(parents=True)
            (base / 'index').mkdir()
            p = page(0, 3)
            proof = dict(chain_id=chain, complete_cloud_readback=True, raw_sha256=p['sha256'],
                         cloud=dict(sha256='b' * 64, bytes=512))
            with sqlite3.connect(base / 'evidence/runtime.sqlite3') as db:
                db.executescript('CREATE TABLE state(id INTEGER PRIMARY KEY,body TEXT);CREATE TABLE pages(start INTEGER PRIMARY KEY,end INTEGER,sha TEXT,proof TEXT,state TEXT);')
                db.execute('INSERT INTO state VALUES(1,?)', (json.dumps(dict(shard_start=0)),))
                db.execute('INSERT INTO pages VALUES(?,?,?,?,?)', (0, 3, p['sha256'], json.dumps(proof), 'indexed'))
            graph = Graph(base / 'index/000000000000.sqlite3', chain_id=chain)
            graph.ingest(p['blocks'], p['sha256'], 0, 3, proof); graph.close()
    def tearDown(self): self.tmp.cleanup()

    def test_distinct_real_evidence_jobs_and_terminal_dedup(self):
        result = enqueue_jobs(self.queue, self.arb, self.rh)
        self.assertEqual(len(self.queue.jobs), 12)
        self.assertEqual(result['offered'], 12)
        self.assertEqual({j['chain'] for j in self.queue.jobs}, {'42161', '4663'})
        self.assertTrue(all(j['summary']['records']['sample']['transactions'] for j in self.queue.jobs))
        self.queue.finish(['retain_gap']); consume_results(self.queue, self.arb, self.rh)
        enqueue_jobs(self.queue, self.arb, self.rh)
        self.assertEqual(len(self.queue.jobs), 12)

    def test_backpressure_bounds_distinct_jobs(self):
        enqueue_jobs(self.queue, self.arb, self.rh, max_pending=8)
        self.assertEqual(len(self.queue.jobs), 8)
        enqueue_jobs(self.queue, self.arb, self.rh, max_pending=8)
        self.assertEqual(len(self.queue.jobs), 8)

    def test_native_checks_query_actual_rows_without_source_mutation(self):
        paths = [self.arb / 'evidence/runtime.sqlite3', self.arb / 'index/000000000000.sqlite3']
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        enqueue_jobs(self.queue, self.arb, self.rh)
        self.queue.finish(['verify_source', 'fetch_blocks', 'follow_address', 'decode_protocol', 'fetch_receipts', 'fetch_traces', 'verify_bridge', 'retain_gap'])
        result = consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(result['consumed'], 12)
        self.assertEqual(result['mismatches'], 0)
        outcome = self.queue.outcomes[1]
        checks = {r['check']: r for r in outcome['checks']}
        self.assertEqual(checks['fetch_blocks']['checked_blocks'], 4)
        self.assertEqual(checks['fetch_blocks']['status'], 'LOCAL_HEADER_RANGE_MATCH')
        self.assertTrue(checks['follow_address']['records'][0]['outgoing'])
        self.assertEqual(checks['fetch_receipts']['status'], 'WAITING_SOURCE')
        self.assertFalse(checks['verify_source']['evidence']['fresh_blob_readback_performed'])
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_changed_page_hash_rejected_before_checks(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        with sqlite3.connect(self.arb / 'evidence/runtime.sqlite3') as db:
            db.execute('UPDATE pages SET sha=?', ('c' * 64,))
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'EVIDENCE_REJECTED')

    def test_chain_identity_mismatch_rejected(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        self.queue.jobs[0]['metadata']['chain_id'] = 4663
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'EVIDENCE_REJECTED')

    def test_model_cannot_redirect_query_paths(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        self.queue.jobs[0]['metadata']['graph_relative'] = '../rh/index/000000000000.sqlite3'
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'EVIDENCE_REJECTED')

    def test_rotated_graph_records_waiting_source(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        (self.arb / 'index/000000000000.sqlite3').rename(self.arb / 'index/archived.sqlite3')
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'WAITING_SOURCE')

    def test_invalid_model_proposal_cannot_trigger_queries(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        self.queue.jobs[0]['proposal']['next_checks'] = ['shell_command']
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'EVIDENCE_REJECTED')

    def test_source_without_verified_readback_receipt_not_offered(self):
        with sqlite3.connect(self.arb / 'evidence/runtime.sqlite3') as db:
            row = json.loads(db.execute('SELECT proof FROM pages').fetchone()[0])
            row['complete_cloud_readback'] = False
            db.execute('UPDATE pages SET proof=?', (json.dumps(row),))
        result = enqueue_jobs(self.queue, self.arb, self.rh)
        self.assertEqual(result['chains']['42161']['state'], 'SOURCE_EVIDENCE_REJECTED')
        self.assertEqual({j['chain'] for j in self.queue.jobs}, {'4663'})

    def test_consumer_share_prevents_robinhood_starvation(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['verify_source'])
        consume_results(self.queue, self.arb, self.rh, max_jobs=4)
        self.assertEqual({j['chain'] for j in self.queue.jobs if j['id'] in self.queue.outcomes}, {'42161', '4663'})

    def test_broken_parent_is_recorded_as_mismatch(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        with sqlite3.connect(self.arb / 'index/000000000000.sqlite3') as db:
            db.execute('UPDATE blocks SET parent_hash=? WHERE number=2', ('0x' + 'f' * 64,))
        result = consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'EVIDENCE_MISMATCH')
        self.assertGreater(result['mismatches'], 0)

    def test_local_evidence_path_cannot_be_symlink(self):
        enqueue_jobs(self.queue, self.arb, self.rh); self.queue.finish(['fetch_blocks'])
        path = self.arb / 'index/000000000000.sqlite3'
        moved = self.arb / 'index/original.sqlite3'; path.rename(moved); path.symlink_to(moved)
        consume_results(self.queue, self.arb, self.rh)
        self.assertEqual(self.queue.outcomes[1]['status'], 'WAITING_SOURCE')

    def test_reader_is_read_only_and_bounded(self):
        path = self.arb / 'evidence/runtime.sqlite3'
        with _read_db(path) as db:
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM pages')
        with patch('economic_machine.arbitrum_research.research_pool_adapter.MAX_QUERY_SECONDS', 0):
            with _read_db(path) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute('WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<1000000) SELECT sum(n) FROM x').fetchone()

    def test_external_mode_never_calls_inline_investigators(self):
        runtime = Runtime.__new__(Runtime)
        runtime.fleet = Mock()
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}):
            runtime.investigate({})
        runtime.fleet.investigate.assert_not_called()

    def test_external_manager_reuses_native_governor_without_model(self):
        runtime = Runtime.__new__(Runtime)
        runtime.base = self.arb; runtime.out = self.arb / 'evidence'
        runtime.storage = Mock(); runtime.fleet = Mock(); runtime.resource_apply = False
        runtime.state = dict(last_manager_ms=0, head=100, next_block=0, backoff_until=0)
        runtime.save = Mock()
        with patch.dict(os.environ, {'EM_RESEARCH_POOL_ROOT': '/retained/pool'}), \
             patch('economic_machine.arbitrum_research.runtime.metrics', measured):
            runtime.manage(force=True)
        runtime.fleet.manage.assert_not_called()
        self.assertFalse(runtime.state['policy']['manager_response_applied'])
        self.assertEqual(runtime.state['policy']['effective_download_concurrency'], 1)


if __name__ == '__main__': unittest.main()
