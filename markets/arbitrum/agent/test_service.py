import json,pathlib,tempfile,time,unittest
from unittest.mock import patch
from batch_journal import BatchJournal
from service import bind,bootstrap,private_json,Status,mandate,main
from test_batch_journal import payload,MARKET,CODE,SENDER

class ServiceChecks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=pathlib.Path(self.tmp.name)
  self.j=BatchJournal(str(self.path/'j.sqlite'),421614,MARKET,CODE,SENDER,10**12,0);self.j.register(1,0)
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def test_restart_mandate_cannot_extend_budget_deadline_or_profile(self):
  config={'deadline':100,'budget':100,'profiles':[1]};bind(self.j,config);bind(self.j,config)
  for c in ({**config,'deadline':200},{**config,'budget':200},{**config,'profiles':[2]}):
   with self.assertRaises(ValueError):bind(self.j,c)
 def test_unknown_signer_restart_halts_and_preserves_fee(self):
  identity=self.j.prepare(payload([(1,1)]),100);self.j.mark_signing(identity,{'nonce':0})
  class Transport:
   journal=self.j
   def audit(self):return 'clear'
  self.assertEqual(bootstrap(Transport()),'halted_unknown_signer');self.assertEqual(self.j.get(identity)['state'],'reorg');self.assertEqual(self.j.used(),100)
 def test_unsigned_crash_can_abort_without_nonce_consumption(self):
  identity=self.j.prepare(payload([(1,1)]),100)
  class Transport:
   journal=self.j
   def audit(self):return 'clear'
  self.assertEqual(bootstrap(Transport()),'ready');self.assertEqual(self.j.get(identity)['state'],'aborted');self.assertEqual(self.j.used(),0)
 def test_private_config_permissions_and_atomic_status(self):
  p=self.path/'rpc.json';p.write_text('{"url":"https://example.com/"}');p.chmod(0o600)
  self.assertEqual(set(private_json(p)),{'url'});p.chmod(0o644)
  with self.assertRaises(ValueError):private_json(p)
  status=Status(self.path/'status.json');status.write('reconciling',reserved='100')
  self.assertEqual(json.loads((self.path/'status.json').read_text())['state'],'reconciling');self.assertFalse((self.path/'status.json.tmp').exists())
 def config(self):
  return {'chain':421614,'market':MARKET,'codehash':CODE,'sender':SENDER,'budget_wei':'1000','fee_cap_wei':'100','initial_sender_nonce':0,'window':8192,'deadline_unix':2000000000,'poll_ms':100,'max_gas':16000000,'priority_fee_wei':'0','library':'/x/lib.so','library_sha256':'ab'*32,'journal':'/x/j.sqlite','status':'/x/status.json','signer_socket':'/x/signer.sock','profiles':[{'account':1,'version':1,'max_position':100,'clip':2,'spread':3,'inventory_weight':1,'ttl_seconds':30,'session_epoch':1}]}
 def test_profile_requires_delegation_and_rejects_expanded_shape(self):
  config=self.config()
  self.assertEqual(mandate(config)['window'],8192)
  with self.assertRaises(ValueError):mandate({**config,'unlimited':True})
  config['profiles'][0]['session_epoch']=0
  with self.assertRaises(ValueError):mandate(config)
 def test_expired_mandate_exits_without_rpc_library_or_signer(self):
  config=self.config();config.update(deadline_unix=int(time.time())-1,status=str(self.path/'status.json'))
  p=self.path/'mandate.json';p.write_text(json.dumps(config));p.chmod(0o600)
  with patch('sys.argv',['run-agent','--mandate',str(p),'--rpc-file','/nonexistent','--run']),patch('builtins.print'):
   self.assertEqual(main(),0)
  self.assertEqual(json.loads((self.path/'status.json').read_text())['signing_requests'],0)
if __name__=='__main__':unittest.main(verbosity=2)
