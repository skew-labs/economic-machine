import json
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'products/arbitrum_research'))
from freezer_continuous import immutable, import_windows, measure, next_batch, retryable, sample, implementation_amendment, plan_avoiding_registered, guard_batch_headroom


class ContinuousTests(unittest.TestCase):
    def test_batch_leaves_archive_headroom_above_original_floor(self):
        from unittest.mock import Mock
        cloud=Mock()
        guard_batch_headroom(cloud)
        cloud.guard_local.assert_called_once_with(8*1024**3)

    def test_headroom_admission_preserves_resource_errors(self):
        from unittest.mock import Mock
        for code in ('RESEARCH_CLOUD_LOCAL_FREE_SPACE','RESEARCH_CLOUD_LOCAL_STORAGE_LIMIT'):
            cloud=Mock();cloud.guard_local.side_effect=ValueError(code)
            with self.assertRaisesRegex(ValueError,code):guard_batch_headroom(cloud)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.db = sqlite3.connect(':memory:'); self.addCleanup(self.db.close)
        self.db.executescript('create table pages(first integer primary key,last integer,blocks integer,body text); create table measurements(id integer primary key,body text);')
        self.registry = sqlite3.connect(':memory:'); self.addCleanup(self.registry.close)
        self.registry.execute('create table shards(id text,first integer,last integer,chain integer,path text)')
        self.window = dict(start=100, end=109, blocks=10, index_sha256='abc', registered_at_ms=200000,
                           network_bytes=1000, network_requests=1)
        self.registry.execute('insert into shards values(?,?,?,?,?)', ('abc',100,109,42161,str(self.folder/'window-100-109/index.sqlite3')))
        self.child = dict(state='COMPLETE_BOUNDED_WINDOW', next_block=110, windows=[self.window])

    def test_bounded_child_and_end(self):
        self.assertEqual(next_batch(10, 1000000), (10, 262153))
        self.assertEqual(next_batch(11, 12), (11, 12))
        self.assertIsNone(next_batch(13, 12))

    def test_planner_splits_before_and_skips_verified_probe(self):
        proof=dict(first=500000000,last=500000063,index_sha256='a'*64)
        self.assertEqual(plan_avoiding_registered(499999900,500100000,[proof]),dict(kind='acquire',first=499999900,last=499999999))
        self.assertEqual(plan_avoiding_registered(500000000,500100000,[proof]),dict(kind='skip_registered',first=500000000,last=500000063,proof=proof))
        self.assertEqual(plan_avoiding_registered(500000064,500100000,[proof]),dict(kind='acquire',first=500000064,last=500100000))

    def test_planner_handles_adjacent_nonoverlap_probes_and_end(self):
        a=dict(first=100,last=163);b=dict(first=164,last=675)
        self.assertEqual(plan_avoiding_registered(164,900,[a,b])['last'],675)
        self.assertEqual(plan_avoiding_registered(676,900,[a,b])['kind'],'acquire')
        self.assertEqual(plan_avoiding_registered(120,150,[a])['last'],150)
        self.assertIsNone(plan_avoiding_registered(151,150,[a]))

    def test_complete_registration_required(self):
        self.registry.execute('delete from shards')
        with self.assertRaisesRegex(ValueError, 'UNREGISTERED'):
            import_windows(self.db, self.registry, self.folder, self.child, 100, 109)
        self.assertEqual(self.db.execute('select count(*) from pages').fetchone()[0], 0)

    def test_gap_rejected(self):
        self.child['windows'][0]['start'] = 101
        with self.assertRaisesRegex(ValueError, 'GAP'):
            import_windows(self.db, self.registry, self.folder, self.child, 100, 109)

    def test_cursor_cannot_jump_registered_prefix(self):
        self.child['next_block'] = 111
        with self.assertRaisesRegex(ValueError, 'CURSOR_WITHOUT_PROOF'):
            import_windows(self.db, self.registry, self.folder, self.child, 100, 109)

    def test_reconcile_idempotent(self):
        for _ in range(2):
            self.assertEqual(import_windows(self.db,self.registry,self.folder,self.child,100,109),110)
        self.assertEqual(self.db.execute('select count(*) from pages').fetchone()[0],1)

    def test_changed_retained_page_rejected(self):
        import_windows(self.db,self.registry,self.folder,self.child,100,109)
        self.child['windows'][0]['network_bytes'] = 99
        with self.assertRaisesRegex(ValueError,'PAGE_CHANGED'):
            import_windows(self.db,self.registry,self.folder,self.child,100,109)

    def test_false_complete_rejected(self):
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_BATCH'):
            import_windows(self.db,self.registry,self.folder,self.child,100,110)

    def test_wall_clock_includes_waits(self):
        import_windows(self.db,self.registry,self.folder,self.child,100,109)
        value=sample(self.db,100000,1100000)
        self.assertEqual(value['elapsed_seconds'],1000)
        self.assertEqual(value['blocks_per_second'],.01)

    def test_no_short_sustained_measurement(self):
        import_windows(self.db,self.registry,self.folder,self.child,100,109)
        measure(self.db,100000,699999)
        self.assertEqual(self.db.execute('select count(*) from measurements').fetchone()[0],0)

    def test_sustained_measurements_nonoverlap_and_not_fully_cold(self):
        import_windows(self.db,self.registry,self.folder,self.child,100,109)
        measure(self.db,100000,700000)
        measure(self.db,100000,1300000)
        rows=[json.loads(r[0]) for r in self.db.execute('select body from measurements order by id')]
        self.assertEqual([r['blocks'] for r in rows],[10,0])
        self.assertFalse(rows[0]['fully_cold'])
        self.assertTrue(rows[0]['search_committed'])
        self.assertEqual(rows[0]['ended_at_ms'],rows[1]['started_at_ms'])

    def test_only_transient_source_errors_retry(self):
        p=self.folder/'acquisition-error.json'
        for kind,message,expected in [('URLError','network',True),('HTTPError','HTTP Error 429: limited',True),
                ('HTTPError','HTTP Error 403: forbidden',False),('ValueError','SNAPSHOT_SOURCE_CHANGED',False),
                ('ValueError','RESEARCH_MONTHLY_BUDGET_LIMIT',False)]:
            p.write_text(json.dumps(dict(error_type=kind,error=message)))
            self.assertEqual(retryable(self.folder),expected)

    def test_policy_never_overwrites_authority(self):
        p=self.folder/'policy.json';immutable(p,dict(limit=1));immutable(p,dict(limit=1))
        with self.assertRaisesRegex(ValueError,'POLICY_CHANGED'):immutable(p,dict(limit=None))
        self.assertEqual(json.loads(p.read_bytes()),dict(limit=1))

    def test_implementation_amendment_preserves_authority(self):
        from economic_machine.arbitrum_research.freezer_source import DEADLINE
        value=dict(schema='economic-machine.freezer-implementation-amendment.v1',original_policy_sha256='a'*64,
                   pipeline_sha256='b'*64,consumers=3,max_inflight=4,deadline_ms=DEADLINE,monthly_budget_krw=1000000,
                   reset_cursor=False,first_batch=13375104,authorized_at_ms=1)
        p=self.folder/'amendment.json';p.write_text(json.dumps(value));digest=hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertEqual(implementation_amendment(p,digest,'a'*64,'b'*64),value)
        for key,wrong in [('consumers',4),('reset_cursor',True),('monthly_budget_krw',2000000)]:
            bad=dict(value,**{key:wrong});p.write_text(json.dumps(bad));digest=hashlib.sha256(p.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'AMENDMENT_SCOPE'):implementation_amendment(p,digest,'a'*64,'b'*64)


if __name__=='__main__':unittest.main()
