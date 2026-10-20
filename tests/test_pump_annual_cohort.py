import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.annual_cohort import (
    AUTHORIZATION, ORIGINAL_DEADLINE_MS, WINDOW_MS, activate, build_snapshot, snapshot_sha256,
)
from economic_machine.pump_research import cohort
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph

AS_OF = 1792055000000


def key(n):
    return encode(n.to_bytes(32, 'big'))


def row(n, **changes):
    value = dict(mint=key(n), creator=key(n + 10000), symbol='T' + str(n),
        created_ms=AS_OF - 1000, usd_market_cap='100')
    value.update(changes)
    return value


def page(rows):
    return dict(provider='public-provider', fetched_ms=AS_OF + 1,
        sha256=snapshot_sha256(rows), rows=rows)


def grant(snapshot, **changes):
    value = dict(authorization=AUTHORIZATION, cutoff_ms=snapshot['as_of_ms'],
        window_start_ms=snapshot['window_start_ms'], snapshot_sha256=snapshot_sha256(snapshot),
        deadline_ms=ORIGINAL_DEADLINE_MS, trading=False, paid_api=False)
    value.update(changes)
    return value


class AnnualSelectionTests(unittest.TestCase):
    def test_inclusive_365_day_window_and_no_future_claim(self):
        values = [row(1, created_ms=AS_OF - WINDOW_MS), row(2, created_ms=AS_OF),
            row(3, created_ms=AS_OF - WINDOW_MS - 1), row(4, created_ms=AS_OF + 1)]
        result = build_snapshot([page(values)], AS_OF)
        self.assertEqual({r['mint'] for r in result['selected']}, {key(1), key(2)})
        self.assertEqual(result['window_days'], 365)

    def test_up_to_1000_current_market_cap_claims_without_global_rank(self):
        result = build_snapshot([page([row(n, usd_market_cap=str(n)) for n in range(1, 1004)])], AS_OF)
        self.assertEqual(len(result['selected']), 1000)
        self.assertEqual(result['selected'][0]['mint'], key(1003))
        self.assertEqual(result['eligible_unselected'], 3)
        self.assertFalse(result['ranking_verified'])
        self.assertFalse(result['universe_complete'])

    def test_less_than_1000_is_not_filled_by_inventing_rows(self):
        result = build_snapshot([page([row(1)])], AS_OF)
        self.assertEqual(len(result['selected']), 1)
        self.assertFalse(result['target_selection_filled'])

    def test_exact_decimal_sort_and_deterministic_ties(self):
        precise = '100.00000000000000000000000000000001'
        result = build_snapshot([page([row(3), row(2, usd_market_cap=precise), row(1)])], AS_OF)
        self.assertEqual(result['selected'][0]['mint'], key(2))
        self.assertEqual(result['selected'][0]['usd_market_cap'], precise)
        self.assertEqual([r['mint'] for r in result['selected'][1:]], sorted([key(1), key(3)]))

    def test_conflicting_duplicate_quarantined_with_both_claims(self):
        result = build_snapshot([page([row(1)]), page([row(1, usd_market_cap='101'), row(2)])], AS_OF)
        self.assertEqual([r['mint'] for r in result['selected']], [key(2)])
        rejected = next(r for r in result['rejections'] if r['reason'] == 'COHORT_DUPLICATE_CONFLICT_OR_INVALID')
        self.assertEqual(len(rejected['valid_claims']), 2)

    def test_invalid_duplicate_cannot_leave_convenient_valid_copy(self):
        result = build_snapshot([page([row(1), row(1, usd_market_cap='NaN')])], AS_OF)
        self.assertEqual(result['selected'], [])

    def test_equal_duplicate_keeps_all_provenance(self):
        result = build_snapshot([page([row(1)]), page([row(1, usd_market_cap='100.00')])], AS_OF)
        self.assertEqual(result['duplicate_equal_rows'], 1)
        self.assertEqual(len(result['selected'][0]['observations']), 2)

    def test_ath_field_never_substitutes_current_cap(self):
        value = row(1, ath_market_cap='1000000000'); del value['usd_market_cap']
        self.assertEqual(build_snapshot([page([value])], AS_OF)['selected'], [])

    def test_input_and_recent30day_module_unchanged(self):
        values = [page([row(1)])]; before = copy.deepcopy(values)
        result = build_snapshot(values, AS_OF)
        self.assertEqual(values, before)
        self.assertEqual(result['pages'][0]['rows_sha256'], snapshot_sha256(values[0]['rows']))
        self.assertEqual(cohort.WINDOW_MS, 30 * 86400000)
        self.assertEqual(cohort.build_snapshot(values, AS_OF)['limit'], 50)

    def test_missing_and_invalid_creator_remain_unknown(self):
        result = build_snapshot([page([row(1, creator=None), row(2, creator='bad')])], AS_OF)
        self.assertTrue(all(r['creator'] is None for r in result['selected']))
        self.assertEqual({r['creator_status'] for r in result['selected']}, {'missing', 'invalid'})

    def test_cap_and_timestamp_validation_reuses_existing_strict_rules(self):
        for cap in (True, 'Infinity', 'NaN', '-1', '0', '1e99999999'):
            with self.subTest(cap=cap):
                self.assertEqual(build_snapshot([page([row(1, usd_market_cap=cap)])], AS_OF)['selected'], [])
        value = row(1); del value['created_ms']; value['created_timestamp'] = AS_OF
        self.assertEqual(build_snapshot([page([value])], AS_OF)['selected'], [])

    def test_limit_cannot_exceed_1000(self):
        for limit in (0, 1001, True):
            with self.assertRaises(ValueError):
                build_snapshot([], AS_OF, limit)


class AnnualActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name); (base / 'evidence').mkdir()
        self.graph = Graph(base); self.addCleanup(self.graph.close)

    def test_append_new_cohort_preserves_old_roots_frontier_and_memberships(self):
        old = cohort.build_snapshot([page([row(1)])], AS_OF)
        old_grant = dict(authorization=cohort.AUTHORIZATION, cutoff_ms=AS_OF,
            window_start_ms=old['window_start_ms'], snapshot_sha256=cohort.snapshot_sha256(old),
            deadline_ms=ORIGINAL_DEADLINE_MS, trading=False, paid_api=False)
        cohort.activate(self.graph, old, old_grant, AS_OF + 1)
        original_root = tuple(self.graph.db.execute('SELECT * FROM roots WHERE mint=?', (key(1),)).fetchone())
        original_jobs = [tuple(r) for r in self.graph.db.execute('SELECT * FROM frontier ORDER BY id')]
        new = build_snapshot([page([row(1), row(2)])], AS_OF)
        result = activate(self.graph, new, grant(new), AS_OF + 2)
        self.assertEqual(result['roots_added'], 1)
        self.assertEqual(result['existing_roots_preserved'], 1)
        self.assertEqual(tuple(self.graph.db.execute('SELECT * FROM roots WHERE mint=?', (key(1),)).fetchone()), original_root)
        self.assertEqual([tuple(r) for r in self.graph.db.execute('SELECT * FROM frontier WHERE root=? ORDER BY id', (key(1),))], original_jobs)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM cohorts').fetchone()[0], 2)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM cohort_memberships').fetchone()[0], 3)

    def test_replay_is_idempotent_and_keeps_later_progress(self):
        snap = build_snapshot([page([row(1)])], AS_OF)
        activate(self.graph, snap, grant(snap), AS_OF + 1)
        with self.graph.db:
            self.graph.db.execute("UPDATE cohort_root_stages SET state='progressed' WHERE stage='forward_funds'")
        result = activate(self.graph, snap, grant(snap), AS_OF + 2)
        self.assertTrue(result['already_active'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 3)
        self.assertEqual(self.graph.db.execute("SELECT state FROM cohort_root_stages WHERE stage='forward_funds'").fetchone()[0], 'progressed')

    def test_missing_creator_only_schedules_explicit_mint_discovery(self):
        snap = build_snapshot([page([row(1, creator=None)])], AS_OF)
        result = activate(self.graph, snap, grant(snap), AS_OF + 1)
        self.assertEqual(result['mint_discovery_jobs_added'], 1)
        self.assertEqual(result['core_jobs_added'], 0)
        job = self.graph.db.execute('SELECT * FROM frontier').fetchone()
        self.assertEqual(job['source'], 'recover_launch')
        self.assertEqual(job['wallet'], key(1))
        self.assertEqual(self.graph.state('cohort_launch_lookup:' + job['id'])['lookup_role'], 'token_identifier_not_creator')
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)
        self.assertEqual(self.graph.db.execute('SELECT kind FROM nodes WHERE id=?', (key(1),)).fetchone()[0], 'token')

    def test_supplemental_grant_is_separate_and_snapshot_pinned(self):
        snap = build_snapshot([page([row(1)])], AS_OF)
        for changes in (dict(authorization=cohort.AUTHORIZATION), dict(snapshot_sha256='0' * 64),
                dict(cutoff_ms=AS_OF + 1), dict(window_start_ms=0), dict(trading=True), dict(paid_api=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                activate(self.graph, snap, grant(snap, **changes), AS_OF + 1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)

    def test_original_absolute_deadline_cannot_change(self):
        snap = build_snapshot([page([row(1)])], AS_OF)
        for deadline in (ORIGINAL_DEADLINE_MS - 1, ORIGINAL_DEADLINE_MS + 1):
            with self.assertRaises(ValueError):
                activate(self.graph, snap, grant(snap, deadline_ms=deadline), AS_OF + 1, deadline_ms=deadline)
        with self.assertRaises(ValueError):
            activate(self.graph, snap, grant(snap), ORIGINAL_DEADLINE_MS)

    def test_future_snapshot_and_selected_order_rejected(self):
        snap = build_snapshot([page([row(1), row(2, usd_market_cap='101')])], AS_OF)
        with self.assertRaises(ValueError):
            activate(self.graph, snap, grant(snap), AS_OF - 1)
        snap['selected'].reverse()
        for rank, item in enumerate(snap['selected'], 1):
            item.update(observed_rank=rank, provider_rank=rank)
        with self.assertRaisesRegex(ValueError, 'SELECTED_ORDER'):
            activate(self.graph, snap, grant(snap), AS_OF + 1)

    def test_activation_failure_rolls_back_all_new_memberships(self):
        snap = build_snapshot([page([row(1), row(2)])], AS_OF)
        with patch.object(self.graph, 'enqueue', side_effect=ValueError('test interruption')):
            with self.assertRaisesRegex(ValueError, 'test interruption'):
                activate(self.graph, snap, grant(snap), AS_OF + 1)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM roots').fetchone()[0], 0)
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM frontier').fetchone()[0], 0)


class AnnualCollectorTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'products/pump_research/annual_collect.py'
        spec = importlib.util.spec_from_file_location('annual_collect_under_test', path)
        self.collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.collector)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / 'cap-page-0000.json'
        self.receipt = self.base / 'cap-page-0000.receipt.json'

    def write_page(self, chain=None):
        # A lexical decimal deliberately exceeds float precision.
        raw = ('[{"mint":"' + key(1) + '","chain_id":"' + (chain or self.collector.CHAIN) +
            '","usd_market_cap":100.000000000000000000000000000001,"created_timestamp":' + str(AS_OF - 1000) + '}]').encode()
        self.path.write_bytes(raw)
        self.receipt.write_text(canonical(dict(url=self.collector.page_url(0), at_ms=AS_OF,
            status=200, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())))
        return raw

    def test_reused_page_checks_hash_and_preserves_decimal_lexeme(self):
        raw = self.write_page()
        page_value = self.collector.load_page(self.path, self.receipt, 0, self.base)
        self.assertEqual(page_value['rows'][0]['usd_market_cap'], '100.000000000000000000000000000001')
        self.assertEqual(page_value['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(page_value['rows'][0]['timestamp_unit'], 'ms')

    def test_mismatched_raw_response_cannot_be_reused(self):
        self.write_page(); self.path.write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'RECEIPT_MISMATCH'):
            self.collector.load_page(self.path, self.receipt, 0, self.base)

    def test_wrong_chain_rejected(self):
        self.write_page(chain='eip155:1')
        with self.assertRaisesRegex(ValueError, 'CHAIN'):
            self.collector.load_page(self.path, self.receipt, 0, self.base)

    def test_provider_url_cannot_expand_beyond_3000_rows(self):
        self.assertIn('sort=market_cap&order=DESC', self.collector.page_url(2950))
        for offset in (3000, -1, 501, True):
            with self.assertRaisesRegex(ValueError, 'OFFSET'):
                self.collector.page_url(offset)

    def test_incomplete_cached_pair_is_preserved_without_fetch(self):
        self.path.write_bytes(b'preserved')
        with patch.object(self.collector.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'INCOMPLETE_PAIR_PRESERVED'):
                self.collector.fetch_page(self.base, 0, None)
        opener.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'preserved')


if __name__ == '__main__':
    unittest.main()
