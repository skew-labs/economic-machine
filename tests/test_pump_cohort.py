import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.cohort import (
    AUTHORIZATION, ORIGINAL_DEADLINE_MS, WINDOW_MS, activate,
    build_snapshot, normalize_timestamp, snapshot_sha256,
)
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph


AS_OF = 1792055000000


def key(number):
    return encode(number.to_bytes(32, 'big'))


def row(number, **changes):
    value = dict(mint=key(number), creator=key(number + 1000), symbol='T'+str(number),
                 created_timestamp=AS_OF-1000, timestamp_unit='ms', usd_market_cap='100')
    value.update(changes)
    return value


def page(rows, **changes):
    value = dict(provider='public-provider', fetched_ms=AS_OF+100,
                 sha256=hashlib.sha256(canonical(rows).encode()).hexdigest(), rows=rows,
                 fetch_metadata=dict(status=200, cursor='page:1', endpoint='public-token-list'))
    value.update(changes)
    return value


def grant(snapshot, **changes):
    value = dict(authorization=AUTHORIZATION, cutoff_ms=snapshot['as_of_ms'],
                 window_start_ms=snapshot['window_start_ms'],
                 snapshot_sha256=snapshot_sha256(snapshot), deadline_ms=ORIGINAL_DEADLINE_MS,
                 trading=False, paid_api=False)
    value.update(changes)
    return value


class SelectionTests(unittest.TestCase):
    def test_exact_30_day_boundaries_and_future(self):
        rows = [row(1, created_timestamp=AS_OF-WINDOW_MS),
                row(2, created_timestamp=AS_OF),
                row(3, created_timestamp=AS_OF-WINDOW_MS-1),
                row(4, created_timestamp=AS_OF+1)]
        snapshot = build_snapshot([page(rows)], AS_OF)
        self.assertEqual({r['mint'] for r in snapshot['selected']}, {key(1), key(2)})
        self.assertEqual(snapshot['window_end_inclusive_ms'], AS_OF)
        self.assertEqual({r['reason'] for r in snapshot['rejections']},
                         {'COHORT_CREATED_BEFORE_WINDOW', 'COHORT_CREATED_AFTER_CUTOFF'})

    def test_seconds_and_milliseconds_exact_and_originals_retained(self):
        first = row(1, created_timestamp=(AS_OF-1000)//1000, timestamp_unit='s')
        second = row(1)
        snapshot = build_snapshot([page([first]), page([second])], AS_OF)
        self.assertEqual(len(snapshot['selected']), 1)
        selected = snapshot['selected'][0]
        self.assertEqual(selected['created_ms'], AS_OF-1000)
        self.assertEqual(snapshot['duplicate_equal_rows'], 1)
        self.assertEqual([o['timestamp_originals'][0]['unit'] for o in selected['observations']], ['s', 'ms'])
        self.assertEqual(selected['observations'][0]['provider_row'], first)
        self.assertEqual(selected['observations'][1]['provider_row'], second)

    def test_timestamp_unit_cannot_be_guessed(self):
        value = row(1)
        del value['timestamp_unit']
        snapshot = build_snapshot([page([value])], AS_OF)
        self.assertEqual(snapshot['selected'], [])
        self.assertIn('COHORT_TIMESTAMP_UNIT', [r['reason'] for r in snapshot['rejections']])
        self.assertEqual(normalize_timestamp('1.001', 's'), 1001)
        with self.assertRaises(ValueError):
            normalize_timestamp('1.0001', 's')

    def test_created_ms_field_is_explicit_and_conflicts_rejected(self):
        value = row(1)
        del value['created_timestamp']
        del value['timestamp_unit']
        value['created_ms'] = AS_OF-1
        self.assertEqual(build_snapshot([page([value])], AS_OF)['selected'][0]['created_ms'], AS_OF-1)
        value['created_at'] = (AS_OF-1000)//1000
        value['timestamp_unit'] = 's'
        self.assertEqual(build_snapshot([page([value])], AS_OF)['selected'], [])

    def test_invalid_caps_and_ath_never_substituted(self):
        rows = [row(i+1, usd_market_cap=cap, ath_market_cap='999999999')
                for i, cap in enumerate(('NaN', 'Infinity', '-Infinity', '0', '-1', '', None, True))]
        value = row(20, ath_market_cap='999999999')
        del value['usd_market_cap']
        rows.append(value)
        snapshot = build_snapshot([page(rows)], AS_OF)
        self.assertEqual(snapshot['selected'], [])
        self.assertTrue(all(r['reason'] in ('COHORT_USD_CAP_INVALID', 'COHORT_DUPLICATE_CONFLICT_OR_INVALID')
                            for r in snapshot['rejections']))

    def test_large_positive_provider_claim_is_not_invented_quarantine(self):
        snapshot = build_snapshot([page([row(1, usd_market_cap='2209000000')])], AS_OF)
        self.assertEqual(snapshot['selected'][0]['usd_market_cap'], '2209000000')
        self.assertFalse(snapshot['selected'][0]['chain_verified'])

    def test_duplicate_conflicting_cap_quarantines_mint(self):
        snapshot = build_snapshot([page([row(1), row(2)]), page([row(1, usd_market_cap='101')])], AS_OF)
        self.assertEqual([r['mint'] for r in snapshot['selected']], [key(2)])
        conflict = next(r for r in snapshot['rejections'] if r['mint'] == key(1))
        self.assertEqual(conflict['reason'], 'COHORT_DUPLICATE_CONFLICT_OR_INVALID')
        self.assertEqual(len(conflict['valid_claims']), 2)

    def test_duplicate_invalid_record_cannot_leave_convenient_valid_copy(self):
        snapshot = build_snapshot([page([row(1), row(1, usd_market_cap='NaN')])], AS_OF)
        self.assertEqual(snapshot['selected'], [])

    def test_decimal_precision_sort_and_mint_tie(self):
        precise = '100.000000000000000000000000000001'
        rows = [row(4), row(2), row(3, usd_market_cap=precise), row(1)]
        selected = build_snapshot([page(rows)], AS_OF)['selected']
        self.assertEqual(selected[0]['mint'], key(3))
        self.assertEqual(selected[0]['usd_market_cap'], precise)
        self.assertEqual([r['mint'] for r in selected[1:]], sorted([key(4), key(2), key(1)]))

    def test_only_50_selected_and_complete_universe_not_assumed(self):
        rows = [row(i, usd_market_cap=str(i)) for i in range(1, 61)]
        snapshot = build_snapshot([page(rows, complete=True, total=60)], AS_OF)
        self.assertEqual(len(snapshot['selected']), 50)
        self.assertEqual(snapshot['selected'][0]['mint'], key(60))
        self.assertEqual(snapshot['eligible_unselected'], 10)
        self.assertFalse(snapshot['universe_complete'])
        self.assertFalse(snapshot['ranking_verified'])

    def test_metadata_page_hash_and_input_immutability(self):
        supplied = page([row(1)])
        before = copy.deepcopy(supplied)
        snapshot = build_snapshot([supplied], AS_OF)
        self.assertEqual(supplied, before)
        metadata = snapshot['pages'][0]
        self.assertEqual(metadata['sha256'], supplied['sha256'])
        self.assertEqual(metadata['fetch_metadata'], supplied['fetch_metadata'])
        self.assertEqual(metadata['rows_sha256'], snapshot_sha256(supplied['rows']))
        supplied['rows'][0]['usd_market_cap'] = '1'
        self.assertEqual(snapshot['selected'][0]['usd_market_cap'], '100')

    def test_creator_missing_or_invalid_remains_missing(self):
        rows = [row(1, creator=None), row(2, creator='not a Solana account')]
        selected = build_snapshot([page(rows)], AS_OF)['selected']
        self.assertTrue(all(r['creator'] is None for r in selected))
        self.assertEqual({r['creator_status'] for r in selected}, {'missing', 'invalid'})

    def test_invalid_mint_rejected_and_bad_page_hash_fails(self):
        self.assertEqual(build_snapshot([page([row(1, mint='fake')])], AS_OF)['selected'], [])
        with self.assertRaisesRegex(ValueError, 'COHORT_PAGE_PROVENANCE'):
            build_snapshot([page([row(1)], sha256='not-a-hash')], AS_OF)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base/'evidence').mkdir()
        self.graph = Graph(base)
        self.addCleanup(self.graph.close)

    def test_new_root_stages_and_replay_preserve_originals(self):
        g = self.graph
        old_body = canonical(dict(mint=key(1), creator=key(1001), symbol='ORIGINAL', custom='retain'))
        with g.db:
            g.db.execute('INSERT INTO roots VALUES(?,?)', (key(1), old_body))
            g.set('core_roots', True)
            g.set('core_early_queued', True)
            old_job = g.enqueue(key(1), key(1001), None, AS_OF-1000, 0, 'core_forward')
            g.db.execute("UPDATE frontier SET state='core_source_exhausted',cursor='123' WHERE id=?", (old_job,))
        old_frontier = tuple(g.db.execute('SELECT * FROM frontier WHERE id=?', (old_job,)).fetchone())
        snapshot = build_snapshot([page([row(1), row(2), row(3, creator=None)])], AS_OF)
        first = activate(g, snapshot, grant(snapshot), AS_OF+1000)
        self.assertEqual(first['roots_added'], 2)
        self.assertEqual(first['existing_roots_preserved'], 1)
        self.assertEqual(first['launch_jobs_added'], 1)
        self.assertEqual(first['core_jobs_added'], 2)
        self.assertEqual(first['missing_creator'], 1)
        self.assertEqual(g.db.execute('SELECT body FROM roots WHERE mint=?', (key(1),)).fetchone()[0], old_body)
        self.assertEqual(tuple(g.db.execute('SELECT * FROM frontier WHERE id=?', (old_job,)).fetchone()), old_frontier)
        self.assertTrue(g.state('core_roots'))
        self.assertTrue(g.state('core_early_queued'))
        self.assertEqual(g.db.execute('SELECT count(*) FROM cohort_root_stages').fetchone()[0], 15)
        self.assertEqual(g.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 4)
        edge = g.db.execute("SELECT body FROM edges WHERE kind='provider_creator_claim'").fetchone()
        self.assertFalse(json.loads(edge[0])['chain_verified'])
        root = json.loads(g.db.execute('SELECT body FROM roots WHERE mint=?', (key(2),)).fetchone()[0])
        self.assertEqual(root['cohort_kind'], 'recent30d_current_marketcap')
        self.assertFalse(root['ath_metric_used'])
        with g.db:
            g.db.execute("UPDATE cohort_root_stages SET state='later_progress' WHERE mint=? AND stage='forward_funds'", (key(2),))
        second = activate(g, snapshot, grant(snapshot), AS_OF+2000)
        self.assertTrue(second['already_active'])
        self.assertEqual(g.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 4)
        self.assertEqual(g.db.execute("SELECT state FROM cohort_root_stages WHERE mint=? AND stage='forward_funds'", (key(2),)).fetchone()[0], 'later_progress')

    def test_missing_creator_has_no_fake_frontier(self):
        snapshot = build_snapshot([page([row(1, creator=None)])], AS_OF)
        activate(self.graph, snapshot, grant(snapshot), AS_OF+1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 0)
        stage = self.graph.db.execute("SELECT state FROM cohort_root_stages WHERE stage='launch'").fetchone()[0]
        self.assertEqual(stage, 'pending_missing_creator_metadata')

    def test_grant_required_pinned_and_cannot_extend_deadline(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        variations = [dict(snapshot_sha256='0'*64), dict(cutoff_ms=AS_OF+1),
                      dict(window_start_ms=0), dict(deadline_ms=ORIGINAL_DEADLINE_MS+1),
                      dict(trading=True), dict(paid_api=True), dict(authorization='other')]
        for changes in variations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                activate(self.graph, snapshot, grant(snapshot, **changes), AS_OF+1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)

    def test_expired_and_future_snapshot_do_not_mutate(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        with self.assertRaises(ValueError):
            activate(self.graph, snapshot, grant(snapshot), ORIGINAL_DEADLINE_MS)
        with self.assertRaises(ValueError):
            activate(self.graph, snapshot, grant(snapshot), AS_OF-1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)

    def test_snapshot_modification_invalidates_pin(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        authorization = grant(snapshot)
        snapshot['selected'][0]['creator'] = key(4000)
        with self.assertRaisesRegex(ValueError, 'COHORT_SUPPLEMENTAL_GRANT'):
            activate(self.graph, snapshot, authorization, AS_OF+1)

    def test_same_snapshot_different_grant_replay_rejected(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        activate(self.graph, snapshot, grant(snapshot), AS_OF+1)
        with self.assertRaisesRegex(ValueError, 'COHORT_ACTIVATION_IDENTITY_CHANGED'):
            activate(self.graph, snapshot, grant(snapshot, note='changed'), AS_OF+2)

    def test_callback_failure_rolls_back_activation(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        with patch.object(self.graph, 'enqueue', side_effect=RuntimeError('fail')):
            with self.assertRaises(RuntimeError):
                activate(self.graph, snapshot, grant(snapshot), AS_OF+1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM nodes').fetchone()[0], 0)

    def test_outer_transaction_not_committed(self):
        snapshot = build_snapshot([page([row(1)])], AS_OF)
        self.graph.db.execute('BEGIN')
        activate(self.graph, snapshot, grant(snapshot), AS_OF+1)
        self.assertTrue(self.graph.db.in_transaction)
        self.graph.db.rollback()
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
