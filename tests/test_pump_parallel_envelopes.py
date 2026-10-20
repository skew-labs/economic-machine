"""Regression tests: one malformed provider reply must not lose other lanes.

All model calls are mocked. No network, credentials or service operations.
"""
import hashlib
import json
import threading
import time
from unittest.mock import patch

from test_pump_parallel_workspace import GraphCase, response
from economic_machine.pump_research.store import canonical
from economic_machine.pump_research.wallet_map import MODEL, UncappedMuse, select_parallel


class EnvelopeRegressionTests(GraphCase):
    def setUp(self):
        super().setUp()
        self.budget = UncappedMuse(self.graph.out / 'test-model.sqlite3', dict(
            authorization='owner_unlimited_muse_top10_forward_wallet_research',
            maximum_usd=None, max_calls=None, model=MODEL, trade_authority=False,
            deadline_ms=time.time_ns() // 1_000_000 + 600_000))
        self.addCleanup(self.budget.db.close)
        root = self.root()
        for _ in range(4):
            self.job(root)

    def mixed(self, bad):
        barrier = threading.Barrier(2, timeout=10)

        def provider(req, credential):
            lane = json.loads(req['messages'][1]['content'])['planner_lane']['lane']
            barrier.wait()
            if lane == 0:
                if isinstance(bad, Exception):
                    raise bad
                return bad
            return response(req)

        with patch('economic_machine.pump_research.wallet_map.provider_call', side_effect=provider):
            jobs = select_parallel(self.graph, self.budget, 'unused-never-read', self.scope, workers=2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.budget.totals()['calls_reserved'], 2)
        self.assertEqual(self.budget.totals()['accepted_responses'], 1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 1)
        record = self.graph.state('parallel_last_batch')
        self.assertEqual(record['dispatched'], 2)
        self.assertEqual(len(record['errors']), 1)
        workers = json.loads((self.graph.out / 'workers.json').read_bytes())
        self.assertEqual(workers['active'], 0)
        self.assertEqual(sorted(w['state'] for w in workers['workers']), ['error', 'planned'])
        return self.budget.db.execute('SELECT id,status,actual,response_hash FROM calls ORDER BY id').fetchall()

    def retained_unknown_response(self, raw, row):
        ident, status, actual, response_hash = row
        self.assertEqual(status, 'received_rejected_cost_unknown')
        self.assertIsNone(actual)
        self.assertEqual(response_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual((self.graph.out / f'muse-response-{ident:06}.raw').read_bytes(), raw.encode())
        self.assertEqual(self.budget.totals()['responses_received'], 2)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 1)

    def test_empty_choices_rejects_one_lane_and_drains_valid_reply(self):
        raw = canonical(dict(model=MODEL, choices=[], usage=dict(cost='0.00003')))
        rows = self.mixed(raw)
        self.assertEqual(rows[0][1:3], ('rejected', '0.00003'))
        self.assertEqual(rows[1][1:3], ('valid', '0.00001'))
        self.assertEqual(self.budget.totals()['responses_received'], 2)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 0)
        self.assertEqual(rows[0][3], hashlib.sha256(raw.encode()).hexdigest())

    def test_malformed_json_keeps_received_raw_bytes_and_unknown_cost(self):
        raw = '{"model":"' + MODEL + '","choices":['
        rows = self.mixed(raw)
        self.retained_unknown_response(raw, rows[0])

    def test_top_level_list_does_not_abort_other_response(self):
        raw = '[]'
        rows = self.mixed(raw)
        self.retained_unknown_response(raw, rows[0])

    def test_duplicate_keys_keep_original_bytes_without_trusting_cost(self):
        raw = '{"model":"' + MODEL + '","model":"other","choices":[],"usage":{"cost":"0.00003"}}'
        rows = self.mixed(raw)
        self.retained_unknown_response(raw, rows[0])

    def test_unexpected_provider_exception_does_not_lose_valid_future(self):
        rows = self.mixed(RuntimeError('synthetic unexpected provider failure'))
        self.assertEqual(rows[0][1:], ('reserved', None, None))
        self.assertEqual(rows[1][1:3], ('valid', '0.00001'))
        self.assertEqual(self.budget.totals()['responses_received'], 1)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 1)
        self.assertFalse((self.graph.out / f'muse-response-{rows[0][0]:06}.raw').exists())

    def test_all_malformed_still_reconciles_received_bytes_before_batch_failure(self):
        raw = '{malformed-json'
        with patch('economic_machine.pump_research.wallet_map.provider_call', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'MAP_MODEL_PARALLEL_ALL_FAILED'):
                select_parallel(self.graph, self.budget, 'unused-never-read', self.scope, workers=2)
        rows = self.budget.db.execute('SELECT id,status,actual,response_hash FROM calls ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2)
        for ident, status, actual, response_hash in rows:
            self.assertEqual(status, 'received_rejected_cost_unknown')
            self.assertIsNone(actual)
            self.assertEqual(response_hash, hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual((self.graph.out / f'muse-response-{ident:06}.raw').read_bytes(), raw.encode())
        self.assertEqual(self.budget.totals()['responses_received'], 2)
        self.assertEqual(self.budget.totals()['accepted_responses'], 0)
        self.assertEqual(self.budget.totals()['unconfirmed_cost_reservations'], 2)
        self.assertEqual(len(self.graph.state('parallel_last_batch')['errors']), 2)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM investigations').fetchone()[0], 0)
