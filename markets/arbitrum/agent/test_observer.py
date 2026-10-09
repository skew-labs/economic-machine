import time,unittest
from eth_utils import keccak
from observer import Observer,calldata
from coordinator import Profile
MARKET='0x'+'11'*20;ORACLE='0x'+'22'*20;SENDER='0x'+'33'*20;HASH='0x'+'44'*32
def encode(*values):return '0x'+b''.join((v%(1<<256)).to_bytes(32,'big') for v in values).hex()
class Fake:
 def __init__(self):self.age=0;self.reorg=False;self.epoch=1;self.sizes=[]
 def __call__(self,method,params):
  if method=='eth_getBlockByNumber':return {'number':'0x10','hash':('0x'+'55'*32) if self.reorg and params[0]!='latest' else HASH,'timestamp':hex(int(time.time())-self.age)}
  if method=='eth_chainId':return hex(421614)
  if method=='eth_getCode':return '0x00'
  data=params[0]['data'];prefix=data[:10]
  if prefix==calldata('oracle()'):return encode(int(ORACLE,16))
  if prefix==calldata('read()'):return encode(1000,1001)
  if prefix==calldata('best(uint8)'):return encode(999 if int(data[10:],16)==0 else 1001)
  if prefix==calldata('accounts(uint32)'):return encode(10**12,-1,7,13)
  if prefix==calldata('orders(uint32)'):return encode(0,0,2,999 if int(data[10:],16)%2==0 else 1001,int(time.time())+30,1)
  if prefix==calldata('sessions(uint32)'):return encode(int(SENDER,16),int(time.time())+1000,self.epoch,100,10**12,1)
  raise ValueError('unexpected view')
 def batch(self,requests):self.sizes.append(len(requests));return [self(m,p) for m,p in requests]
class ObserverChecks(unittest.TestCase):
 def observer(self,rpc,count=1):
  profiles=[Profile(421614,i+1,1,time.monotonic_ns()+10**12,7,100,2,3,1,30,1) for i in range(count)]
  return Observer(rpc,421614,MARKET,'0x'+keccak(b'\0').hex(),SENDER,profiles)
 def test_signed_inventory_and_session_read_at_one_anchor(self):
  rpc=Fake();o=self.observer(rpc);frames,anchor=o.read()
  self.assertEqual(frames[1].inventory,-1);self.assertEqual(frames[1].account_nonce,7)
  self.assertEqual(frames[1].session_epoch,1);self.assertEqual(anchor['hash'],HASH)
 def test_32_profiles_respect_rpc_batch_capacity(self):
  rpc=Fake();o=self.observer(rpc,32);frames,_=o.read()
  self.assertEqual(len(frames),32);self.assertLessEqual(max(rpc.sizes),128)
 def test_stalled_head_reorg_and_revocation_reject(self):
  for attr,value in [('age',10),('reorg',True),('epoch',2)]:
   rpc=Fake();o=self.observer(rpc);setattr(rpc,attr,value)
   with self.assertRaises(ValueError):o.read()
if __name__=='__main__':unittest.main(verbosity=2)
