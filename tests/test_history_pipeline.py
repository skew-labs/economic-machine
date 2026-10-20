import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from economic_machine.pump_research.parallel_archive import upload_parts
from economic_machine.pump_research.research_cloud import archive_file,retire_file,CloudFile
from economic_machine.robinhood_research.rpc_gate import Gate
from economic_machine.arbitrum_research.source import SourceBackoff
from test_pump_research_cloud import FakeCloud
from test_pump_research_spend import setup_budget

class UploadTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name);setup_budget(self.base);self.cloud=FakeCloud(self.base);self.addCleanup(self.cloud.close)
  self.raw=bytes(range(251))*20;self.path=self.base/'source';self.path.write_bytes(self.raw)
  self.expected=dict(bytes=len(self.raw),sha256=hashlib.sha256(self.raw).hexdigest());self.ref=self.base/'reference.json'
 def test_parallel_parts_restore_exact_order_and_retirement_requires_proof(self):
  active=[0,0];lock=threading.Lock()
  class Slow(FakeCloud):
   def upload(self,path,expected=None):
    with lock:active[0]+=1;active[1]=max(active)
    try:time.sleep(.01);return super().upload(path,expected)
    finally:
     with lock:active[0]-=1
  with patch('economic_machine.pump_research.research_cloud.Cloud',Slow),patch('economic_machine.pump_research.research_cloud.BLOCK',128):
   value=archive_file(self.cloud,self.path,self.ref,self.expected,workers=4)
   self.assertGreater(active[1],1)
   self.assertEqual([x['offset'] for x in value['blocks']],list(range(0,len(self.raw),128)))
   reader=CloudFile(self.base,self.ref,self.expected)
   try:self.assertEqual(reader.read(0,len(self.raw)),self.raw)
   finally:reader.close()
   self.assertEqual(retire_file(self.cloud,self.path,self.ref,self.expected),len(self.raw))
   self.assertFalse(self.path.exists())
 def test_failed_lane_keeps_original_and_never_publishes_complete_reference(self):
  class Broken(FakeCloud):
   def upload(self,path,expected=None):raise RuntimeError('readback failed')
  with patch('economic_machine.pump_research.research_cloud.Cloud',Broken),patch('economic_machine.pump_research.research_cloud.BLOCK',128):
   with self.assertRaises(RuntimeError):archive_file(self.cloud,self.path,self.ref,self.expected,workers=4)
  self.assertEqual(self.path.read_bytes(),self.raw);self.assertFalse(self.ref.exists())
 def test_source_hash_mismatch_rejects_complete_reference(self):
  with patch('economic_machine.pump_research.research_cloud.Cloud',FakeCloud),patch('economic_machine.pump_research.research_cloud.BLOCK',128):
   with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):
    archive_file(self.cloud,self.path,self.ref,dict(self.expected,sha256='0'*64),workers=4)
  self.assertTrue(self.path.exists());self.assertFalse(self.ref.exists())
 def test_workers_are_bounded(self):
  with self.assertRaises(ValueError):archive_file(self.cloud,self.path,self.ref,self.expected,workers=5)
 def test_shared_capacity_guard_is_serialized_and_failure_preserves_source(self):
  lock=threading.Lock();calls=[]
  def guard(extra):
   self.assertTrue(lock.acquire(blocking=False))
   try:time.sleep(.001);calls.append(extra)
   finally:lock.release()
  upload_parts(self.base,self.path,len(self.raw),128,4,FakeCloud,guard=guard)
  self.assertGreater(len(calls),4)
  with self.assertRaisesRegex(ValueError,'CAPACITY'):
   upload_parts(self.base,self.path,len(self.raw),128,4,FakeCloud,
    guard=lambda _:(_ for _ in ()).throw(ValueError('CAPACITY')))
  self.assertEqual(self.path.read_bytes(),self.raw)

class GateTests(unittest.TestCase):
 def test_retry_after_is_shared_and_endpoints_are_independent(self):
  with tempfile.TemporaryDirectory() as root:
   now=[1000.];clock=lambda:now[0]
   with self.assertRaises(SourceBackoff):
    with Gate(root,'provider-a',16,clock=clock):raise SourceBackoff(900,429)
   with self.assertRaises(SourceBackoff) as e:
    with Gate(root,'provider-a',1,clock=clock):pass
   self.assertEqual(e.exception.retry_after_seconds,900);self.assertEqual(e.exception.status,-1)
   with Gate(root,'provider-b',1,clock=clock):pass
   now[0]=1901
   with Gate(root,'provider-a',1,clock=clock):pass
 def test_counted_rpc_units_and_shared_pacing(self):
  with tempfile.TemporaryDirectory() as root:
   now=[1000.];clock=lambda:now[0]
   def sleep(delay):now[0]+=delay
   with Gate(root,'p',16,clock,sleep):pass
   with Gate(root,'p',1,clock,sleep):pass
   self.assertAlmostEqual(now[0],1000.32)
   p=next(Path(root).glob('*/state.json'));state=json.loads(p.read_text())
   self.assertEqual((state['requests'],state['rpc_units']),(2,17))

import test_arbitrum_runtime as runtime_fixtures
class AuditIsolationTests(unittest.TestCase):
 setUp=runtime_fixtures.RuntimeTests.setUp
 tearDown=runtime_fixtures.RuntimeTests.tearDown
 def test_comparison_provider_backoff_does_not_pause_ingestion_provider(self):
  page=self.r.one();before=self.r.state['backoff_until']
  with patch.object(self.r,'audit',side_effect=SourceBackoff(800,429)):
   self.r.scheduled_audit(page)
  self.assertEqual(self.r.state['backoff_until'],before)
  self.assertGreater(self.r.state['audit_not_before'],time.time()+790)
  self.assertEqual(json.loads((self.r.out/'audit-retry.json').read_text())['state'],'INDEPENDENT_AUDIT_PENDING')
 def test_integrity_mismatch_still_stops(self):
  page=self.r.one()
  with patch.object(self.r,'audit',side_effect=ValueError('MISMATCH')):
   with self.assertRaises(ValueError):self.r.scheduled_audit(page)
