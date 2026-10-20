import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.research_workspace import Workspace
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.commit_graph import _schema, _revision
from economic_machine.pump_research.address_index import AddressIndex


def key(number):
    return encode(number.to_bytes(32, 'big'))


class WorkspaceReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        out = self.base/'evidence/wallet-map-v1'
        out.mkdir(parents=True)
        self.workspace = Workspace(self.base)
        self.addCleanup(self.workspace.close)
        self.db = sqlite3.connect(out/'graph.sqlite3')
        self.addCleanup(self.db.close)
        self.index = AddressIndex(out/'address-index.sqlite3')
        self.addCleanup(self.index.close)
        self.db.executescript('''
            CREATE TABLE edges(id TEXT PRIMARY KEY, root TEXT, source TEXT,
              target TEXT, kind TEXT, slot INTEGER, stamp INTEGER, body TEXT);
            CREATE TABLE frontier(id TEXT PRIMARY KEY, root TEXT, state TEXT);
            CREATE TABLE investigations(id INTEGER PRIMARY KEY, call_id INTEGER,
              body TEXT, at INTEGER);
            CREATE TABLE nodes(id TEXT PRIMARY KEY, kind TEXT, roles TEXT);
        ''')
        self.wallet = key(1)
        self.request = self.workspace.submit(self.wallet)
        self.sequence = 0

    def edge(self, sender=None, target=None, kind='indexed_transfer', root=None,
             slot=400000000, stamp=None, **evidence):
        self.sequence += 1
        ident = 'edge-'+str(self.sequence)
        with self.db:
            self.db.execute('INSERT INTO edges VALUES(?,?,?,?,?,?,?,?)',
                (ident, root or self.wallet, sender or self.wallet, target or key(2),
                 kind, slot, stamp, canonical(evidence)))
        return ident

    def report(self):
        return self.workspace.report(self.request['id'])

    def advance_index_revision(self):
        """Append source metadata so existing assessments become stale."""
        with self.index.db:
            self.index.db.execute('INSERT INTO tx_commit_evidence(tx,version,body) VALUES(?,?,?)',
                (self.index.db.execute('SELECT count(*)+1 FROM tx_commit_evidence').fetchone()[0], 1,
                 canonical(dict(synthetic_presentation_fixture=True))))

    def fixture_assessment(self, ident, status='committed', **fields):
        """Explicit synthetic ledger for presentation tests, never raw proof."""
        _schema(self.db)
        revision = _revision(self.index.db)[0]
        rowid, body = self.db.execute('SELECT rowid,body FROM edges WHERE id=?', (ident,)).fetchone()
        evidence = json.loads(body)
        assessment = dict(edge=ident, status=status, instruction_commit_status=status,
            assessment_revision=revision, assessment_current=True,
            edge_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            reason='synthetic_presentation_fixture', transaction_success=True,
            source_sha256='a'*64, **fields)
        if evidence.get('transfer_kind') is not None and 'transfer_kind' not in fields:
            assessment['transfer_kind'] = evidence['transfer_kind']
        with self.db:
            self.db.execute('INSERT INTO edge_commit_assessments(edge,revision,pass_id,edge_rowid,status,body) VALUES(?,?,?,?,?,?)',
                            (ident, revision, 1, rowid, status, canonical(assessment)))
            self.db.execute('INSERT INTO edge_commit_checkpoints(pass_id,cursor,revision,done,body) VALUES(?,?,?,?,?)',
                            (1, rowid, revision, 1, '{}'))

    def assessed_edge(self, **kwargs):
        ident = self.edge(**kwargs)
        self.fixture_assessment(ident)
        return ident

    def transfers(self):
        return self.report()['report']['details']['direct_transfers']

    def raw(self, **overrides):
        return dict(dict(signature='signature-1', ix=1, source='sparse_archive',
                         symbol='SOL', amount='9', transfer_kind='system_transfer'),
                    **overrides)

    def test_preserves_large_integer_and_provider_units_without_valuation(self):
        self.assessed_edge(**self.raw(amount='9007199254740993123456789'))
        self.assessed_edge(kind='funding_record', amount='1.000000000000000001', symbol='SOL',
                  source='core_arkm_funding')
        report = self.report()
        totals = report['analysis']['details']['direct_transfers']['counterparty_unit_totals']
        by_unit = {r['unit']: r['amount'] for r in totals}
        self.assertEqual(by_unit, {'lamports': '9007199254740993123456789',
                                 'provider_reported_SOL_units': '1.000000000000000001'})
        self.assertNotIn('usd_value', canonical(totals))
        self.assertTrue(report['analysis']['details']['direct_transfers']['provider_observations_may_overlap_raw_records'])

    def test_sums_compatible_units_exactly_and_keeps_unknown_token_decimals(self):
        self.assessed_edge(**self.raw(amount='9007199254740993'))
        self.assessed_edge(**self.raw(signature='signature-2', amount='2'))
        self.assessed_edge(**self.raw(signature='signature-3', symbol=key(99), amount='123456789'))
        totals = self.transfers()['counterparty_unit_totals']
        self.assertEqual(next(v['amount'] for v in totals if v['unit']=='lamports'), '9007199254740995')
        token = next(v for v in totals if v['unit']=='token_base_units')
        self.assertEqual(token['amount'], '123456789')
        self.assertNotIn('decimals', token)

    def test_direct_directions_do_not_attribute_downstream_funds_to_wallet(self):
        self.assessed_edge(sender=key(3), target=self.wallet, **self.raw(signature='incoming'))
        self.assessed_edge(**self.raw(signature='outgoing'))
        self.assessed_edge(target=self.wallet, **self.raw(signature='self'))
        self.assessed_edge(sender=key(2), target=key(4), **self.raw(signature='downstream'))
        transfer = self.transfers()
        self.assertEqual((transfer['incoming_evidence_items'], transfer['outgoing_evidence_items'],
                          transfer['self_evidence_items'], transfer['related_downstream_records']), (1,1,1,1))
        self.assertNotIn(key(4), [v['counterparty'] for v in transfer['counterparty_unit_totals']])

    def test_signed_instruction_corroboration_deduplicates_only_derived_totals(self):
        self.assessed_edge(root=key(10), **self.raw())
        self.assessed_edge(root=key(11), kind='raw_archive_transfer', **self.raw())
        report = self.report()
        transfer = report['report']['details']['direct_transfers']
        self.assertEqual(report['analysis']['observed_relationships'], 2)
        self.assertEqual(len(report['graph']['edges']), 2)
        self.assertEqual(transfer['duplicate_signed_instruction_observations'], 1)
        self.assertEqual(transfer['outgoing_evidence_items'], 1)
        self.assertEqual(transfer['counterparty_unit_totals'][0]['amount'], '9')
        self.assertEqual(self.db.execute('SELECT count(*) FROM edges').fetchone()[0], 2)

    def test_conflicting_instruction_quantities_are_not_summed(self):
        self.assessed_edge(root=key(10), **self.raw(amount='9'))
        self.assessed_edge(root=key(11), **self.raw(amount='10'))
        transfer = self.transfers()
        self.assertEqual(transfer['conflicting_items'], 1)
        self.assertEqual(transfer['counterparty_unit_totals'], [])
        self.assertEqual(len(transfer['conflicts'][0]['record_ids']), 2)

    def test_account_creation_is_separate_and_unspecified_is_not_called_payment(self):
        self.assessed_edge(**self.raw(transfer_kind='create_account', amount='2039280'))
        self.assessed_edge(kind='raw_archive_transfer', **self.raw(signature='unknown', transfer_kind=None))
        transfer = self.transfers()
        self.assertEqual(transfer['account_creation_funding_items'], 1)
        self.assertEqual({v['instruction_class'] for v in transfer['counterparty_unit_totals']},
                         {'account_creation_funding','instruction_kind_unspecified'})
        self.assertIn('account-creation funding', ' '.join(self.report()['report']['findings']))

    def test_time_bounds_keep_missing_and_explicit_unknown_times(self):
        self.assessed_edge(stamp=1705618800000, **self.raw())
        self.assessed_edge(stamp=1792055000000, **self.raw(signature='last'))
        self.assessed_edge(stamp=None, **self.raw(signature='missing'))
        self.assessed_edge(stamp=1792055001000, **self.raw(signature='unknown', timestamp_unknown=True))
        period = self.report()['report']['details']['time_range']
        self.assertEqual(period['known_timestamp_records'], 2)
        self.assertEqual(period['unknown_timestamp_records'], 2)
        self.assertEqual(period['first_known_ms'], 1705618800000)
        self.assertEqual(period['last_known_ms'], 1792055000000)
        self.assertFalse(period['full_interval_covered'])

    def test_cross_token_participation_separates_payer_creator_and_provider_claims(self):
        self.assessed_edge(target=key(10), kind='create', **self.raw(signature='create'))
        self.assessed_edge(target=key(11), kind='later_buy', **self.raw(signature='buy'))
        self.assessed_edge(sender=key(2), target=key(12), kind='launch_payer', **self.raw(signature='payer'))
        self.assessed_edge(sender=key(2), target=key(13), kind='early_buy', **self.raw(signature='other-buy'))
        self.assessed_edge(target=key(14), kind='provider_creator_claim', source='provider')
        self.assessed_edge(target=key(15), kind='dataset_early_buy', source='sample')
        activity = self.report()['report']['details']['token_participation']
        wallet = next(a for a in activity['actors'] if a['requested_wallet'])
        self.assertEqual(wallet['distinct_tokens'], 2)
        self.assertFalse(wallet['same_funds_proven'])
        related = next(a for a in activity['actors'] if not a['requested_wallet'])
        payer = next(t for t in related['tokens'] if t['token']==key(12))
        self.assertEqual(payer['roles'], ['launch_payer'])
        self.assertEqual(len(activity['provider_claims']), 2)
        self.assertNotIn(key(14), [t['token'] for t in wallet['tokens']])

    def test_failed_records_never_become_successful_transfer_or_reuse_findings(self):
        self.assessed_edge(**self.raw(success=False))
        self.assessed_edge(target=key(10), kind='create', **self.raw(signature='failed-create', err={'InstructionError':1}))
        self.assessed_edge(target=key(11), kind='later_buy', **self.raw(signature='single-buy'))
        details = self.report()['report']['details']
        self.assertEqual(details['direct_transfers']['failed_records_excluded'], 1)
        self.assertEqual(details['direct_transfers']['counterparty_unit_totals'], [])
        self.assertFalse(details['token_participation']['actors'][0]['repeated_address_participation'])

    def test_invalid_or_missing_quantities_stay_visible_without_invented_totals(self):
        for index, amount in enumerate((None, 'NaN', 'Infinity', '-1', '1.5', True, '1e1000000')):
            self.assessed_edge(**self.raw(signature='invalid-'+str(index), amount=amount))
        transfer = self.transfers()
        self.assertEqual(transfer['quantity_unavailable_items'], 7)
        self.assertEqual(transfer['counterparty_unit_totals'], [])
        self.assertEqual(len(self.report()['graph']['edges']), 7)

    def test_muse_660_661_remain_planning_and_engine_only_not_attributed_to_model(self):
        with self.db:
            self.db.execute('INSERT INTO frontier VALUES(?,?,?)', ('wallet-job', self.wallet, 'pending'))
            for call, selected, reason in ((660,['wallet-job'],'Check incoming funds.'),
                                            (661,['wallet-job'],'Inspect later token buys.'),
                                            (662,['other-job'],'Unrelated model choice.')):
                self.db.execute('INSERT INTO investigations(call_id,body,at) VALUES(?,?,?)',
                    (call, canonical(dict(job_ids=selected, scheduled_job_ids=['wallet-job'], reason=reason)), call))
        report = self.report()
        planning = report['analysis']['details']['planning']
        self.assertEqual([r['call_id'] for r in planning['muse']], [661,660])
        self.assertTrue(all(r['purpose']=='investigation_planning' and not r['verified_conclusion'] for r in planning['muse']))
        self.assertEqual(planning['engine_supplements'][0]['call_id'], 662)
        self.assertFalse(planning['engine_supplements'][0]['selected_by_model'])
        self.assertEqual(report['analysis']['muse'], planning['muse'])

    def test_bounds_deterministic_partial_report_and_evidence_tiers(self):
        for index in range(65):
            self.assessed_edge(target=key(index+2), **self.raw(signature='signature-'+str(index)))
        self.assessed_edge(kind='provider_lead', signature='provider', source='sqd_public_portal')
        with patch('economic_machine.pump_research.research_workspace.time.time_ns', return_value=1792055000000000000):
            first = self.report()
            second = self.report()
        self.assertEqual(first, second)
        self.assertLessEqual(len(first['report']['details']['direct_transfers']['counterparty_unit_totals']), 40)
        self.assertTrue(first['report']['details']['direct_transfers']['totals_truncated'])
        self.assertEqual(first['report']['details']['evidence_tiers'],
                         {'provider_or_local_observation':1, 'raw_transaction_record':65})
        self.assertLessEqual(len(first['report']['findings']), 11)
        self.assertFalse(first['report']['complete_history'])
        self.assertFalse(first['coverage']['current_balance_verified'])
        self.assertEqual(first['report']['details'], first['analysis']['details'])

    def test_missing_assessment_does_not_draw_or_sum_legacy_raw_flow(self):
        ident = self.edge(**self.raw())
        before = self.db.execute('SELECT body FROM edges WHERE id=?', (ident,)).fetchone()[0]
        report = self.report()
        self.assertEqual(report['graph']['edges'], [])
        self.assertEqual(report['analysis']['raw_relationships'], 0)
        self.assertEqual(report['analysis']['quarantined_relationships'], 1)
        self.assertEqual(report['analysis']['details']['direct_transfers']['counterparty_unit_totals'], [])
        review = report['analysis']['details']['instruction_commit_review']
        self.assertEqual(review['reasons'], {'assessment_pending':1})
        self.assertTrue(review['source_records_preserved'])
        self.assertEqual(self.db.execute('SELECT body FROM edges WHERE id=?', (ident,)).fetchone()[0], before)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='edge_commit_assessments'").fetchone())

    def test_current_rejected_and_unchecked_assessments_remain_quarantined(self):
        for index, status in enumerate(('rejected','legacy_unchecked')):
            ident = self.edge(**self.raw(signature='excluded-'+str(index)))
            self.fixture_assessment(ident, status=status)
        report = self.report()
        self.assertEqual(report['graph']['edges'], [])
        self.assertEqual(report['analysis']['details']['instruction_commit_review']['assessment_statuses'],
                         {'rejected':1, 'legacy_unchecked':1})
        self.assertEqual(report['report']['evidence_records'], [])

    def test_new_source_revision_quarantines_previously_committed_flow(self):
        self.assessed_edge(**self.raw())
        self.assertEqual(len(self.report()['graph']['edges']), 1)
        self.advance_index_revision()
        report = self.report()
        self.assertEqual(report['graph']['edges'], [])
        # Read-through reassessment now exposes the actual missing proof in
        # this deliberately synthetic presentation fixture.
        self.assertEqual(report['analysis']['details']['instruction_commit_review']['reasons'], {'missing_signature':1})
        self.assertEqual(self.db.execute('SELECT count(*) FROM edge_commit_assessments').fetchone()[0], 1)

    def test_edge_body_change_invalidates_old_committed_assessment(self):
        ident = self.assessed_edge(**self.raw())
        with self.db:
            self.db.execute("UPDATE edges SET body=json_set(body,'$.amount','999') WHERE id=?", (ident,))
        self.assertEqual(self.report()['graph']['edges'], [])
        self.assertEqual(self.transfers()['counterparty_unit_totals'], [])

    def test_legacy_transfer_kind_is_recovered_only_in_read_view(self):
        ident = self.edge(**self.raw(transfer_kind=None, amount='2039280'))
        original = self.db.execute('SELECT body FROM edges WHERE id=?', (ident,)).fetchone()[0]
        self.fixture_assessment(ident, transfer_kind='account_creation_funding')
        report = self.report()
        self.assertEqual(report['graph']['edges'][0]['evidence']['transfer_kind'], 'account_creation_funding')
        transfer = report['analysis']['details']['direct_transfers']
        self.assertEqual(transfer['account_creation_funding_items'], 1)
        self.assertEqual(transfer['counterparty_unit_totals'][0]['instruction_class'], 'account_creation_funding')
        self.assertEqual(self.db.execute('SELECT body FROM edges WHERE id=?', (ident,)).fetchone()[0], original)

    def test_failed_dead_and_explicit_signature_failures_remain_excluded_after_synthetic_commit(self):
        for index, fields in enumerate((dict(transaction_success=False), dict(dead_branch=True), dict(transaction_signature_verified=False))):
            self.assessed_edge(**self.raw(signature='invalid-'+str(index), **fields))
        report = self.report()
        self.assertEqual(report['graph']['edges'], [])
        review = report['analysis']['details']['instruction_commit_review']
        self.assertEqual(review['excluded_records'], 3)
        self.assertEqual(review['failed_records_excluded'], 1)
        self.assertEqual(report['analysis']['details']['direct_transfers']['failed_records_excluded'], 1)

    def test_unassessed_provider_claim_is_not_promoted_to_raw_instruction(self):
        self.edge(kind='provider_creator_claim', source='provider', target=key(12))
        report = self.report()
        self.assertEqual(len(report['graph']['edges']), 1)
        self.assertEqual(report['analysis']['raw_relationships'], 0)
        self.assertEqual(report['analysis']['details']['token_participation']['provider_claims'][0]['token'], key(12))
        self.assertEqual(report['analysis']['quarantined_relationships'], 0)

    def test_retained_coverage_uses_live_assessments_and_preserves_historical_receipts(self):
        from economic_machine.pump_research.coverage import Coverage, DEADLINE_MS
        clock=patch('economic_machine.pump_research.coverage.time.time_ns',return_value=(DEADLINE_MS-1000)*1000000)
        clock.start();self.addCleanup(clock.stop)
        ident = self.assessed_edge(**self.raw(signature=encode(bytes([7])*64)))
        path = self.base/'evidence/wallet-map-v1/coverage.sqlite3'
        ledger = Coverage(path, guard=lambda: None)
        try:
            refs = ['graph_edge:'+ident, 'object:'+'a'*64]
            ledger.register_source('fixture', kind='retained_raw_transaction_record',
                start_slot=400000000, end_slot=400000000, evidence_refs=refs)
            frontier = ledger.register_wallet(self.wallet, 'fixture')
            ledger.record_span(self.wallet, 'fixture', 'inspected', 400000000, 400000000,
                               method='retained_single_transaction_reference', evidence_refs=refs)
            ledger.record_link(self.wallet, 'fixture', key(2), 'outgoing', 400000000, evidence_refs=refs)
            ledger.advance(self.wallet, 'fixture', cursor={'fixture':1}, state='scanning',
                           expected_revision=frontier['revision'], evidence_refs=refs, latest_observed_slot=400000000)
            before = ledger.db.total_changes
            first = self.report()
            current = first['coverage']['source_ledger']
            self.assertEqual(current['latest_observed_slot'], 400000000)
            self.assertEqual(len(current['trace_evidence']['outgoing']), 1)
            self.advance_index_revision()
            self.fixture_assessment(ident, status='rejected')
            second = self.report()
            current = second['coverage']['source_ledger']
            self.assertEqual(current['trace_evidence']['outgoing'], [])
            self.assertEqual(current['spans']['inspected'], [])
            self.assertIsNone(current['latest_observed_slot'])
            self.assertTrue(current['current_evidence_view']['retraction_or_commit_review_pending'])
            self.assertEqual(second['graph']['edges'], [])
            self.assertEqual(ledger.db.total_changes, before)
            self.assertEqual(len(ledger.report(self.wallet)['trace_evidence']['outgoing']), 1)
        finally:
            ledger.close()

    def test_unavailable_optional_coverage_does_not_hide_reviewed_graph_or_invent_empty_coverage(self):
        self.assessed_edge(**self.raw())
        path = self.base/'evidence/wallet-map-v1/coverage.sqlite3'
        path.write_bytes(b'presentation-test-placeholder')
        error = sqlite3.OperationalError('sensitive internal locator must not appear')
        error.sqlite_errorname = 'SQLITE_CANTOPEN'
        with patch('economic_machine.pump_research.coverage_projection.report_retained', side_effect=error):
            report = self.report()
        self.assertEqual(len(report['graph']['edges']), 1)
        self.assertEqual(report['analysis']['raw_relationships'], 1)
        self.assertEqual(report['analysis']['details']['direct_transfers']['counterparty_unit_totals'][0]['amount'], '9')
        status = report['coverage']['source_coverage_status']
        self.assertFalse(status['available'])
        self.assertEqual(status['reason'], 'retained_coverage_database_unavailable')
        self.assertEqual(status['sqlite_error_name'], 'SQLITE_CANTOPEN')
        self.assertNotIn('source_ledger', report['coverage'])
        self.assertNotIn('source_count', report['coverage'])
        self.assertNotIn('gaps', report['coverage'])
        self.assertNotIn('latest_observed_slot', report['coverage'])
        self.assertFalse(report['coverage']['complete'])
        self.assertEqual(report['report']['details']['source_coverage_unavailable'], status)
        self.assertIn('temporarily unavailable', ' '.join(report['report']['findings']))
        self.assertNotIn('sensitive internal locator', canonical(report))
        self.assertEqual(path.read_bytes(), b'presentation-test-placeholder')

    def test_missing_optional_coverage_is_unavailable_without_zero_claims(self):
        report = self.report()
        self.assertEqual(report['coverage']['source_coverage_status']['reason'],
                         'retained_coverage_database_not_present')
        self.assertFalse(report['coverage']['source_coverage_status']['available'])
        self.assertNotIn('source_count', report['coverage'])
        self.assertNotIn('source_ledger', report['coverage'])


if __name__=='__main__':
    unittest.main()
