import importlib.util,queue,threading,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('pipeline',Path(__file__).parents[1]/'products/arbitrum_research/freezer_pipeline.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class PipelineTests(unittest.TestCase):
 def test_exact_contiguous_bounded_pages(self):
  pages=p.pages(10131072,10393215);self.assertEqual(len(pages),16);self.assertEqual(sum(c for _,c in pages),262144)
  self.assertTrue(all(a+c==b for (a,c),(b,_) in zip(pages,pages[1:])))
 def test_partial_final_page(self):self.assertEqual(p.pages(8,16394),[(8,16384),(16392,3)])
 def test_bad_scope(self):
  for a,b in [(-1,8),(9,8),(0,262144),(True,8)]:
   with self.assertRaises(ValueError):p.pages(a,b)
 def test_cancelled_publish_retains_queue(self):
  q=queue.Queue(1);q.put('existing');stop=threading.Event();stop.set();self.assertFalse(p.publish(q,'new',stop));self.assertEqual(q.get(),'existing')
 def test_stopped_producer_does_not_take_slot(self):
  slots=threading.BoundedSemaphore(2);stop=threading.Event();stop.set();self.assertFalse(p.acquire_slot(slots,stop));self.assertTrue(slots.acquire(False));self.assertTrue(slots.acquire(False));self.assertFalse(slots.acquire(False))
 def test_two_inflight_limit(self):
  slots=threading.BoundedSemaphore(2);stop=threading.Event();self.assertTrue(p.acquire_slot(slots,stop));self.assertTrue(p.acquire_slot(slots,stop));self.assertFalse(slots.acquire(False))
 def test_all_source_proofs_drain_before_handoff_data_returns(self):
  events=[]
  class Source:
   def table(self,name,start,count):events.append(name);return [name]
   def drain(self):events.append('proofs-verified')
  data=p.acquire_tables(Source(),5,1);self.assertEqual(events,['headers','hashes','bodies','receipts','proofs-verified']);self.assertEqual(len(data),4)
 def test_source_proof_failure_cannot_return_handoff_data(self):
  class Source:
   def table(self,*args):return []
   def drain(self):raise ValueError('unverified-source')
  with self.assertRaisesRegex(ValueError,'unverified-source'):p.acquire_tables(Source(),5,1)

if __name__=='__main__':unittest.main()
