import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from economic_machine.pump_research.research_workspace import Workspace


class PoolWorkspaceStatus(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        (self.base/'evidence/wallet-map-v1').mkdir(parents=True)
        self.pool=self.base/'pool'
        (self.pool/'evidence/muse-pool-v1').mkdir(parents=True)
        self.path=self.pool/'evidence/muse-pool-v1/status.json'
        self.env=patch.dict('os.environ',{'EM_RESEARCH_POOL_ROOT':str(self.pool)})
        self.env.start();self.workspace=Workspace(self.base)
    def tearDown(self):
        self.workspace.close();self.env.stop();self.tmp.cleanup()
    def write(self,**changes):
        value=dict(configured=50,active=17,updated_ms=int(time.time()*1000),state='RUNNING',credential='must not leak')
        value.update(changes);self.path.write_text(json.dumps(value))
    def test_live_pool_replaces_old_batch_status_and_whitelists_fields(self):
        self.write();value=self.workspace.snapshot()['workers']
        self.assertEqual(value['active'],17);self.assertTrue(value['shared_pool'])
        self.assertNotIn('credential',value)
    def test_missing_pool_is_stale_not_old_planner_running(self):
        (self.base/'evidence/wallet-map-v1/workers.json').write_text(json.dumps(dict(configured=100,active=100,updated_ms=int(time.time()*1000))))
        value=self.workspace.snapshot()['workers']
        self.assertEqual(value['active'],0);self.assertTrue(value['stale'])
    def test_stale_pool_does_not_claim_active_calls(self):
        self.write(updated_ms=1);value=self.workspace.snapshot()['workers']
        self.assertEqual(value['active'],0);self.assertTrue(value['stale'])
    def test_invalid_overflow_does_not_claim_more_than50(self):
        self.write(active=51);self.assertEqual(self.workspace.snapshot()['workers']['active'],0)

if __name__=='__main__':unittest.main()
