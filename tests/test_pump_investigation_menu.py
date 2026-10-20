from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research import investigation_menu
from economic_machine.pump_research.investigation_menu import (
    ROTATION_KEY, build_menu, compact_cursor, supplement_selection,
)
from economic_machine.pump_research.store import canonical, encode
from economic_machine.pump_research.wallet_map import Graph


def key(number):
    return encode(number.to_bytes(32, 'big'))


class CursorTests(unittest.TestCase):
    def test_small_cursor_unchanged(self):
        self.assertEqual(compact_cursor('[12,3,4]'), '[12,3,4]')
        self.assertEqual(compact_cursor(None), 'null')

    def test_large_cursor_keeps_hash_and_bounded_head(self):
        raw = canonical(dict(time_search=dict(samples=[[i, i+100] for i in range(1000)])))
        result = compact_cursor(raw)
        self.assertLessEqual(len(result), 600)
        self.assertLessEqual(len(result.encode()), 600)
        summary = json.loads(result)
        self.assertTrue(summary['truncated'])
        self.assertEqual(summary['sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(summary['original_bytes'], len(raw.encode()))
        self.assertTrue(raw.startswith(summary['head']))

    def test_unicode_escapes_and_small_custom_bound(self):
        raw = '지갑\"\\\n' * 2000
        self.assertLessEqual(len(compact_cursor(raw).encode()), 600)
        self.assertEqual(compact_cursor(raw, 64), hashlib.sha256(raw.encode()).hexdigest())
        with self.assertRaises(ValueError):
            compact_cursor(raw, 63)


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'evidence').mkdir()
        self.graph = Graph(self.base)
        self.addCleanup(self.graph.close)
        self.sequence = 0

    def root(self, number, recent=False):
        body = dict(mint=key(number), creator=key(number+1000))
        if recent:
            body.update(cohort_id='frozen-cohort', cohort_kind='recent30d_current_marketcap')
        with self.graph.db:
            self.graph.db.execute('INSERT INTO roots VALUES(?,?)', (key(number), canonical(body)))
        return key(number)

    def job(self, root, source='recover_launch', depth=0, cursor='null'):
        self.sequence += 1
        with self.graph.db:
            ident = self.graph.enqueue(root, key(100000+self.sequence), None,
                                       1792055000000+self.sequence, depth, source)
            self.graph.db.execute('UPDATE frontier SET cursor=? WHERE id=?', (cursor, ident))
        return ident

    def test_ten_root_two_job_bound_and_old_new_fairness(self):
        for i in range(1, 61):
            root = self.root(i, recent=i > 10)
            for _ in range(3):
                self.job(root)
        menu = build_menu(self.graph)
        self.assertEqual(len(menu['admitted_roots']), 10)
        self.assertEqual(len(menu['jobs']), 20)
        self.assertEqual(menu['original_roots_admitted'], 5)
        self.assertEqual(menu['recent_roots_admitted'], 5)
        self.assertTrue(all(count == 2 for count in Counter(j['root'] for j in menu['jobs']).values()))
        self.assertEqual(len(set(j['id'] for j in menu['jobs'])), 20)

    def test_rotation_admits_every_pending_root(self):
        expected = {self.root(i, recent=i > 10) for i in range(1, 61)}
        for root in sorted(expected):
            self.job(root)
        observed = set()
        for _ in range(10):
            observed.update(build_menu(self.graph)['admitted_roots'])
        self.assertEqual(observed, expected)
        state = self.graph.state(ROTATION_KEY)
        self.assertEqual(state['menus'], 10)
        other = Graph(self.base)
        try:
            self.assertEqual(other.state(ROTATION_KEY), state)
        finally:
            other.close()

    def test_history_queries_are_not_starved_by_older_pending_jobs(self):
        root = self.root(1)
        old = [self.job(root) for _ in range(8)]
        history = self.job(root, source='portal_forward')
        menu = build_menu(self.graph, max_roots=1)
        self.assertEqual([j['id'] for j in menu['jobs']], [old[0], history])
        self.assertEqual(menu['groups']['portal'], [history])
        mapping = {j['id']: j for j in menu['jobs']}
        selection = supplement_selection([old[0]], mapping, menu['groups'])
        self.assertEqual(selection['original_job_ids'], [old[0]])
        self.assertIn(history, selection['scheduled_job_ids'])
        self.assertEqual(selection['engine_reserved']['portal'], history)

    def test_portal_raw_verification_precedes_another_discovery_query(self):
        root = self.root(1)
        self.job(root)
        history = self.job(root, source='portal_forward')
        verify = self.job(root, source='portal_verify')
        self.assertEqual(build_menu(self.graph, max_roots=1)['groups']['portal'], [verify])
        with self.graph.db:
            self.graph.db.execute("UPDATE frontier SET state='raw_checked' WHERE id=?", (verify,))
        self.assertEqual(build_menu(self.graph, max_roots=1)['groups']['portal'], [history])

    def test_candidate_queries_stop_after_each_lane_has_enough_roots(self):
        for number in range(1, 101):
            root = self.root(number, recent=number > 20)
            self.job(root)
        with patch('economic_machine.pump_research.investigation_menu._candidates',
                   wraps=investigation_menu._candidates) as candidates:
            menu = build_menu(self.graph)
        self.assertEqual(len(menu['admitted_roots']), 10)
        self.assertEqual(candidates.call_count, 20)

    def test_exclusions_precede_limit_and_parallel_menus_do_not_overlap(self):
        root = self.root(1)
        expected = {self.job(root) for _ in range(20)}
        excluded = set()
        for _ in range(10):
            menu = build_menu(self.graph, excluded_ids=sorted(excluded))
            ids = {j['id'] for j in menu['jobs']}
            self.assertFalse(ids.intersection(excluded))
            self.assertEqual(len(ids), 2)
            excluded.update(ids)
        self.assertEqual(excluded, expected)
        self.assertEqual(build_menu(self.graph, excluded_ids=sorted(excluded))['jobs'], [])

    def test_original_snapshot_overrides_root_body_classification(self):
        first = self.root(1, recent=True)
        second = self.root(2, recent=True)
        self.job(first)
        self.job(second)
        with self.graph.db:
            self.graph.set('original_research_roots_v1', [first])
        menu = build_menu(self.graph)
        self.assertEqual(menu['original_roots_admitted'], 1)
        self.assertEqual(menu['recent_roots_admitted'], 1)

    def test_single_root_budget_alternates_groups(self):
        old = self.root(1)
        new = self.root(2, recent=True)
        self.job(old)
        self.job(new)
        selected = [build_menu(self.graph, max_roots=1)['admitted_roots'][0] for _ in range(4)]
        self.assertEqual(selected, [old, new, old, new])

    def test_readiness_and_nonpending_jobs_excluded(self):
        root = self.root(1)
        pending = {source: self.job(root, source=source) for source in
                   ('core_forward', 'core_early', 'core_participation', 'indexed_forward', 'portal_verify')}
        finished = self.job(root)
        with self.graph.db:
            self.graph.db.execute("UPDATE frontier SET state='source_exhausted' WHERE id=?", (finished,))
        self.assertEqual([j['id'] for j in build_menu(self.graph)['jobs']], [pending['portal_verify']])
        core = self.base/'evidence/core-trace-v1'
        core.mkdir()
        for name in ('arkm_funding.sqlite3', 'pump_event_default.sqlite3'):
            (core/name).touch()
        (self.graph.out/'address-index.sqlite3').touch()
        seen = set()
        for _ in range(3):
            menu = build_menu(self.graph, excluded_ids=sorted(seen))
            seen.update(j['id'] for j in menu['jobs'])
        self.assertEqual(seen, set(pending.values()))

    def test_priority_slot_and_special_sources_rotate_without_extra_root(self):
        root = self.root(1)
        launch = self.job(root, 'recover_launch', depth=100)
        portal = self.job(root, 'portal_verify')
        raw = self.job(root, 'archive_transfer')
        bridge = self.job(root, 'wormhole_forward')
        custody = self.job(root, 'local_forward')
        with self.graph.db:
            self.graph.db.execute('INSERT INTO custody_jobs(job,case_id) VALUES(?,?)', (custody, 'public-gap'))
        second_slots = []
        for _ in range(4):
            menu = build_menu(self.graph)
            self.assertEqual(menu['jobs'][0]['id'], launch)
            self.assertEqual(len(menu['jobs']), 2)
            self.assertEqual(menu['admitted_roots'], [root])
            second_slots.append(menu['jobs'][1]['id'])
        self.assertEqual(second_slots, [portal, raw, custody, bridge])

    def test_full_cursor_and_other_state_not_mutated(self):
        root = self.root(1)
        raw = canonical(dict(samples=list(range(1000))))
        ident = self.job(root, cursor=raw)
        with self.graph.db:
            self.graph.set('core_roots', True)
        menu = build_menu(self.graph)
        self.assertEqual(menu['jobs'][0]['cursor'], raw)
        self.assertEqual(self.graph.db.execute('SELECT cursor FROM frontier WHERE id=?', (ident,)).fetchone()[0], raw)
        self.assertTrue(self.graph.state('core_roots'))

    def interactive_root(self, number):
        root = self.root(number, recent=True)
        with self.graph.db:
            body = json.loads(self.graph.db.execute('SELECT body FROM roots WHERE mint=?', (root,)).fetchone()[0])
            body.update(root_kind='wallet', cohort_kind='internal_wallet_request')
            self.graph.db.execute('UPDATE roots SET body=? WHERE mint=?', (canonical(body), root))
        return root

    def test_interactive_request_admitted_ahead_of_old_token_backlog(self):
        for number in range(1, 61):
            root = self.root(number, recent=number > 10)
            self.job(root)
            self.job(root)
        wallet = self.interactive_root(100)
        ids = {self.job(wallet, 'portal_forward'), self.job(wallet, 'local_forward')}
        menu = build_menu(self.graph)
        self.assertEqual(menu['admitted_roots'][0], wallet)
        self.assertEqual(len(menu['admitted_roots']), 10)
        self.assertEqual(len(menu['jobs']), 20)
        self.assertEqual(menu['interactive_roots_admitted'], 1)
        self.assertEqual(menu['original_roots_admitted']+menu['recent_roots_admitted'], 9)
        self.assertLessEqual(abs(menu['original_roots_admitted']-menu['recent_roots_admitted']), 1)
        self.assertEqual(set(menu['groups']['interactive']), ids)
        self.assertEqual(set(menu['cohort_groups']['interactive']), ids)

    def test_interactive_roots_rotate_and_only_one_uses_each_menu(self):
        original = self.root(1)
        recent = self.root(2, recent=True)
        self.job(original)
        self.job(recent)
        wallets = [self.interactive_root(number) for number in (100, 101, 102)]
        for wallet in wallets:
            self.job(wallet, 'portal_forward')
        admitted = []
        for _ in range(4):
            menu = build_menu(self.graph)
            self.assertEqual(menu['interactive_roots_admitted'], 1)
            self.assertIn(original, menu['admitted_roots'])
            self.assertIn(recent, menu['admitted_roots'])
            admitted.append(menu['admitted_roots'][0])
        self.assertEqual(admitted, wallets+[wallets[0]])
        self.assertEqual(self.graph.state(ROTATION_KEY)['positions']['interactive'], 1)

    def test_excluded_interactive_jobs_do_not_hide_another_request(self):
        first = self.interactive_root(100)
        second = self.interactive_root(101)
        excluded = {self.job(first, 'portal_forward'), self.job(first, 'local_forward')}
        expected = self.job(second, 'portal_forward')
        menu = build_menu(self.graph, excluded_ids=sorted(excluded))
        self.assertEqual(menu['admitted_roots'], [second])
        self.assertEqual(menu['groups']['interactive'], [expected])
        self.assertFalse(excluded.intersection(j['id'] for j in menu['jobs']))

    def test_invalid_bounds_fail_and_empty_menu_has_no_rotation(self):
        for values in (dict(max_roots=11), dict(per_root=3), dict(max_roots=True)):
            with self.assertRaises(ValueError):
                build_menu(self.graph, **values)
        self.assertEqual(build_menu(self.graph)['jobs'], [])
        self.assertIsNone(self.graph.state(ROTATION_KEY))


class SupplementTests(unittest.TestCase):
    def test_interactive_coverage_is_reserved_without_rewriting_muse_ids(self):
        original = [str(number) for number in range(8)]
        mapping = {str(number): {} for number in range(15)}
        groups = dict(interactive=['8'], original=['0'], recent=['9'], raw_history=['10'],
                      portal=['11'], custody=['12'], crosschain=['13'])
        result = supplement_selection(original, mapping, groups)
        self.assertEqual(result['original_job_ids'], original)
        self.assertEqual(len(result['scheduled_job_ids']), 8)
        self.assertIn('8', result['scheduled_job_ids'])
        self.assertEqual(result['engine_reserved']['interactive'], '8')
        self.assertEqual(result['uncovered_groups'], [])

    def test_engine_coverage_preserves_model_record_and_eight_bound(self):
        mapping = {str(i): {} for i in range(20)}
        original = [str(i) for i in range(8)]
        groups = dict(original=['0'], recent=['8'], raw_history=['9'],
                      portal=['10'], custody=['11'], crosschain=['12'])
        before = copy.deepcopy((original, mapping, groups))
        result = supplement_selection(original, mapping, groups)
        self.assertEqual((original, mapping, groups), before)
        self.assertEqual(result['original_job_ids'], original)
        self.assertEqual(len(result['scheduled_job_ids']), 8)
        self.assertEqual(len(set(result['scheduled_job_ids'])), 8)
        self.assertEqual(result['uncovered_groups'], [])
        self.assertTrue(all(set(ids).intersection(result['scheduled_job_ids']) for ids in groups.values()))
        self.assertEqual(set(result['engine_reserved']), set(groups)-{'original'})
        self.assertEqual(len(result['dropped_original_job_ids']), 5)

    def test_overlapping_coverage_uses_one_candidate(self):
        mapping = {'model': {}, 'shared': {}}
        result = supplement_selection(['model'], mapping,
                                      dict(recent=['shared'], raw_history=['shared'], custody=['shared']))
        self.assertEqual(result['scheduled_job_ids'], ['model', 'shared'])
        self.assertEqual(set(result['engine_reserved'].values()), {'shared'})

    def test_already_selected_coverage_is_not_engine_attributed(self):
        result = supplement_selection(['a', 'b'], {'a': {}, 'b': {}},
                                      dict(original=['a'], recent=['b'], custody=['a', 'b']))
        self.assertEqual(result['scheduled_job_ids'], ['a', 'b'])
        self.assertEqual(result['engine_reserved'], {})

    def test_more_than_eight_disjoint_groups_reports_unsatisfied(self):
        mapping = {str(i): {} for i in range(10)}
        groups = {'group'+str(i): [str(i)] for i in range(10)}
        result = supplement_selection(['0'], mapping, groups)
        self.assertEqual(len(result['scheduled_job_ids']), 8)
        self.assertEqual(len(result['uncovered_groups']), 2)

    def test_unknown_and_invalid_model_ids_rejected(self):
        mapping = {'a': {}, 'b': {}}
        for ids in ([], ['a', 'a'], ['unknown'], 'a'):
            with self.assertRaises(ValueError):
                supplement_selection(ids, mapping, {})
        with self.assertRaises(ValueError):
            supplement_selection(['a'], mapping, {'recent': ['unknown']})


if __name__ == '__main__':
    unittest.main()
