import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from economic_machine.pump_research.store import canonical
from economic_machine.robinhood_research.source import normalize,parse_page,GENESIS,ENDPOINTS,flatten,RPC
from economic_machine.robinhood_research.bridge import decode,Bridge,SEND,EXECUTED,ARBSYS,ROUTES,validate_receipt
from economic_machine.arbitrum_research.graph import Graph
from economic_machine.arbitrum_research.muse import MuseFleet

H='0x'+'11'*32;T='0x'+'22'*32;A='0x'+'33'*20;B='0x'+'44'*20

def envelope(method,params,result):
 return dict(chain_id=4663,provider=ENDPOINTS[4663],request=dict(jsonrpc='2.0',id=1,method=method,params=params),response=dict(jsonrpc='2.0',id=1,result=result))
def fixture():
 tx=dict(hash=T,transactionIndex='0x0',blockHash=H,blockNumber='0x1',**{'from':A,'to':B,'value':'0x1'})
 receipt=dict(transactionHash=T,transactionIndex='0x0',blockHash=H,blockNumber='0x1',status='0x1',logs=[])
 block=dict(number='0x1',hash=H,parentHash=GENESIS,transactions=[tx],timestamp='0x1')
 return dict(block=envelope('eth_getBlockByNumber',['0x1',True],block),receipts=envelope('eth_getBlockReceipts',['0x1'],[receipt]))
def word(n):return n.to_bytes(32,'big')
def addr(a):return bytes.fromhex(a[2:]).rjust(32,b'\0')
def withdrawal(chain=4663,index=7):
 return dict(address=ARBSYS,topics=[SEND,'0x'+addr(B).hex(),'0x'+'00'*32,'0x'+word(index).hex()],
  data='0x'+(addr(A)+word(1)+word(200)+word(1000)+word(5)+word(192)+word(0)).hex(),
  blockHash=H,blockNumber='0x1',transactionHash=T,transactionIndex='0x0',logIndex='0x0',removed=False)
def executed(child=4663,index=7):
 return dict(address=ROUTES[child]['outbox'],topics=[EXECUTED,'0x'+addr(B).hex(),'0x'+addr(A).hex(),'0x'+word(0).hex()],data='0x'+word(index).hex())

class SourceTests(unittest.TestCase):
 def test_normal(self):self.assertEqual(normalize(fixture(),1)['transactions'][0]['status'],1)
 def test_missing_receipt(self):
  x=fixture();x['receipts']['response']['result']=[]
  with self.assertRaises(Exception):normalize(x,1)
 def test_wrong_chain(self):
  x=fixture();x['block']['chain_id']=42161
  with self.assertRaises(Exception):normalize(x,1)
 def test_wrong_hash(self):
  x=fixture();x['receipts']['response']['result'][0]['transactionHash']='0x'+'ab'*32
  with self.assertRaises(Exception):normalize(x,1)
 def test_wrong_block(self):
  x=fixture();x['receipts']['response']['result'][0]['blockHash']='0x'+'ab'*32
  with self.assertRaises(Exception):normalize(x,1)
 def test_unknown_status(self):
  x=fixture();x['receipts']['response']['result'][0]['status']='0x2'
  with self.assertRaises(Exception):normalize(x,1)
 def test_trace_missing_is_gap(self):self.assertEqual(normalize(fixture(),1)['trace_boundary'],'TRACE_UNAVAILABLE')
 def test_trace_binding(self):
  x=fixture();x['traces']=envelope('debug_traceBlockByNumber',['0x1',{'tracer':'callTracer'}],[dict(txHash='0x'+'ab'*32,result={})])
  with self.assertRaises(Exception):normalize(x,1)
 def test_failed_ancestor_preserved(self):
  rows=flatten(dict(type='CALL',error='revert',calls=[dict(type='CALL',**{'from':A,'to':B,'value':'0x5'})]),0,T)
  self.assertEqual(rows[0]['error'],'revert');self.assertEqual(rows[1]['traceAddress'],[0])
 def test_read_only(self):
  with self.assertRaises(ValueError):RPC().call('eth_sendRawTransaction',[])
 def test_graph_chain_isolation(self):
  with tempfile.TemporaryDirectory() as tmp:
   g=Graph(Path(tmp)/'g',chain_id=4663);g.close()
   with self.assertRaises(ValueError):Graph(Path(tmp)/'g',chain_id=42161)
 def test_page_replay(self):
  raw=(canonical(dict(schema='economic-machine.robinhood-rpc-page.v1',chain_id=4663,start=1,requested_end=16,
    identity=envelope('eth_chainId',[],'0x1237'),entries=[fixture()]))+'\n').encode()
  self.assertEqual(parse_page(raw,1,16)['sha256'],hashlib.sha256(raw).hexdigest())
  with self.assertRaises(Exception):parse_page(raw,0,16)

class BridgeTests(unittest.TestCase):
 def test_decode(self):
  x=decode(4663,withdrawal());self.assertEqual((x['message_id'],x['value_wei'],x['sender']),('7','5',A))
 def test_chain_separation(self):
  self.assertIsNone(decode(1,withdrawal()));self.assertIsNone(decode(4663,executed()))
 def test_untrusted_emitter(self):
  x=withdrawal();x['address']=A;self.assertIsNone(decode(4663,x))
 def test_removed(self):
  x=withdrawal();x['removed']=True
  with self.assertRaises(ValueError):decode(4663,x)
 def test_dynamic_bounds(self):
  x=withdrawal();x['data']=x['data'][:-64]
  with self.assertRaises(ValueError):decode(4663,x)
 def test_receipt_binding(self):
  log=withdrawal();r=dict(status='0x1',transactionHash=T,blockHash=H,blockNumber='0x1',transactionIndex='0x0',logs=[log]);h=dict(hash=H,number='0x1',transactions=[T])
  self.assertEqual(validate_receipt(4663,r,h,T,log)['message_id'],'7')
  r['status']='0x0'
  with self.assertRaises(ValueError):validate_receipt(4663,r,h,T,log)
 def test_two_leg_matching_no_same_wallet_heuristic(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge(tmp,None)
   def add(chain,log,tx):
    x=decode(chain,log);x.update(chain_id=chain,tx_hash=tx,log_index=0);b.insert(x,{'test':True})
   add(4663,withdrawal(),T);add(1,executed(42161),H)
   self.assertEqual(b.snapshot()['paired_canonical_messages'],0)
   add(1,executed(4663),'0x'+'55'*32)
   self.assertEqual(b.snapshot()['paired_canonical_messages'],1)
   b.close()
 def test_conflicting_sender_fails(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge(tmp,None);a=decode(4663,withdrawal());a.update(chain_id=4663,tx_hash=T,log_index=0);b.insert(a,{})
   z=decode(1,executed());z.update(chain_id=1,tx_hash=H,log_index=0,sender=B)
   with self.assertRaises(Exception):b.insert(z,{})
   self.assertEqual(b.snapshot()['paired_canonical_messages'],0);b.close()
 def test_restart_journal_preserved(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge(tmp,None);a=decode(4663,withdrawal());a.update(chain_id=4663,tx_hash=T,log_index=0);b.insert(a,{});b.close()
   b=Bridge(tmp,None);self.assertEqual(b.snapshot()['receipt_checked_legs'],1);b.close()


from economic_machine.robinhood_research import layerzero as lz
class LayerZeroTests(unittest.TestCase):
 def records(self):
  sender=addr(A);receiver=addr(B);g=lz.guid(9,30110,sender,30416,receiver)
  packet=b'\x01'+(9).to_bytes(8,'big')+(30110).to_bytes(4,'big')+sender+(30416).to_bytes(4,'big')+receiver+bytes.fromhex(g[2:])+b'payload'
  first=word(len(packet))+packet+b'\0'*((-len(packet))%32)
  data=word(96)+word(96+len(first))+addr(A)+first+word(0)
  sent=dict(address=lz.REGISTRY[42161][1],topics=[lz.SENT],data='0x'+data.hex())
  done=dict(address=lz.REGISTRY[4663][1],topics=[lz.DELIVERED],data='0x'+(word(30110)+sender+word(9)+receiver).hex())
  return sent,done
 def test_guid_matches_both_chains(self):
  a,b=self.records();x=lz.decode(42161,a);y=lz.decode(4663,b)
  self.assertEqual(x['message_id'],y['message_id']);self.assertEqual(x['destination_chain'],4663)
 def test_chain_id_is_not_eid(self):
  a,b=self.records();b['data']='0x'+(word(42161)+addr(A)+word(9)+addr(B)).hex()
  self.assertIsNone(lz.decode(4663,b))
 def test_wrong_emitter(self):
  a,_=self.records();a['address']=A;self.assertIsNone(lz.decode(42161,a))
 def test_corrupted_guid(self):
  a,_=self.records();raw=bytearray.fromhex(a['data'][2:]);raw[128+81]^=1;a['data']='0x'+raw.hex()
  with self.assertRaises(ValueError):lz.decode(42161,a)
 def test_direct_pair_persists(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Bridge(tmp,None)
   for chain,log,tx in zip((42161,4663),self.records(),(T,H)):
    item=decode(chain,log);item.update(chain_id=chain,tx_hash=tx,log_index=0);b.insert(item,{})
   self.assertEqual(b.snapshot()['direct_arbitrum_robinhood_message_pairs'],1);b.close()

from unittest.mock import patch
from test_arbitrum_muse import summary,answer,envelope as model_envelope
from test_pump_research_spend import setup_budget
from economic_machine.pump_research.research_spend import Spend
class RobinhoodMuseTests(unittest.TestCase):
 def test_ten_workers_shared_budget_namespace(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);shared=base/'shared';shared.mkdir();setup_budget(shared)
   f=MuseFleet(base/'rh',shared,shared/'private/openrouter.key',chain_id=4663)
   def respond(req,_):
    self.assertIn('Robinhood',req['messages'][0]['content'])
    x=json.loads(req['messages'][1]['content'])['evidence'];return model_envelope(answer(x))
   with patch('economic_machine.arbitrum_research.muse.provider_call',side_effect=respond):r=f.investigate([summary(i) for i in range(10)],max_workers=10)
   self.assertEqual(r['totals']['valid_responses'],10)
   spend=Spend(shared);rows=spend.db.execute("SELECT key FROM operations WHERE kind='model'").fetchall();spend.close()
   self.assertEqual(len(rows),10);self.assertTrue(all(x[0].startswith('rh:') for x in rows))
   with self.assertRaises(ValueError):f.investigate([summary(i) for i in range(11)],max_workers=10)
   with self.assertRaises(ValueError):MuseFleet(base/'rh',shared,shared/'private/openrouter.key',chain_id=42161)


class TraceRecoveryTests(unittest.TestCase):
 def test_genesis_empty_does_not_disable_next_block_traces(self):
  from economic_machine.robinhood_research.source import PublicSource
  class Fake:
   def check(self):return envelope('eth_chainId',[],'0x1237')
   def result(self,method,params):
    tag=params[0];n=int(tag,16)
    if method=='eth_getBlockByNumber':
     result=fixture()['block']['response']['result'] if n else dict(number='0x0',hash=GENESIS,parentHash='0x'+'00'*32,transactions=[])
     if params[1] is False:result=dict(result,transactions=[T] if n else [])
    else:result=fixture()['receipts']['response']['result'] if n else []
    return result,envelope(method,params,result)
   def call(self,method,params):
    if params[0]=='0x0':raise AssertionError('must not probe empty genesis')
    return envelope(method,params,[dict(txHash=T,result=dict(type='CALL',value='0x1',**{'from':A,'to':B}))])
  source=PublicSource();source.rpc=Fake();page=source.fetch(0,1)
  self.assertEqual(len(page['blocks'][1]['traces']),1)
  self.assertEqual(source.trace_retry_at,0)


class PrefixRecoveryTests(unittest.TestCase):
 def test_completed_prefix_survives_retry_after(self):
  from economic_machine.robinhood_research.source import PublicSource,SourceBackoff
  class Fake:
   def check(self):return envelope('eth_chainId',[],'0x1237')
   def result(self,method,params):
    if params[0]=='0x1':raise SourceBackoff(999,429)
    value=(dict(number='0x0',hash=GENESIS,parentHash='0x'+'00'*32,transactions=[]) if method=='eth_getBlockByNumber' else [])
    return value,envelope(method,params,value)
  source=PublicSource();source.rpc=Fake();page=source.fetch(0,15)
  self.assertEqual(page['last_block'],0);self.assertEqual(page['retry_after_seconds'],999)
  self.assertEqual(parse_page(page['raw'],0,15)['last_block'],0)

if __name__=='__main__':unittest.main()
