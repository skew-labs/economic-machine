import hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'products/arbitrum_research'))
from freezer_continuous import execution_amendment,execution_required,guard_host_admission,DEADLINE

class ExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name);self.path=self.base/'execution.json'
  self.flags={'EM_FREEZER_ENVELOPE_OVERLAP':'1','EM_FREEZER_DECODED_MEMORY':'1','EM_FREEZER_HOST_ADMISSION':'1',
   'EM_FREEZER_GRAPH_MEMORY_BUILD':'1','EM_FREEZER_GRAPH_CACHE_MIB':'512'}
  self.value=dict(schema='economic-machine.freezer-execution-amendment.v1',original_policy_sha256='a'*64,
   deadline_ms=DEADLINE,monthly_budget_krw=1000000,minimum_host_free_bytes=128*1024**3,batch_headroom_bytes=8*1024**3,
   source_writers=1,consumers=3,max_inflight=4,max_native_input_bytes=60*1024**2,reset_cursor=False,
   preserve_existing_policies=True,first_batch=37099136,authorized_at_ms=1,flags=self.flags,files={})
  for name in ('products/arbitrum_research/freezer_continuous.py','products/arbitrum_research/freezer_probe.py',
   'src/economic_machine/arbitrum_research/freezer_envelope_overlap.py','src/economic_machine/arbitrum_research/freezer_decoded_memory.py',
   'src/economic_machine/arbitrum_research/freezer_host_admission.py','src/economic_machine/arbitrum_research/freezer_cloud_index.py',
   'src/economic_machine/arbitrum_research/freezer_graph_options.py','src/economic_machine/arbitrum_research/graph.py'):
   p=self.base/'source'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(name.encode())
   self.value['files'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
 def check(self):
  self.path.write_text(json.dumps(self.value));digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
  with patch.dict(os.environ,self.flags):return execution_amendment(self.path,digest,'a'*64,self.base)
 def test_pinned_original_scope_and_files(self):self.assertEqual(self.check(),self.value)
 def test_scope_cannot_expand(self):
  for key,value in [('deadline_ms',DEADLINE+1),('monthly_budget_krw',2000000),('minimum_host_free_bytes',1),
   ('batch_headroom_bytes',1),('source_writers',2),('consumers',4),('max_inflight',5),('max_native_input_bytes',64*1024**2),
   ('reset_cursor',True),('preserve_existing_policies',False)]:
   old=self.value[key];self.value[key]=value
   with self.subTest(key=key),self.assertRaisesRegex(ValueError,'EXECUTION_SCOPE'):self.check()
   self.value[key]=old
 def test_future_authorization_rejected(self):
  self.value['authorized_at_ms']=2**63
  with self.assertRaisesRegex(ValueError,'EXECUTION_TIME'):self.check()
 def test_missing_or_extra_implementation_file_rejected(self):
  key=next(iter(self.value['files']));old=self.value['files'].pop(key)
  with self.assertRaisesRegex(ValueError,'EXECUTION_FILES'):self.check()
  self.value['files'][key]=old;self.value['files']['../elsewhere']='a'*64
  with self.assertRaisesRegex(ValueError,'EXECUTION_FILES'):self.check()
 def test_changed_source_rejected(self):
  p=self.base/'source'/next(iter(self.value['files']));p.write_bytes(b'changed')
  with self.assertRaisesRegex(ValueError,'IMPLEMENTATION_CHANGED'):self.check()
 def test_changed_environment_rejected(self):
  self.path.write_text(json.dumps(self.value));digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
  with patch.dict(os.environ,{**self.flags,'EM_FREEZER_DECODED_MEMORY':'0'}),self.assertRaisesRegex(ValueError,'EXECUTION_FLAGS'):
   execution_amendment(self.path,digest,'a'*64,self.base)
 def test_wrong_amendment_hash_rejected(self):
  self.path.write_text(json.dumps(self.value))
  with self.assertRaisesRegex(ValueError,'EXECUTION_HASH'):execution_amendment(self.path,'b'*64,'a'*64,self.base)
 def test_symlink_source_rejected(self):
  p=self.base/'source'/next(iter(self.value['files']));raw=p.read_bytes();p.unlink();target=self.base/'target';target.write_bytes(raw);p.symlink_to(target)
  with self.assertRaisesRegex(ValueError,'IMPLEMENTATION_CHANGED'):self.check()
 def test_off_guard_does_not_observe_or_write(self):
  with patch('economic_machine.arbitrum_research.freezer_host_admission.check',side_effect=AssertionError):
   self.assertIsNone(guard_host_admission(self.base,'0'))
 def test_guard_returns_deny_without_changing_original_limits(self):
  result={'allow_new_work':False,'state':'BACKOFF','reasons':['sustained_io_pressure']}
  with patch('economic_machine.arbitrum_research.freezer_host_admission.check',return_value=result) as check:
   self.assertEqual(guard_host_admission(self.base,'1'),result)
   self.assertEqual(check.call_args.args[0],self.base)
   self.assertEqual(check.call_args.args[1].name,'batch-admission-state.json')
 def test_invalid_guard_flag_fails_closed(self):
  with self.assertRaisesRegex(ValueError,'ADMISSION_SETTING'):guard_host_admission(self.base,'yes')
 def test_decoder_route_requires_implementation_pin(self):
  relative='src/economic_machine/arbitrum_research/freezer_decoder.py'
  p=self.base/'source'/relative;p.write_bytes(b'route helper')
  route={'path':str(self.base/'route.json'),'sha256':'b'*64}
  self.value.update(decoder_routing='typed_v1',decoder_amendment=route)
  with patch.dict(os.environ,{'EM_FREEZER_DECODER_AMENDMENT':route['path'],'EM_FREEZER_DECODER_AMENDMENT_SHA':route['sha256']}):
   with self.assertRaisesRegex(ValueError,'EXECUTION_FILES'):self.check()
   self.value['files'][relative]=hashlib.sha256(p.read_bytes()).hexdigest();self.check()
 def test_unpinned_decoder_environment_rejected(self):
  with patch.dict(os.environ,{'EM_FREEZER_DECODER_AMENDMENT':'anything','EM_FREEZER_DECODER_AMENDMENT_SHA':'a'*64}):
   with self.assertRaisesRegex(ValueError,'DECODER_REQUIRED'):self.check()
 def test_decoder_only_still_requires_execution_amendment(self):
  for name in ('EM_FREEZER_DECODER_AMENDMENT','EM_FREEZER_DECODER_AMENDMENT_SHA'):
   with self.subTest(name=name),patch.dict(os.environ,{name:'configured'},clear=True):
    self.assertTrue(execution_required())
 def test_legacy_all_flags_off_does_not_require_amendment(self):
  with patch.dict(os.environ,{},clear=True):self.assertFalse(execution_required())
 def support(self):
  relative='src/economic_machine/arbitrum_research/freezer_ram_cache_v1.py'
  p=self.base/'source'/relative;p.write_bytes(b'pinned RAM cache helper')
  self.value['support_files']={relative:hashlib.sha256(p.read_bytes()).hexdigest()}
  return p
 def test_optional_support_file_pin_accepts_exact_helper(self):
  self.support();self.assertEqual(self.check(),self.value)
 def test_changed_support_file_rejected(self):
  p=self.support();p.write_bytes(b'changed')
  with self.assertRaisesRegex(ValueError,'SUPPORT_CHANGED'):self.check()
 def test_missing_support_file_rejected(self):
  self.support().unlink()
  with self.assertRaisesRegex(ValueError,'SUPPORT_CHANGED'):self.check()
 def test_symlink_support_file_rejected(self):
  p=self.support();raw=p.read_bytes();p.unlink();target=self.base/'helper';target.write_bytes(raw);p.symlink_to(target)
  with self.assertRaisesRegex(ValueError,'SUPPORT_CHANGED'):self.check()
 def test_support_scope_cannot_add_or_redirect_paths(self):
  for value in (None,{},[],{'../elsewhere':'a'*64},{'src/economic_machine/arbitrum_research/freezer_ram_cache_v1.py':'a'*64,'extra':'b'*64}):
   self.value['support_files']=value
   with self.subTest(value=value),self.assertRaisesRegex(ValueError,'SUPPORT_FILES'):self.check()
 def test_support_digest_must_be_canonical_sha256(self):
  self.support();relative=next(iter(self.value['support_files']))
  for digest in (None,True,'short','A'*64,'z'*64):
   self.value['support_files'][relative]=digest
   with self.subTest(digest=digest),self.assertRaisesRegex(ValueError,'SUPPORT_HASH'):self.check()

if __name__=='__main__':unittest.main()
