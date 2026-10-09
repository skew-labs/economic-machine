import pathlib,tempfile,unittest
from journal import Journal
MARKET='0x'+'11'*20;CODE='0x'+'22'*32;TX='0x'+'33'*32;BLOCK='0x'+'44'*32
class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(pathlib.Path(self.tmp.name)/'journal.sqlite');self.j=Journal(self.path,421614,MARKET,CODE,1000);self.j.register(7,0,421614,MARKET,CODE)
 def tearDown(self):self.j.close();self.tmp.cleanup()
 def payload(self,n,expiry=10000):return bytes.fromhex('ac8e6b9e')+(7).to_bytes(32,'big')+(n|(expiry<<64)).to_bytes(32,'big')
 def prepare(self,n=1,fee=100):return self.j.prepare(7,n,self.payload(n),fee)
 def include(self,identity,success=True):self.j.broadcast(identity,TX);self.j.included(identity,TX,BLOCK,10,BLOCK,1 if success else 0,60,success)
 def test_restart_does_not_rebroadcast_unknown(self):
  identity=self.prepare();self.j.broadcast(identity,TX);self.j.close();self.j=Journal(self.path,421614,MARKET,CODE,1000)
  with self.assertRaises(ValueError):self.prepare()
  self.j.broadcast(identity,TX)
 def test_nonce_advances_on_canonical_inclusion_not_finality(self):
  identity=self.prepare();self.include(identity);self.prepare(2)
  self.assertEqual(self.j.check_canonical(identity,BLOCK,9),'included');self.assertEqual(self.j.check_canonical(identity,BLOCK,10),'finalized')
 def test_reorg_halts_all_descendants(self):
  identity=self.prepare();self.include(identity);self.prepare(2)
  self.assertEqual(self.j.check_canonical(identity,'0x'+'55'*32,10),'halted_reorg')
  with self.assertRaises(ValueError):self.prepare(2)
  self.assertEqual(self.j.db.execute("select count(*) from intents where state='reorg'").fetchone()[0],2)
 def test_budget_reserves_until_finalized(self):
  identity=self.prepare(fee=900);self.include(identity)
  with self.assertRaises(ValueError):self.prepare(2,101)
  self.j.check_canonical(identity,BLOCK,10);self.prepare(2,940)
 def test_receipt_and_state_disagreement(self):
  identity=self.prepare();self.j.broadcast(identity,TX)
  with self.assertRaises(ValueError):self.j.included(identity,TX,BLOCK,10,BLOCK,2,60,True)
  with self.assertRaises(ValueError):self.j.included(identity,TX,BLOCK,10,'0x'+'66'*32,1,60,True)
 def test_failed_receipt_needs_finality_before_retry(self):
  identity=self.prepare();self.include(identity,False)
  with self.assertRaises(ValueError):self.prepare()
  self.assertEqual(self.j.check_canonical(identity,BLOCK,10),'reverted');self.j.prepare(7,1,self.payload(1,10001),100)
 def test_wrong_chain_target_payload_and_budget(self):
  with self.assertRaises(ValueError):self.j.register(8,0,42161,MARKET,CODE)
  with self.assertRaises(ValueError):self.j.prepare(7,1,self.payload(2),100)
  with self.assertRaises(ValueError):self.prepare(fee=1001)
  self.j.close()
  with self.assertRaises(ValueError):Journal(self.path,421614,MARKET,CODE,1001)
  self.j=Journal(self.path,421614,MARKET,CODE,1000)
 def test_concurrent_preparation_only_one_wins(self):
  other=Journal(self.path,421614,MARKET,CODE,1000);self.prepare()
  try:
   with self.assertRaises(ValueError):other.prepare(7,1,self.payload(1),100)
  finally:other.close()
if __name__=='__main__':unittest.main(verbosity=2)
