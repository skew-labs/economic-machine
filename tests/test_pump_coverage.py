from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.coverage import Coverage, DEADLINE_MS
from economic_machine.pump_research.store import encode


def key(number):
    return encode(number.to_bytes(32, 'big'))


class CoverageTests(unittest.TestCase):
    def setUp(self):
        clock = patch('economic_machine.pump_research.coverage.time.time_ns', return_value=(DEADLINE_MS-1000)*1000000)
        clock.start(); self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'coverage.sqlite3'
        self.guard_calls = []
        self.coverage = Coverage(self.path, guard=lambda: self.guard_calls.append(1))
        self.addCleanup(self.coverage.close)
        self.wallet = key(1)
        self.source = 'epoch685:gsfa-v1'
        self.register()

    def register(self, source=None, start=100, end=199, epoch=685):
        source = source or self.source
        result = self.coverage.register_source(source, kind='public_address_index',
            start_slot=start, end_slot=end, epoch=epoch, evidence_refs=['manifest:sha256-a'])
        self.coverage.register_wallet(self.wallet, source)
        return result

    def test_validated_continuation_writes_after_original_deadline(self):
        from types import SimpleNamespace
        from test_pump_research_continuation import make_continuation_fixture
        from economic_machine.pump_research.research_continuation import ContinuationScope,GIB
        with patch('economic_machine.pump_research.coverage.time.time_ns',return_value=(DEADLINE_MS+1000)*1000000),patch(
                'economic_machine.pump_research.history_core.used_bytes',return_value=0),patch(
                'economic_machine.pump_research.research_continuation.shutil.disk_usage',return_value=SimpleNamespace(free=400*GIB)):
            make_continuation_fixture(self.path.parent)
            scope=ContinuationScope(self.path.parent)
            try:
                self.coverage.guard=scope.guard
                self.advance(cursor={'after_renewal':1})
                self.assertEqual(self.coverage.frontier(self.wallet,self.source)['revision'],1)
            finally:scope.close()

    def test_arbitrary_guard_cannot_remove_original_deadline(self):
        from types import SimpleNamespace
        class Fake:
            continuation=SimpleNamespace(effective_deadline_ms=None,hash='a'*64)
            def guard(self):pass
        self.coverage.guard=Fake().guard
        with patch('economic_machine.pump_research.coverage.time.time_ns',return_value=DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):self.advance()

    def advance(self, **overrides):
        options = dict(cursor={'offset': 1}, state='scanning', expected_revision=0,
                       evidence_refs=['query:page-1'])
        options.update(overrides)
        return self.coverage.advance(self.wallet, self.source, **options)

    def test_scope_registration_is_immutable_and_idempotent(self):
        before = self.coverage.report(self.wallet)
        self.register()
        self.assertEqual(before, self.coverage.report(self.wallet))
        with self.assertRaisesRegex(ValueError, 'SOURCE_SCOPE_CHANGED'):
            self.register(end=299)
        self.assertEqual(self.coverage.report(self.wallet)['sources'][0]['scope']['end_slot'], 199)

    def test_arbitrary_valid_address_and_registered_sources_are_required(self):
        for invalid in ('not-a-wallet', '0x1234', '', None, ['wallet']):
            with self.assertRaisesRegex(ValueError, 'ADDRESS'):
                self.coverage.register_wallet(invalid, self.source)
        with self.assertRaisesRegex(ValueError, 'SOURCE_UNREGISTERED'):
            self.coverage.register_wallet(key(2), 'missing-source')
        result = self.coverage.register_wallet(key(9999999), self.source)
        self.assertEqual(result['state'], 'pending')
        self.assertIsNone(result['latest_slot'])

    def test_available_inspected_and_gap_remain_distinct(self):
        available = self.coverage.record_span(self.wallet, self.source, 'available', 100, 199,
            method='provider_manifest', evidence_refs=['manifest:sha256-a'])
        inspected = self.coverage.record_span(self.wallet, self.source, 'inspected', 100, 120,
            method='address_index_lookup', evidence_refs=['lookup:sha256-b'])
        gap = self.coverage.record_span(self.wallet, self.source, 'gap', 121, 130,
            method='address_index_lookup', evidence_refs=['error:missing-block'], reason='Source block unavailable.')
        report = self.coverage.report(self.wallet)
        self.assertEqual([r['id'] for r in report['spans']['available']], [available])
        self.assertEqual([r['id'] for r in report['spans']['inspected']], [inspected])
        self.assertEqual([r['id'] for r in report['spans']['gap']], [gap])
        self.assertIsNone(report['latest_observed_slot'])
        self.assertFalse(report['coverage_complete'])

    def test_spans_are_idempotent_and_later_inspection_does_not_erase_gap(self):
        arguments = dict(method='address_index_lookup', evidence_refs=['query:1'])
        first = self.coverage.record_span(self.wallet, self.source, 'gap', 110, 115,
            reason='Missing source records.', **arguments)
        self.assertEqual(first, self.coverage.record_span(self.wallet, self.source, 'gap', 110, 115,
            reason='Missing source records.', **arguments))
        self.coverage.record_span(self.wallet, self.source, 'inspected', 110, 115, **arguments)
        self.assertEqual(self.coverage.db.execute('SELECT count(*) FROM coverage_spans').fetchone()[0], 2)
        self.assertEqual(len(self.coverage.report(self.wallet)['spans']['gap']), 1)

    def test_span_and_observation_cannot_escape_source_scope(self):
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_SOURCE_SCOPE'):
            self.coverage.record_span(self.wallet, self.source, 'inspected', 99, 120,
                method='lookup', evidence_refs=['query:1'])
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_SOURCE_SCOPE'):
            self.advance(latest_observed_slot=200)
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_SOURCE_SCOPE'):
            self.coverage.record_link(self.wallet, self.source, key(2), 'outgoing', 200, evidence_refs=['edge:1'])
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 0)

    def test_no_unsupported_complete_state_or_evidence_free_inspection(self):
        with self.assertRaisesRegex(ValueError, 'FRONTIER_STATE'):
            self.advance(state='complete')
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_REQUIRED'):
            self.coverage.record_span(self.wallet, self.source, 'inspected', 100, 110,
                method='lookup', evidence_refs=[])
        with self.assertRaisesRegex(ValueError, 'GAP_REASON'):
            self.coverage.record_span(self.wallet, self.source, 'gap', 100, 110,
                method='lookup', evidence_refs=['query:1'])
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_REQUIRED'):
            self.advance(latest_observed_slot=110, evidence_refs=[])

    def test_cursor_cas_replay_preserves_journal_and_original_cursor(self):
        first = self.advance()
        replay = self.advance()
        self.assertEqual(first['transition_id'], replay['transition_id'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.coverage.db.execute('SELECT count(*) FROM coverage_transitions').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'STALE_REVISION'):
            self.advance(cursor={'offset':2})
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['cursor'], {'offset':1})
        self.coverage.register_wallet(self.wallet, self.source)
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 1)

    def test_error_transition_keeps_cursor_and_retry_does_not_reset_it(self):
        self.advance()
        self.advance(state='failed', expected_revision=1, reason='Temporary source failure.')
        with self.assertRaisesRegex(ValueError, 'CURSOR_RESET'):
            self.advance(cursor=None, expected_revision=2)
        self.advance(cursor={'offset':2}, expected_revision=2)
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 3)
        self.assertEqual(self.coverage.db.execute('SELECT count(*) FROM coverage_transitions').fetchone()[0], 3)

    def test_exhausted_epoch_never_claims_complete_wallet_lifetime(self):
        self.advance(state='exhausted_source')
        report = self.coverage.report(self.wallet)
        self.assertEqual(report['sources'][0]['state'], 'exhausted_source')
        for field in ('coverage_complete', 'entire_wallet_lifetime_verified', 'current_balance_verified',
                      'independent_consensus_verified', 'same_funds_continuity_proven'):
            self.assertFalse(report[field])
        self.assertFalse(report['sources'][0]['entire_source_inspected'])
        with self.assertRaisesRegex(ValueError, 'SOURCE_EXHAUSTED'):
            self.advance(expected_revision=1, cursor={'offset':2})
        self.register(source='epoch686:gsfa-v1', start=200, end=299, epoch=686)
        self.assertEqual(len(self.coverage.report(self.wallet)['sources']), 2)

    def test_latest_observed_slot_is_monotonic_and_keeps_provenance(self):
        self.advance(latest_observed_slot=150, evidence_refs=['transaction:150'])
        self.advance(cursor={'offset':2}, expected_revision=1, latest_observed_slot=120,
                     evidence_refs=['transaction:120'])
        report = self.coverage.report(self.wallet)
        self.assertEqual(report['latest_observed_slot'], 150)
        self.assertEqual(report['latest_observation_refs'], ['transaction:150'])
        self.assertEqual(report['latest_observation_source'], self.source)
        self.assertFalse(report['current_balance_verified'])

    def test_incoming_outgoing_trace_preserves_refs_without_same_fund_inference(self):
        incoming = self.coverage.record_link(self.wallet, self.source, key(2), 'incoming', 120,
            evidence_refs=['edge:incoming'])
        outgoing = self.coverage.record_link(self.wallet, self.source, key(3), 'outgoing', 130,
            evidence_refs=['edge:outgoing'], parent_evidence_refs=['edge:incoming'])
        self.assertEqual(outgoing, self.coverage.record_link(self.wallet, self.source, key(3), 'outgoing', 130,
            evidence_refs=['edge:outgoing'], parent_evidence_refs=['edge:incoming']))
        report = self.coverage.report(self.wallet)
        self.assertEqual(report['trace_evidence']['incoming'][0]['id'], incoming)
        link = report['trace_evidence']['outgoing'][0]
        self.assertEqual(link['parent_evidence_refs'], ['edge:incoming'])
        self.assertFalse(link['same_funds_continuity_proven'])
        self.assertFalse(link['common_owner_proven'])
        self.assertEqual(report['latest_observed_slot'], 130)
        self.assertEqual(report['latest_observation_refs'], ['edge:outgoing'])
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 0)

    def test_invalid_direction_and_bad_refs_do_not_write_links(self):
        with self.assertRaisesRegex(ValueError, 'LINK_DIRECTION'):
            self.coverage.record_link(self.wallet, self.source, self.wallet, 'outgoing', 120, evidence_refs=['edge:1'])
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_REFS'):
            self.coverage.record_link(self.wallet, self.source, key(2), 'outgoing', 120,
                evidence_refs=['https://provider/?secret=must-not-store'])
        self.assertEqual(self.coverage.db.execute('SELECT count(*) FROM coverage_links').fetchone()[0], 0)

    def test_read_only_report_is_bounded_and_does_not_change_frontier(self):
        self.advance(cursor={'long_cursor':'a'*1500})
        for slot in range(100, 105):
            self.coverage.record_span(self.wallet, self.source, 'inspected', slot, slot,
                method='lookup', evidence_refs=['query:'+str(slot)])
            self.coverage.record_link(self.wallet, self.source, key(slot), 'outgoing', slot,
                evidence_refs=['edge:'+str(slot)])
        before = self.coverage.frontier(self.wallet, self.source)
        reader = Coverage(self.path, read_only=True)
        self.addCleanup(reader.close)
        result = reader.report(self.wallet, limit=2)
        self.assertEqual(len(result['spans']['inspected']), 2)
        self.assertTrue(result['span_list_truncated'])
        self.assertTrue(result['trace_list_truncated'])
        self.assertTrue(result['sources'][0]['cursor']['truncated'])
        self.assertEqual(result['latest_observed_slot'], 104)
        self.assertEqual(before, self.coverage.frontier(self.wallet, self.source))
        with self.assertRaisesRegex(ValueError, 'READ_ONLY'):
            reader.register_wallet(key(2), self.source)

    def test_deadline_stops_writes_but_keeps_reports_readable(self):
        with patch('economic_machine.pump_research.coverage.time.time_ns', return_value=DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError, 'DEADLINE'):
                self.advance()
            reader = Coverage(self.path, read_only=True)
            try:
                self.assertFalse(reader.report(self.wallet)['coverage_complete'])
            finally:
                reader.close()
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 0)

    def test_existing_scope_guard_is_required_and_cannot_be_bypassed(self):
        with self.assertRaisesRegex(ValueError, 'SCOPE_GUARD_REQUIRED'):
            Coverage(self.path.parent/'unguarded.sqlite3')
        def fail():
            raise ValueError('EXISTING_STORAGE_LIMIT')
        self.coverage.guard = fail
        with self.assertRaisesRegex(ValueError, 'EXISTING_STORAGE_LIMIT'):
            self.advance()
        self.assertEqual(self.coverage.frontier(self.wallet, self.source)['revision'], 0)
        self.assertFalse((self.path.parent/'unguarded.sqlite3').exists())

    def test_unregistered_wallet_is_unknown_and_cursors_and_reports_have_bounds(self):
        report = self.coverage.report(key(2))
        self.assertEqual(report['sources'], [])
        self.assertIsNone(report['latest_observed_slot'])
        self.assertFalse(report['coverage_complete'])
        for limit in (0,129,True):
            with self.assertRaisesRegex(ValueError, 'REPORT_BOUND'):
                self.coverage.report(self.wallet, limit=limit)
        with self.assertRaisesRegex(ValueError, 'CURSOR_BOUND'):
            self.advance(cursor='a'*4097)


if __name__ == '__main__':
    unittest.main()
