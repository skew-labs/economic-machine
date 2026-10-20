import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research import freezer_cache_pressure as pressure


class CachePressureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base/'evidence/freezer-search-v1/cloud-cache'
        self.root.mkdir(parents=True); (self.base/'evidence/freezer-continuous-v1').mkdir()
        self.registry = sqlite3.connect(self.root.parent/'catalogue.sqlite3'); self.addCleanup(self.registry.close)
        self.registry.execute('create table shards(id text primary key,chain integer,path text,body text)')
        self.objects = sqlite3.connect(':memory:'); self.addCleanup(self.objects.close)
        self.objects.execute('create table objects(digest text,bytes integer,verified integer)')
        self.extra = 8 * 1024**3; self.free = self.extra + 1000 - 10
        self.cloud = SimpleNamespace(spend=SimpleNamespace(db=self.objects,grant={'minimum_host_free_bytes':1000}))
        self.clock = patch.object(pressure.time,'time_ns',return_value=1792175000000000000); self.clock_mock=self.clock.start(); self.addCleanup(self.clock.stop)
        self.disk = patch.object(pressure.shutil,'disk_usage',side_effect=lambda p:SimpleNamespace(free=self.free)); self.disk.start(); self.addCleanup(self.disk.stop)

    def fixture(self, raw=b'index data here', name=None):
        digest=hashlib.sha256(raw).hexdigest(); p=self.root/((name or digest)+'.sqlite3');p.write_bytes(raw)
        def proof(content):
            h=hashlib.sha256(content).hexdigest();self.objects.execute('insert into objects values(?,?,1)',(h,len(content)))
            return dict(schema='economic-machine.arbitrum-object.v1',chain_id=42161,encoding='gzip',complete_cloud_readback=True,
                        raw_sha256=h,raw_bytes=len(content),cloud=dict(sha256=h,bytes=len(content)))
        part=proof(raw)
        manifest=dict(schema='economic-machine.freezer-cloud-index.v1',chain_id=42161,index_sha256=digest,bytes=len(raw),parts=[dict(offset=0,proof=part)])
        raw_manifest=json.dumps(manifest).encode();mp=self.base/(digest+'.json');mp.write_bytes(raw_manifest)
        ref=dict(path=str(mp),sha256=hashlib.sha256(raw_manifest).hexdigest(),proof=proof(raw_manifest))
        body=dict(prefer_cloud_index=True,cloud_index=ref)
        self.registry.execute('insert into shards values(?,?,?,?)',(digest,42161,str(self.base/'original.sqlite3'),json.dumps(body)));self.registry.commit()
        return p,mp,body

    def call(self):return pressure.evict_for_headroom(self.base,self.cloud,self.extra)

    def test_verified_duplicate_only(self):
        p,mp,_=self.fixture();unknown=self.root/'unknown.tmp';unknown.write_bytes(b'keep')
        result=self.call();self.assertEqual(result['state'],'EVICTED');self.assertFalse(p.exists())
        self.assertTrue(mp.exists());self.assertTrue(unknown.exists());self.assertEqual(self.registry.execute('select count(*) from shards').fetchone()[0],1)
        self.assertEqual(len(list((self.base/'evidence/freezer-continuous-v1/cache-pressure').glob('*.json'))),2)

    def test_no_pressure_no_unlink(self):
        p,_,_=self.fixture();self.free+=10;self.assertEqual(self.call()['state'],'NOT_NEEDED');self.assertTrue(p.exists())

    def test_active_reader_lease_stays_paused(self):
        p,_,_=self.fixture()
        with (self.root/'cache.lock').open('a+b') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.call()['state'],'READER_BUSY');self.assertTrue(p.exists())

    def test_manifest_mismatch_rejects_before_any_unlink(self):
        p,mp,_=self.fixture();mp.write_bytes(mp.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'MANIFEST_SCOPE'):self.call()
        self.assertTrue(p.exists())

    def test_file_hash_mismatch(self):
        p,_,_=self.fixture();p.write_bytes(b'broken data now')
        with self.assertRaisesRegex(ValueError,'MANIFEST_SCOPE|FILE_CHANGED'):self.call()
        self.assertTrue(p.exists())

    def test_unverified_object_rejected(self):
        p,_,_=self.fixture();self.objects.execute('update objects set verified=0')
        with self.assertRaisesRegex(ValueError,'UNVERIFIED_OBJECT'):self.call()
        self.assertTrue(p.exists())

    def test_unknown_files_preserved(self):
        p=self.root/'not-a-digest.sqlite3';p.write_bytes(b'original unknown')
        self.assertEqual(self.call()['state'],'NO_ELIGIBLE_CACHE');self.assertTrue(p.exists())

    def test_valid_name_unknown_registry_is_not_deleted(self):
        p=self.root/('a'*64+'.sqlite3');p.write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError,'UNKNOWN_SHARD'):self.call()
        self.assertTrue(p.exists())

    def test_nonempty_wal_preserved(self):
        p,_,_=self.fixture();wal=p.with_name(p.name+'-wal');wal.write_bytes(b'pending')
        with self.assertRaisesRegex(ValueError,'NONEMPTY_WAL'):self.call()
        self.assertTrue(p.exists());self.assertTrue(wal.exists())

    def test_deadline_blocks_mutation(self):
        p,_,_=self.fixture();self.clock_mock.return_value=pressure.DEADLINE*1000000
        with self.assertRaisesRegex(ValueError,'DEADLINE'):self.call()
        self.assertTrue(p.exists())

    def test_entire_selected_set_validated_first(self):
        p,_,_=self.fixture(b'first');q,mp,_=self.fixture(b'next');self.free-=100
        mp.write_bytes(mp.read_bytes()+b' ')
        with self.assertRaises(ValueError):self.call()
        self.assertTrue(p.exists());self.assertTrue(q.exists())

    def test_receipt_directory_sync_failure_blocks_unlink(self):
        p,_,_=self.fixture()
        import stat
        original=pressure.os.fsync
        def sync(fd):
            if stat.S_ISDIR(pressure.os.fstat(fd).st_mode):raise OSError('directory sync failure')
            original(fd)
        with patch.object(pressure.os,'fsync',side_effect=sync):
            with self.assertRaisesRegex(OSError,'directory sync failure'):self.call()
        self.assertTrue(p.exists())

    def test_wrong_headroom_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'HEADROOM'):pressure.evict_for_headroom(self.base,self.cloud,1)

    def test_limit_bounds_eviction(self):
        p,_,_=self.fixture();self.free-=100
        with patch.object(pressure,'MAX_EVICTION',1):self.assertEqual(self.call()['state'],'NO_ELIGIBLE_CACHE')
        self.assertTrue(p.exists())


class ControllerPressureTests(unittest.TestCase):
    def test_rechecks_original_guard_after_eviction(self):
        import freezer_continuous as controller
        from unittest.mock import Mock
        cloud=Mock()
        cloud.guard_local.side_effect=[ValueError('RESEARCH_CLOUD_LOCAL_FREE_SPACE'),None]
        with patch.object(pressure,'evict_for_headroom',return_value={'state':'EVICTED'}) as evict:
            self.assertEqual(controller.guard_batch_headroom(cloud,Path('/scope')),{'state':'EVICTED'})
        self.assertEqual(cloud.guard_local.call_count,2)
        self.assertEqual(cloud.guard_local.call_args.args,(8*1024**3,))
    def test_still_pauses_when_reader_busy_and_floor_not_met(self):
        import freezer_continuous as controller
        from unittest.mock import Mock
        cloud=Mock();cloud.guard_local.side_effect=ValueError('RESEARCH_CLOUD_LOCAL_FREE_SPACE')
        with patch.object(pressure,'evict_for_headroom',return_value={'state':'READER_BUSY'}):
            with self.assertRaisesRegex(ValueError,'LOCAL_FREE_SPACE'):controller.guard_batch_headroom(cloud,Path('/scope'))
    def test_local_inventory_limit_not_bypassed(self):
        import freezer_continuous as controller
        from unittest.mock import Mock
        cloud=Mock();cloud.guard_local.side_effect=ValueError('RESEARCH_CLOUD_LOCAL_STORAGE_LIMIT')
        with patch.object(pressure,'evict_for_headroom') as evict:
            with self.assertRaisesRegex(ValueError,'LOCAL_STORAGE_LIMIT'):controller.guard_batch_headroom(cloud,Path('/scope'))
            evict.assert_not_called()
