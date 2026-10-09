import pathlib,tempfile,unittest
from unittest.mock import patch
from eth_account import Account
from batch_journal import BatchJournal
from transport import Transport,verify_signed,RPC,RPCFault,BatchRejected
from eth_utils import keccak
from test_batch_journal import payload,MARKET,CODE
class SignChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.owner=Account.create()
  self.j=BatchJournal(str(pathlib.Path(self.tmp.name)/'journal.sqlite'),421614,MARKET,CODE,self.owner.address,10**12,0)
  self.j.register(1,0);self.i=self.j.prepare(payload([(1,1)]),10**9);self.r=self.j.get(self.i)
  self.tx={'type':2,'chainId':421614,'nonce':0,'to':MARKET,'value':0,'data':self.r['payload'],'gas':200000,'maxFeePerGas':10,'maxPriorityFeePerGas':1,'accessList':[]}
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def raw(self,**changes):return bytes(Account.sign_transaction({**self.tx,**changes},self.owner.key).raw_transaction)
 def test_valid_signature_binds_quote_domain_and_fee(self):
  self.assertEqual(len(verify_signed(self.raw(),self.r,self.j)),66)
 def test_chain_recipient_value_nonce_payload_and_access_list_reject(self):
  changes=[{'chainId':42161},{'to':'0x'+'33'*20},{'value':1},{'nonce':1},{'data':payload([(1,2)])},{'accessList':[{'address':MARKET,'storageKeys':[]}]}]
  for c in changes:
   with self.assertRaises(ValueError):verify_signed(self.raw(**c),self.r,self.j)
 def test_fee_gas_and_wrong_signer_reject(self):
  for c in [{'maxFeePerGas':10000},{'gas':16000001}]:
   with self.assertRaises(ValueError):verify_signed(self.raw(**c),self.r,self.j)
  other=Account.create()
  with self.assertRaises(ValueError):verify_signed(bytes(Account.sign_transaction(self.tx,other.key).raw_transaction),self.r,self.j)
 def test_request_rejected_before_signer_if_scope_changes(self):
  class RejectSigner:
   def sign(self,request):raise AssertionError('must not reach signer')
  transport=Transport(None,self.j,RejectSigner())
  with self.assertRaises(ValueError):transport.sign(self.i,{**self.tx,'data':'0x00'})
  self.assertEqual(self.j.get(self.i)['state'],'prepared')
 def test_signer_timeout_stays_durable_signing(self):
  class Timeout:
   def sign(self,request):raise TimeoutError('test timeout')
  transport=Transport(None,self.j,Timeout())
  request={**self.tx,'data':'0x'+self.r['payload'].hex()}
  with self.assertRaises(TimeoutError):transport.sign(self.i,request)
  self.assertEqual(self.j.get(self.i)['state'],'signing')
  with self.assertRaises(ValueError):self.j.abort_unsigned(self.i)
 def test_public_http_and_credential_userinfo_reject(self):
  for url in ['http://example.com/','https://user:password@example.com/','file:///tmp/rpc']:
   with self.assertRaises(ValueError):RPC(url)
class PreflightChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.j=BatchJournal(str(pathlib.Path(self.tmp.name)/'j.sqlite'),421614,MARKET,'0x'+keccak(b'\0').hex(),'0x'+'44'*20,10**12,0);self.j.register(1,0)
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def rpc(self):
  class Fake:
   calls=0;batch_calls=0;requests=[];reject=False;reorg=False
   def __call__(self,method,params):
    self.calls+=1
    return {'number':'0x1','hash':'0x'+('55' if self.reorg and params[0]!='latest' else '44')*32,'baseFeePerGas':'0xa'}
   def batch(self,requests,allow_errors=False):
    self.batch_calls+=1;self.requests=requests
    return [hex(421614),'0x00',RPCFault(3) if self.reject else '0x',RPCFault(3) if self.reject else hex(200000),'0x0']
  return Fake()
 def test_identity_admission_estimate_and_nonce_use_three_roundtrips(self):
  rpc=self.rpc();identity,tx=Transport(rpc,self.j,None).prepare(payload([(1,1)]),10**9)
  self.assertEqual((rpc.calls,rpc.batch_calls),(2,1));self.assertEqual(tx['gas'],220000)
  self.assertEqual(rpc.requests[2][1][1],'0x1');self.assertEqual(rpc.requests[3][1][1],'0x1')
  self.assertEqual(self.j.get(identity)['state'],'prepared')
 def test_rejected_call_still_triggers_member_admission_if_estimate_rejects(self):
  rpc=self.rpc();rpc.reject=True
  with self.assertRaises(BatchRejected):Transport(rpc,self.j,None).prepare(payload([(1,1)]),10**9)
  self.assertEqual(self.j.used(),0)
 def test_reorg_before_prepare_does_not_reserve_a_nonce_or_fee(self):
  rpc=self.rpc();rpc.reorg=True
  with self.assertRaises(ValueError):Transport(rpc,self.j,None).prepare(payload([(1,1)]),10**9)
  self.assertEqual(self.j.used(),0)
 def test_slow_rpc_preflight_is_rejected_before_journaling_or_signing(self):
  with patch('transport.time.monotonic_ns',side_effect=[0,1000000000]):
   with self.assertRaises(ValueError):Transport(self.rpc(),self.j,None).prepare(payload([(1,1)]),10**9)
  self.assertEqual(self.j.used(),0)
if __name__=='__main__':unittest.main(verbosity=2)
