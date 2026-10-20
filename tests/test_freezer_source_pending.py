from pathlib import Path
import unittest
import test_freezer_source_pipeline as baseline
from economic_machine.arbitrum_research.freezer_source_pipeline import pending_range_limit
class Bound(unittest.TestCase):
 def test_only_explicit_two_or_four(self):
  self.assertEqual(pending_range_limit("2"),2);self.assertEqual(pending_range_limit("4"),4)
  for value in ("0","3","5","100",2,4,None,"4.0"):
   with self.assertRaises(ValueError):pending_range_limit(value)
class FourPending(baseline.PipelinedSourceTests):
 def test_four_ranges_can_wait_without_committing_unverified_data(self):
  self.s.max_pending_ranges=4
  for i in range(4):self.s.read(8+32*i,32)
  self.assertEqual(len(self.s.pending),4)
  self.assertEqual(self.s.db.execute("select count(*) from ranges").fetchone()[0],0)
  self.assertEqual(self.s.db.execute("select count(*) from attempts where state='reserved'").fetchone()[0],4)
  self.s.uploads.finish(0);self.s.read(136,32)
  self.assertEqual(len(self.s.pending),4);self.assertEqual(self.s.metrics["max_pending"],4)
  self.assertEqual(self.s.db.execute("select count(*) from ranges").fetchone()[0],1)
 def test_oldest_failure_prevents_fifth_and_retains_all_four(self):
  self.s.max_pending_ranges=4
  for i in range(4):self.s.read(8+32*i,32)
  self.s.uploads.finish(0,error=ValueError("oldest failed"))
  with self.assertRaisesRegex(ValueError,"oldest failed"):self.s.read(136,32)
  self.assertEqual(len(self.s.pending),4);self.assertEqual(len(self.s.uploads.jobs),4)
  self.assertEqual(self.s.db.execute("select count(*) from attempts").fetchone()[0],4)
  self.assertEqual(len(list(self.s.out.glob("ranges/*.raw"))),4)
 def test_drain_requires_every_full_blob_proof(self):
  self.s.max_pending_ranges=4
  for i in range(4):self.s.read(8+32*i,32)
  for i in range(4):self.s.uploads.finish(i,wrong=i==2)
  with self.assertRaisesRegex(ValueError,"BLOB_PROOF"):self.s.drain()
  self.assertEqual(self.s.db.execute("select count(*) from ranges").fetchone()[0],2)
  self.assertEqual(self.s.db.execute("select count(*) from attempts where state='reserved'").fetchone()[0],2)
if __name__=="__main__":unittest.main()
