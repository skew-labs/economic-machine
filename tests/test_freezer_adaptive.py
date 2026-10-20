
import base64,hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch
from economic_machine.arbitrum_research.freezer_adaptive import count_for_sizes,FIELDS,MAX_ENVELOPE,record_sizes
from economic_machine.pump_research.store import canonical
import freezer_pipeline_adaptive as adaptive
import freezer_continuous as continuous

def raw_request(chain,start,sizes,count):
 return (canonical(dict(chain=chain,blocks=[dict(number=start+i,**{key:base64.b64encode(bytes(sizes[name][i])).decode() for key,name in FIELDS}) for i in range(count)]))+'\n').encode()

class AdaptiveSizingTests(unittest.TestCase):
 def test_exact_prediction_all_base64_remainders(self):
  sizes={name:[0,1,2,3,4,5,6,7] for _,name in FIELDS}
  count,n=count_for_sizes(42161,999995,sizes)
  self.assertEqual(count,8);self.assertEqual(n,len(raw_request(42161,999995,sizes,count)))
 def test_reduced_page_never_exceeds_bound(self):
  sizes={name:[30]*8 for _,name in FIELDS};two=len(raw_request(42161,8,sizes,2))
  count,n=count_for_sizes(42161,8,sizes,limit=two)
  self.assertEqual(count,2);self.assertEqual(n,two)
  count,n=count_for_sizes(42161,8,sizes,limit=two-1)
  self.assertEqual(count,1);self.assertLess(n,two)
 def test_dense_modern_fixture_splits_before_large_body_fetch(self):
  sizes={name:[100 if name=='headers' else 32 if name=='hashes' else 2000]*16384 for _,name in FIELDS}
  count,n=count_for_sizes(42161,500100000,sizes)
  self.assertLess(count,16384);self.assertLessEqual(n,MAX_ENVELOPE)
  self.assertGreater(len(raw_request(42161,500100000,sizes,count+1)),MAX_ENVELOPE)
 def test_single_block_too_big_explicit(self):
  sizes={name:[16*1024**2] for _,name in FIELDS}
  with self.assertRaisesRegex(ValueError,'SINGLE_BLOCK_TOO_LARGE'):count_for_sizes(42161,0,sizes)
 def test_bad_scope(self):
  for chain,start,limit in [(4663,0,50),(42161,-1,50),(42161,0,MAX_ENVELOPE+1)]:
   with self.assertRaises(ValueError):count_for_sizes(chain,start,{name:[1] for _,name in FIELDS},limit)
 def test_mismatched_counts_or_invalid_sizes(self):
  sizes={name:[1] for _,name in FIELDS};sizes['bodies']=[1,2]
  with self.assertRaises(ValueError):count_for_sizes(42161,0,sizes)
  for bad in [-1,True,16*1024**2+1]:
   sizes={name:[bad] for _,name in FIELDS}
   with self.assertRaises(ValueError):count_for_sizes(42161,0,sizes)
 def test_selector_real_modern_retained_envelope(self):
  p=Path('/srv/economic-machine/arbitrum/evidence/freezer-modern-distant-focus-v1/window-500100000-500101023/candidate-records.json')
  if not p.is_file():self.skipTest('Optional retained provider page is not part of the public source snapshot')
  data=json.loads(p.read_bytes());sizes={name:[len(base64.b64decode(b[key],validate=True)) for b in data['blocks']] for key,name in FIELDS}
  count,n=count_for_sizes(data['chain'],data['blocks'][0]['number'],sizes)
  self.assertEqual(count,len(data['blocks']));self.assertEqual(n,p.stat().st_size)

class AdaptiveOrderTests(unittest.TestCase):
 def setUp(self):self.q=adaptive.OrderedResults('/x',100,108)
 def add(self,start,count):
  f=Future();self.q.admit(dict(start=start,count=count,folder=f'/x/window-{start}-{start+count-1}'),lambda _:f);return f
 def test_variable_page_exact_coverage(self):
  a=self.add(100,3);b=self.add(103,6);b.set_result({});self.assertIsNone(self.q.ready());a.set_result({})
  self.q.commit(dict(first=100,blocks=3));self.q.commit(dict(first=103,blocks=6))
  self.assertTrue(self.q.complete());self.assertTrue(self.q.admissions_complete())
 def test_gap_overlap_overshoot_rejected(self):
  for start,count in [(101,2),(99,2),(100,10),(100,0),(100,True)]:
   with self.assertRaises(ValueError):self.add(start,count)
 def test_unverified_or_miscount_no_advance(self):
  a=self.add(100,3)
  with self.assertRaises(ValueError):self.q.commit(dict(first=100,blocks=3))
  a.set_result({})
  with self.assertRaises(ValueError):self.q.commit(dict(first=100,blocks=2))
  self.assertEqual(self.q.next_commit,100)
 def test_later_failure_blocks_prefix(self):
  a=self.add(100,3);b=self.add(103,6);a.set_result({});b.set_exception(ValueError('fail'))
  with self.assertRaises(ValueError):self.q.ready()
 def test_four_pending_bound(self):
  for i in range(4):self.add(100+i,1)
  with self.assertRaises(ValueError):self.add(104,1)

class AdaptiveAmendmentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'amendment.json'
  self.value=dict(schema='economic-machine.freezer-adaptive-amendment.v1',original_policy_sha256='original',baseline_pipeline_sha256='baseline',
   adaptive_pipeline_sha256='adaptive',deadline_ms=1792351694298,monthly_budget_krw=1000000,source_writers=1,consumers=3,max_inflight=4,
   max_page_blocks=16384,max_native_input_bytes=60*1024**2,batch_blocks=65536,reset_cursor=False,preserve_existing_policies=True,first_batch=36705920,authorized_at_ms=1)
 def tearDown(self):self.tmp.cleanup()
 def write(self):
  self.path.write_text(json.dumps(self.value));return hashlib.sha256(self.path.read_bytes()).hexdigest()
 def test_correct_append_only_scope(self):
  sha=self.write();self.assertEqual(continuous.adaptive_amendment(self.path,sha,'original','baseline','adaptive'),self.value)
 def test_no_budget_deadline_reset_or_input_expansion(self):
  for key,value in [('deadline_ms',1792351694299),('monthly_budget_krw',1000001),('reset_cursor',True),('max_native_input_bytes',64*1024**2),
                    ('batch_blocks',262144),('consumers',4),('original_policy_sha256','changed'),('adaptive_pipeline_sha256','changed')]:
   original=self.value[key];self.value[key]=value;sha=self.write()
   with self.assertRaises(ValueError):continuous.adaptive_amendment(self.path,sha,'original','baseline','adaptive')
   self.value[key]=original
 def test_changed_file_hash_rejected(self):
  self.write()
  with self.assertRaises(ValueError):continuous.adaptive_amendment(self.path,'wrong','original','baseline','adaptive')


class AdaptiveProducerTests(unittest.TestCase):
 def setUp(self):
  import queue,threading
  from types import SimpleNamespace
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.out=self.root/'out';self.out.mkdir();self.source=self.root/'source';self.source.mkdir()
  self.q=queue.Queue();self.slots=threading.Semaphore(4);self.stop=threading.Event();owner=self
  class Store:
   def __init__(self,*a,**kw):self.cloud=SimpleNamespace(guard_local=lambda n=0:None)
   def close(self):pass
  class Snap:
   def __init__(self,*a,**kw):
    self.db=sqlite3.connect(owner.source/'source.sqlite3');self.identity={'pinned':'fixture'};self.calls=self.network_bytes=0;self.last_refs=[]
   def guard(self,n=0):pass
   def table(self,name,start,count):return [b'x']*count
   def drain(self):pass
   def close(self):self.db.close()
  self.policy=dict(chain=42161,start=100,end=104,source_cache=str(self.source),binary_sha256='fixed',page_mode='byte_bounded_v1',max_native_input_bytes=MAX_ENVELOPE)
  self.config=dict(out=str(self.out),base=str(self.root),policy=self.policy,policy_sha256='pinned-policy',next_block=100)
  def select(chain,start,sizes):
   limited={k:v[:2] for k,v in sizes.items()}
   return count_for_sizes(chain,start,limited)
  self.patches=[patch('economic_machine.arbitrum_research.freezer_adaptive.Storage',Store),
   patch('economic_machine.arbitrum_research.freezer_adaptive.PipelinedSnapshot',Snap),
   patch('economic_machine.arbitrum_research.freezer_adaptive.record_sizes',side_effect=lambda snap,name,start,count:[1]*count),
   patch('economic_machine.arbitrum_research.freezer_adaptive.count_for_sizes',side_effect=select)]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def run_produce(self):
  from economic_machine.arbitrum_research.freezer_adaptive import produce
  produce(self.config,self.q,self.slots,self.stop)
  rows=[]
  while not self.q.empty():rows.append(self.q.get_nowait())
  return rows
 def test_variable_prepared_prefix_complete(self):
  events=self.run_produce()
  self.assertEqual([(r['start'],r['count']) for r in events[:-1]],[(100,2),(102,2),(104,1)])
  self.assertEqual(events[-1],{'done':True})
  db=sqlite3.connect(self.source/'source.sqlite3')
  self.assertEqual(db.execute('select count(*) from prepared_pages').fetchone()[0],3);db.close()
  for e in events[:-1]:
   raw=(Path(e['folder'])/'candidate-records.json').read_bytes();ready=json.loads((Path(e['folder'])/'acquired.json').read_bytes())
   self.assertEqual(len(raw),ready['adaptive_selection']['predicted_bytes']);self.assertEqual(ready['raw_sha256'],hashlib.sha256(raw).hexdigest())
 def test_resume_reuses_committed_count_and_no_new_acquisition(self):
  self.run_produce()
  before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.out.glob('window-*/candidate-records.json')}
  self.config['next_block']=102
  import threading;self.slots=threading.Semaphore(4)
  def read(folder,*a,**kw):return (folder/'candidate-records.json').read_bytes(),json.loads((folder/'acquired.json').read_bytes())
  with patch('economic_machine.arbitrum_research.freezer_adaptive.record_sizes',side_effect=AssertionError('no reacquisition')),patch('economic_machine.arbitrum_research.freezer_adaptive.read_prepared',side_effect=read):
   events=self.run_produce()
  self.assertEqual([(r['start'],r['count']) for r in events[:-1]],[(102,2),(104,1)])
  self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
 def test_partial_changed_file_never_admitted(self):
  folder=self.out/'window-100-101';folder.mkdir();(folder/'candidate-records.json').write_bytes(b'changed')
  events=self.run_produce()
  self.assertEqual(len(events),1);self.assertIn('error_type',events[0])
  db=sqlite3.connect(self.source/'source.sqlite3');self.assertEqual(db.execute('select count(*) from prepared_pages').fetchone()[0],0);db.close()
  self.assertEqual((folder/'candidate-records.json').read_bytes(),b'changed')

if __name__=='__main__':unittest.main()
