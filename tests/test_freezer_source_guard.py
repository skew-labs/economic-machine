from pathlib import Path
from types import SimpleNamespace
import os
import unittest
from unittest.mock import Mock,patch
import test_freezer_archive_guard as baseline
from economic_machine.arbitrum_research import freezer_source_guard as guard
from economic_machine.arbitrum_research.freezer_source_pipeline import UploadLane
class SourceGuard(baseline.InventoryGuardTests):
 def setUp(self):
  super().setUp()
  env=patch.dict(os.environ,{"EM_FREEZER_SOURCE_SHARED_INVENTORY":"1"})
  env.start();self.addCleanup(env.stop)
 def installed(self,**kwargs):
  cloud=self.cloud(**kwargs);self.assertTrue(guard.install_source_guard(cloud,self.cache));return cloud
 def test_source_off_does_not_change_cloud(self):
  cloud=self.cloud();method=cloud.guard_local
  with patch.dict(os.environ,{"EM_FREEZER_SOURCE_SHARED_INVENTORY":"0"}):
   self.assertFalse(guard.install_source_guard(cloud,self.cache))
  self.assertEqual(cloud.guard_local,method)
 def test_source_flag_is_separate_from_archive(self):
  cloud=self.cloud()
  with patch.dict(os.environ,{"EM_FREEZER_SOURCE_SHARED_INVENTORY":"0","EM_FREEZER_ARCHIVE_SHARED_INVENTORY":"1"}):
   self.assertFalse(guard.install_source_guard(cloud,self.cache))
 def test_invalid_source_flag_rejected(self):
  with patch.dict(os.environ,{"EM_FREEZER_SOURCE_SHARED_INVENTORY":"2"}):
   with self.assertRaisesRegex(ValueError,"SOURCE_INVENTORY_SETTING"):guard.install_source_guard(self.cloud())
 def test_source_duplicate_install_rejected(self):
  cloud=self.installed()
  with self.assertRaisesRegex(ValueError,"ALREADY_INSTALLED"):guard.install_source_guard(cloud)
 def test_upload_lane_installs_guard_in_own_thread(self):
  cloud=self.cloud();cloud.close=Mock();cloud.upload=Mock(return_value={"verified":True})
  lane=UploadLane(self.root,factory=lambda base:cloud)
  try:
   with patch("economic_machine.arbitrum_research.freezer_source_pipeline.install_source_guard",wraps=lambda c:guard.install_source_guard(c,self.cache)) as install:
    self.assertEqual(lane.pool.submit(lane.upload,self.root/"fake").result(),{"verified":True})
    install.assert_called_once_with(cloud)
   self.assertTrue(getattr(cloud.guard_local,"_source_inventory_guard",False))
  finally:lane.close()
 def test_failed_lane_install_closes_private_cloud(self):
  cloud=self.cloud();cloud.close=Mock();cloud.upload=Mock()
  lane=UploadLane(self.root,factory=lambda base:cloud)
  try:
   with patch("economic_machine.arbitrum_research.freezer_source_pipeline.install_source_guard",side_effect=ValueError("guard error")):
    with self.assertRaisesRegex(ValueError,"guard error"):lane.pool.submit(lane.upload,self.root/"fake").result()
   cloud.close.assert_called_once();cloud.upload.assert_not_called();self.assertIsNone(lane.cloud)
  finally:lane.close()
if __name__=="__main__":unittest.main()
