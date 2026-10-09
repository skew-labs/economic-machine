import pathlib,tempfile,unittest
from batch_journal import BatchJournal,decode
MARKET='0x'+'11'*20;CODE='0x'+'22'*32;SENDER='0x'+'77'*20;BLOCK='0x'+'44'*32
def payload(members):
 records=b''.join(a.to_bytes(4,'big')+(n|(10000<<64)|(1<<200)).to_bytes(32,'big') for a,n in members)
 return bytes.fromhex('30554c7e')+(32).to_bytes(32,'big')+len(records).to_bytes(32,'big')+records+bytes((-len(records))%32)
class BatchChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(pathlib.Path(self.tmp.name)/'batch.sqlite')
  self.j=BatchJournal(self.path,421614,MARKET,CODE,SENDER,1000,0)
  for a in (1,2,3):self.j.register(a,0)
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def prepare(self,members=((1,1),(2,1)),fee=100):return self.j.prepare(payload(members),fee)
 def include(self,i,success=True):
  r=self.j.get(i);tx='0x'+(r['sender_nonce']+1).to_bytes(32,'big').hex()
  self.j.signed(i,tx,b'signed test fixture');self.j.broadcast(i)
  members=r['members'];observed={a:n if success else n-1 for a,n,_ in members}
  commands={a:int(c) for a,_,c in members} if success else {}
  self.j.included(i,tx,BLOCK,10,BLOCK,observed,60,success,commands)
 def test_all_members_commit_together_and_pipeline_provisionally(self):
  i=self.prepare();self.include(i);self.prepare(((1,2),(2,2)))
  self.assertEqual(self.j.canonical(i,BLOCK,9),'included');self.assertEqual(self.j.canonical(i,BLOCK,10),'finalized')
 def test_duplicate_malformed_and_padding_rejected(self):
  for p in (payload([(1,1),(1,1)]),payload([(1,1)])[:-1],payload([(1,1)])[:-1]+b'\x01'):
   with self.assertRaises(ValueError):self.j.prepare(p,100)
  with self.assertRaises(ValueError):self.j.prepare(payload([(1,1)])[:4]+bytes(32)+payload([(1,1)])[36:],100)
 def test_partial_receipt_never_advances_any_member(self):
  i=self.prepare();self.j.signed(i,'0x'+'55'*32,b'test');self.j.broadcast(i)
  r=self.j.get(i)
  with self.assertRaises(ValueError):self.j.included(i,r['txhash'],BLOCK,10,BLOCK,{1:1,2:0},60,True,{a:int(c) for a,_,c in r['members']})
  self.assertEqual(self.j.db.execute('select nonce from accounts where account=1').fetchone()[0],0)
  self.assertEqual(self.j.get(i)['state'],'broadcast')
 def test_missing_quote_log_and_wrong_hash_reject(self):
  i=self.prepare();tx='0x'+'55'*32;self.j.signed(i,tx,b'test');self.j.broadcast(i)
  with self.assertRaises(ValueError):self.j.included(i,tx,BLOCK,10,BLOCK,{1:1,2:1},60,True,{1:dict((a,int(c)) for a,_,c in self.j.get(i)['members'])[1]})
 def test_restart_retains_signed_unknown_bytes_and_fee(self):
  i=self.prepare(fee=900);self.j.signed(i,'0x'+'55'*32,b'persisted raw');self.j.broadcast(i);self.j.close()
  self.j=BatchJournal(self.path,421614,MARKET,CODE,SENDER,1000,0)
  self.assertEqual(self.j.get(i)['raw'],b'persisted raw');self.assertEqual(self.j.used(),900)
  with self.assertRaises(ValueError):self.prepare(((3,1),))
 def test_reorg_halts_sender_domain_including_unrelated_accounts(self):
  i=self.prepare();self.include(i);other=self.prepare(((3,1),))
  self.assertEqual(self.j.canonical(i,'0x'+'66'*32,10),'halted_reorg')
  self.assertEqual(self.j.get(other)['state'],'reorg')
  self.assertEqual(self.j.db.execute('select sum(halted) from accounts').fetchone()[0],3)
 def test_recovery_requires_all_account_fences_and_sender_fence_finalized(self):
  i=self.prepare();self.include(i);self.j.canonical(i,'0x'+'66'*32,10)
  for observed,height,finalized,sender in [({1:1},11,11,1),({1:1,2:1},11,10,1),({1:0,2:1},11,11,1),({1:1,2:1},11,11,0)]:
   with self.assertRaises(ValueError):self.j.recover_fenced(observed,BLOCK,height,BLOCK,finalized,sender)
  self.j.recover_fenced({1:2,2:2},BLOCK,11,BLOCK,11,1)
  self.assertEqual(self.j.get(i)['state'],'fenced');self.assertEqual(self.j.used(),100)
  self.prepare(((1,3),(2,3)))
 def test_failed_atomic_batch_needs_finality_before_retry(self):
  i=self.prepare();self.include(i,False)
  with self.assertRaises(ValueError):self.prepare()
  self.assertEqual(self.j.canonical(i,BLOCK,10),'reverted');self.prepare()
 def test_global_fee_budget_and_window(self):
  i=self.prepare(fee=900);self.include(i)
  with self.assertRaises(ValueError):self.prepare(((1,2),(2,2)),101)
  self.j.canonical(i,BLOCK,10);self.prepare(((1,2),(2,2)),940)
 def test_provisional_window_is_explicit_and_terminal_history_not_active(self):
  self.j.close();self.path=str(pathlib.Path(self.tmp.name)/'bounded.sqlite')
  self.j=BatchJournal(self.path,421614,MARKET,CODE,SENDER,100000,0,window=2)
  for a in (1,2,3):self.j.register(a,0)
  first=self.prepare();self.include(first);second=self.prepare(((1,2),(2,2)));self.include(second)
  with self.assertRaises(ValueError):self.prepare(((1,3),(2,3)))
  self.j.finalize_prefix(10)
  self.assertEqual(self.j.used(),120);self.assertEqual(self.j.db.execute('select count(*) from active_members').fetchone()[0],0)
  self.prepare(((1,3),(2,3)))
 def test_failed_inclusion_is_never_reported_as_success(self):
  identity=self.prepare();self.include(identity,False)
  self.assertEqual(self.j.canonical(identity,BLOCK,9),'included_revert')
  self.assertEqual(self.j.canonical(identity,BLOCK,10),'reverted')
 def test_unsigned_abort_can_retry_same_command_without_fee_or_nonce_leak(self):
  i=self.prepare();self.j.abort_unsigned(i);other=self.prepare()
  self.assertNotEqual(i,other);self.assertEqual(self.j.get(other)['sender_nonce'],0);self.assertEqual(self.j.used(),100)
 def test_signer_timeout_cannot_be_misreported_as_unsigned(self):
  i=self.prepare();self.j.mark_signing(i,{'nonce':0})
  with self.assertRaises(ValueError):self.j.abort_unsigned(i)
  self.j.close();self.j=BatchJournal(self.path,421614,MARKET,CODE,SENDER,1000,0)
  self.assertEqual(self.j.get(i)['state'],'signing')
  with self.assertRaises(ValueError):self.prepare(((3,1),))
 def test_database_identity_and_hex_validation(self):
  self.j.close()
  with self.assertRaises(ValueError):BatchJournal(self.path,42161,MARKET,CODE,SENDER,1000,0)
  self.j=BatchJournal(self.path,421614,MARKET,CODE,SENDER,1000,0)
  with self.assertRaises(ValueError):self.j.signed(self.prepare(),'0x'+'zz'*32,b'raw')
if __name__=='__main__':unittest.main(verbosity=2)
