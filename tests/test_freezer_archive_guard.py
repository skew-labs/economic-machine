from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from economic_machine.arbitrum_research import freezer_archive_guard as guard
from economic_machine.arbitrum_research.freezer_archive_storage import ArchiveStorage,archive_storage
from economic_machine.pump_research.research_cloud import Cloud


class InventoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.cache=guard.InventoryCache()
        self.now=10.0
        self.clock=patch.object(guard.time,'monotonic',side_effect=lambda:self.now)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.scan=patch('economic_machine.pump_research.history_core.used_bytes',return_value=100).start()
        self.addCleanup(patch.stopall)
        self.disk=patch('economic_machine.pump_research.research_cloud.shutil.disk_usage',return_value=SimpleNamespace(free=10**12)).start()
        self.env=patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_SHARED_INVENTORY':'1'});self.env.start();self.addCleanup(self.env.stop)

    def cloud(self,root=None,hash_='a'*64):
        cloud=Cloud.__new__(Cloud);cloud.base=root or self.root
        cloud.local_bytes=0;cloud.last_storage_check=0
        cloud.spend=SimpleNamespace(hash=hash_,grant=dict(local_storage_bytes=240518168576,minimum_host_free_bytes=137438953472))
        return cloud

    def installed(self,**kwargs):
        cloud=self.cloud(**kwargs);self.assertTrue(guard.install_archive_guard(cloud,self.cache));return cloud

    def test_hit_reuses_scan_but_calls_original_fresh_disk_check(self):
        a=self.installed();b=self.installed()
        a.guard_local(10);b.guard_local(20)
        self.assertEqual(self.scan.call_count,1);self.assertEqual(self.disk.call_count,2)
        self.assertEqual(a.last_storage_check,b.last_storage_check)
        self.assertIsNot(a.spend,b.spend)
        self.assertEqual(self.cache.snapshot()['cache_hits'],1)

    def test_hits_never_extend_scan_completion_timestamp(self):
        a=self.installed();a.guard_local(0)
        self.now=11.9;b=self.installed();b.guard_local(0)
        self.assertEqual(b.last_storage_check,10.0)
        self.now=12.0001;c=self.installed();c.guard_local(0)
        self.assertEqual(c.last_storage_check,12.0001)
        self.assertEqual(self.scan.call_count,2)

    def test_exact_two_second_observation_uses_original_rule(self):
        self.installed().guard_local(0);self.now=12.0;self.installed().guard_local(0)
        self.assertEqual(self.scan.call_count,1)

    def test_changed_grant_hash_forces_scan(self):
        self.installed().guard_local(0);self.installed(hash_='b'*64).guard_local(0)
        self.assertEqual(self.scan.call_count,2)

    def test_different_root_forces_scan(self):
        other=self.root/'other';other.mkdir()
        self.installed().guard_local(0);self.installed(root=other).guard_local(0)
        self.assertEqual(self.scan.call_count,2)

    def test_replaced_root_inode_forces_scan(self):
        path=self.root/'shared';path.mkdir();self.installed(root=path).guard_local(0)
        path.rename(self.root/'retained-root');path.mkdir();self.installed(root=path).guard_local(0)
        self.assertEqual(self.scan.call_count,2)

    def test_same_resolved_root_alias_can_share_scan(self):
        target=self.root/'shared';target.mkdir();alias=self.root/'alias';alias.symlink_to(target,target_is_directory=True)
        self.installed(root=target).guard_local(0);self.installed(root=alias).guard_local(0)
        self.assertEqual(self.scan.call_count,1)

    def test_failed_scan_not_published_or_used_by_waiter(self):
        self.scan.side_effect=[OSError('inventory unavailable'),101]
        with self.assertRaisesRegex(OSError,'inventory unavailable'):self.installed().guard_local(0)
        self.assertEqual(self.cache.snapshot()['entries'],0)
        b=self.installed();b.guard_local(0)
        self.assertEqual(b.local_bytes,101);self.assertEqual(self.scan.call_count,2)

    def test_fresh_host_free_check_can_fail_on_cached_inventory(self):
        self.installed().guard_local(0)
        self.disk.return_value=SimpleNamespace(free=137438953472+9)
        with self.assertRaisesRegex(ValueError,'LOCAL_FREE_SPACE'):self.installed().guard_local(10)
        self.assertEqual(self.scan.call_count,1);self.assertEqual(self.disk.call_count,2)
        self.assertEqual(self.cache.snapshot()['entries'],0)

    def test_original_local_limit_and_128mib_margin_remain(self):
        a=self.installed();a.guard_local(0)
        b=self.installed();b.spend.grant['local_storage_bytes']=100+128*1024**2+10
        with self.assertRaisesRegex(ValueError,'LOCAL_STORAGE_LIMIT'):b.guard_local(11)
        self.assertEqual(self.scan.call_count,1)
        self.assertEqual(self.cache.snapshot()['entries'],0)

    def test_backward_clock_does_not_reuse_future_observation(self):
        self.installed().guard_local(0);self.now=9.0;self.installed().guard_local(0)
        self.assertEqual(self.scan.call_count,2)

    def test_cache_is_bounded_and_evicted_identity_refreshes(self):
        self.cache=guard.InventoryCache(2)
        for value in ('a','b','c'):self.installed(hash_=value*64).guard_local(0)
        self.assertEqual(self.cache.snapshot()['entries'],2)
        self.installed().guard_local(0);self.assertEqual(self.scan.call_count,4)

    def test_three_concurrent_instances_single_flight_one_scan(self):
        start=threading.Barrier(3)
        def inventory(base):time.sleep(.04);return 100
        self.scan.side_effect=inventory
        def worker(_):
            cloud=self.installed();start.wait(timeout=2);cloud.guard_local(8);return cloud
        with ThreadPoolExecutor(max_workers=3) as pool:clouds=list(pool.map(worker,range(3)))
        self.assertEqual(self.scan.call_count,1);self.assertEqual(self.disk.call_count,3)
        self.assertEqual(len({id(c.spend) for c in clouds}),3)
        self.assertEqual(self.cache.snapshot()['cache_hits'],2)

    def test_failed_first_waiter_leaves_next_waiter_to_rescan(self):
        start=threading.Barrier(2)
        def inventory(base):
            if self.scan.call_count==1:
                time.sleep(.04);raise OSError('scan failed')
            return 100
        self.scan.side_effect=inventory
        def worker(_):
            cloud=self.installed();start.wait(timeout=2)
            try:cloud.guard_local(0);return 'ok'
            except OSError:return 'failed'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
        self.assertEqual(sorted(results),['failed','ok']);self.assertEqual(self.scan.call_count,2)

    def test_invalid_identity_fails_without_inventory_or_admission(self):
        a=self.installed(hash_='not-a-grant-hash')
        with self.assertRaisesRegex(ValueError,'IDENTITY'):a.guard_local(0)
        self.scan.assert_not_called();self.disk.assert_not_called()

    def test_off_setting_keeps_original_method(self):
        cloud=self.cloud();method=cloud.guard_local
        with patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_SHARED_INVENTORY':'0'}):
            self.assertFalse(guard.install_archive_guard(cloud,self.cache))
        self.assertEqual(cloud.guard_local,method)

    def test_bad_setting_and_double_install_fail_closed(self):
        cloud=self.cloud()
        with patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_SHARED_INVENTORY':'yes'}):
            with self.assertRaisesRegex(ValueError,'SETTING'):guard.install_archive_guard(cloud,self.cache)
        guard.install_archive_guard(cloud,self.cache)
        with self.assertRaisesRegex(ValueError,'ALREADY_INSTALLED'):guard.install_archive_guard(cloud,self.cache)

    def test_archive_factory_off_never_changes_ordinary_storage(self):
        ordinary=Mock(return_value='ordinary')
        with patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_DIRECT_RESTORE':'0'}),patch.object(guard,'install_archive_guard') as installer:
            self.assertEqual(archive_storage('a','b',chain_id=42161,ordinary_storage=ordinary),'ordinary')
            installer.assert_not_called()

    def test_archive_init_attaches_only_its_own_cloud_and_closes_on_failure(self):
        cloud=self.cloud();cloud.close=Mock()
        def initialize(storage,*args,**kwargs):storage.cloud=cloud
        with patch('economic_machine.arbitrum_research.freezer_archive_storage.Storage.__init__',initialize):
            ArchiveStorage('unused','unused',chain_id=42161)
        self.assertTrue(getattr(cloud.guard_local,'_archive_inventory_guard',False))
        with patch('economic_machine.arbitrum_research.freezer_archive_storage.Storage.__init__',initialize),patch.dict(os.environ,{'EM_FREEZER_ARCHIVE_SHARED_INVENTORY':'bad'}):
            with self.assertRaisesRegex(ValueError,'SETTING'):ArchiveStorage('unused','unused',chain_id=42161)
        cloud.close.assert_called_once()


if __name__=='__main__':unittest.main()
