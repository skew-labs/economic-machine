from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.muse import (
    MODEL, MuseFleet, canonical, validate_summary, validate_investigation,
)
from economic_machine.arbitrum_research.governor import conservative_proposal, govern
from economic_machine.pump_research.research_spend import Spend
from test_arbitrum_governor import metrics
from test_pump_research_spend import setup_budget


def summary(n=0):
    return dict(job_id='graph:' + str(n), kind='graph', source_ids=['block:' + str(n)],
                records=[dict(block_number=n, from_address='0x' + 'a' * 40,
                              to_address='0x' + 'b' * 40, value_wei=str(n))])


def answer(item):
    return dict(job_id=item['job_id'], source_ids=item['source_ids'], assessment='hypothesis', priority=70,
                hypothesis='The supplied public transfer suggests a follow-up address-history query.',
                next_checks=['fetch_traces', 'verify_source'],
                remaining_gaps=['Independent canonical finality and private exchange ledgers are unavailable.'])


def envelope(content, cost='0.00001', **changes):
    value = dict(model=MODEL, choices=[dict(finish_reason='stop', message=dict(content=canonical(content)))])
    if cost is not None: value['usage'] = {'cost': cost}
    value.update(changes)
    return canonical(value)


class MuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.shared = self.base / 'shared'
        self.shared.mkdir()
        setup_budget(self.shared)
        self.fleet = MuseFleet(self.base / 'arb', self.shared, self.shared / 'private/openrouter.key')

    def respond(self, req, _):
        payload = json.loads(req['messages'][1]['content'])['evidence']
        return envelope(answer(payload))

    def test_30_distinct_calls_share_real_budget_and_journal(self):
        barrier = threading.Barrier(30)
        def call(req, path):
            self.assertEqual(req['model'], MODEL)
            self.assertFalse(req['provider']['allow_fallbacks'])
            barrier.wait(timeout=20)
            return self.respond(req, path)
        with patch('economic_machine.arbitrum_research.muse.provider_call', side_effect=call):
            result = self.fleet.investigate([summary(i) for i in range(30)])
        self.assertEqual(result['completed'], 30)
        self.assertEqual(result['observed_peak_provider_calls'], 30)
        self.assertEqual(result['totals']['valid_responses'], 30)
        self.assertEqual(Decimal(result['totals']['actual_reported_cost_usd']), Decimal('.0003'))
        with closing(Spend(self.shared)) as spend:
            rows = spend.db.execute("SELECT key,krw FROM operations WHERE kind='model'").fetchall()
        self.assertEqual(len(rows), 30)
        self.assertTrue(all(key.startswith('arb:') for key, _ in rows))

    def test_manager_has_separate_role_and_exact_metric_source(self):
        m = metrics()
        def call(req, _):
            payload = json.loads(req['messages'][1]['content'])['evidence']
            return envelope(conservative_proposal(payload))
        with patch('economic_machine.arbitrum_research.muse.provider_call', side_effect=call):
            result = self.fleet.manage(m)
        self.assertEqual(result['status'], 'valid')
        self.assertEqual(result['totals']['manager_calls'], 1)
        self.assertEqual(govern(result['proposal'], m)['scope'], 'isolated_arbitrum_research_only')

    def test_unknown_provider_cost_is_reserved_not_free(self):
        with patch('economic_machine.arbitrum_research.muse.provider_call', side_effect=TimeoutError('secret provider body')):
            result = self.fleet.investigate([summary()])
        self.assertEqual(result['totals']['errors'], 1)
        self.assertEqual(result['totals']['unknown_cost_calls'], 1)
        self.assertGreater(Decimal(result['totals']['unknown_cost_reserved_usd']), 0)
        self.assertNotIn('secret provider body', canonical(result))

    def test_received_response_without_cost_distinguished(self):
        with patch('economic_machine.arbitrum_research.muse.provider_call', return_value=envelope(answer(summary()), None)):
            result = self.fleet.investigate([summary()])
        self.assertEqual(result['totals']['responses_received'], 1)
        self.assertEqual(result['totals']['valid_responses'], 1)
        self.assertEqual(result['totals']['unknown_cost_calls'], 1)

    def test_invented_source_rejected_but_cost_recorded(self):
        invalid = answer(summary())
        invalid['source_ids'] = ['block:invented']
        with patch('economic_machine.arbitrum_research.muse.provider_call', return_value=envelope(invalid)):
            result = self.fleet.investigate([summary()])
        self.assertEqual(result['totals']['rejected_responses'], 1)
        self.assertEqual(result['totals']['unknown_cost_calls'], 0)
        self.assertIsNone(result['results'][0]['proposal'])

    def test_cost_breach_blocks_future_attempts(self):
        with patch('economic_machine.arbitrum_research.muse.provider_call', return_value=envelope(answer(summary()), '1')) as mocked:
            first = self.fleet.investigate([summary()])
            second = self.fleet.investigate([summary(1)])
        self.assertTrue(first['totals']['cost_breach'])
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(second['results'][0]['status'], 'admission_rejected')

    def test_shared_monthly_budget_exhaustion_prevents_provider(self):
        with closing(Spend(self.shared)) as spend: spend.reserve('model', 'solana-spend', usd='400')
        with patch('economic_machine.arbitrum_research.muse.provider_call') as mocked:
            result = self.fleet.investigate([summary()])
        mocked.assert_not_called()
        self.assertEqual(result['observed_peak_provider_calls'], 0)
        self.assertEqual(result['totals']['calls_reserved'], 0)
        self.assertEqual(result['totals']['admission_rejected'], 1)

    def test_disallowed_context_never_reaches_provider_or_journal(self):
        item = summary()
        item['records'][0]['api_key'] = 'sensitive'
        with patch('economic_machine.arbitrum_research.muse.provider_call') as mocked, self.assertRaisesRegex(ValueError, 'SENSITIVE'):
            self.fleet.investigate([item])
        mocked.assert_not_called()
        self.assertEqual(self.fleet.totals()['calls_recorded'], 0)

    def test_duplicate_evidence_and_duplicate_job_not_parallelized(self):
        for jobs in ([summary(), summary()], [summary(), dict(summary(), job_id='different')]):
            with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
                self.fleet.investigate(jobs)

    def test_restart_retains_unknown_reservations(self):
        with patch('economic_machine.arbitrum_research.muse.provider_call', side_effect=TimeoutError()):
            self.fleet.investigate([summary()])
        restarted = MuseFleet(self.base / 'arb', self.shared, self.shared / 'private/openrouter.key')
        self.assertEqual(restarted.totals(), self.fleet.totals())

    def test_interrupted_two_ledger_admission_recovers_as_unknown(self):
        with closing(self.fleet._db()) as db, db:
            db.execute("INSERT INTO calls(id,role,reserved,status) VALUES('crash','investigator','.001','preparing')")
        with closing(Spend(self.shared)) as spend: spend.reserve('model', 'arb:crash', usd='.001')
        restarted = MuseFleet(self.base / 'arb', self.shared, self.shared / 'private/openrouter.key')
        self.assertEqual(restarted.totals()['unknown_cost_calls'], 1)
        self.assertEqual(restarted.totals()['committed_or_reserved_usd'], '0.001')

    def test_received_cost_reconciled_after_shared_finish_crash(self):
        with patch('economic_machine.arbitrum_research.muse.provider_call', side_effect=self.respond), \
                patch.object(Spend, 'model_finish', side_effect=RuntimeError('crash')):
            result = self.fleet.investigate([summary()])
        self.assertEqual(result['results'][0]['status'], 'worker_error')
        with closing(Spend(self.shared)) as spend:
            before = spend.totals()['operations_and_models_krw']
        MuseFleet(self.base / 'arb', self.shared, self.shared / 'private/openrouter.key')
        with closing(Spend(self.shared)) as spend:
            after = spend.totals()['operations_and_models_krw']
        self.assertGreater(before, after)
        self.assertEqual(after, 1)

    def test_schema_rejects_executable_actions_and_false_confirmed_status(self):
        for changes in (dict(next_checks=['run_shell']), dict(assessment='confirmed_owner'),
                        dict(hypothesis='Use https://example.com'), dict(source_ids=[{}]), dict(priority=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_investigation(dict(answer(summary()), **changes), summary())

    def test_truncated_model_fallback_or_nan_cost_are_rejected(self):
        no_identity = json.loads(envelope(answer(summary())))
        no_identity.pop('model')
        malformed = [canonical(no_identity), envelope(answer(summary()), model='different-model'),
                     envelope(answer(summary()), 'NaN'),
                     envelope(answer(summary()), choices=[dict(finish_reason='length', message=dict(content=canonical(answer(summary()))))])]
        for raw in malformed:
            with patch('economic_machine.arbitrum_research.muse.provider_call', return_value=raw):
                result = self.fleet.investigate([summary()])
            self.assertEqual(result['results'][0]['status'], 'rejected')


if __name__ == '__main__': unittest.main()
