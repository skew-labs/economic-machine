import copy
import unittest
from unittest.mock import patch
from economic_machine.arbitrum_research.acceleration import accelerate
from economic_machine.robinhood_research.source import RPC,PublicSource,SourceIntegrityError,result
from test_robinhood_research import fixture,envelope,GENESIS,T,A,B
from economic_machine.robinhood_research.bridge import Bridge
import tempfile

class AdmissionTests(unittest.TestCase):
 def test_idle_delay_removed_without_mutating_original(self):
  p={'state':'ADMITTED','schedule_delay_seconds':30,'download_batch_blocks':2048}
  m=dict(cpu_busy_pct=10,io_pressure_pct=0,source_backoff_seconds=0,monthly_remaining_krw=50000)
  x=accelerate(p,m,{},chain_id=42161,enabled=True)
  self.assertEqual((x['schedule_delay_seconds'],x['download_batch_blocks']),(1,8192))
  self.assertEqual(p['schedule_delay_seconds'],30)
  for change in ({'source_backoff_seconds':90},{'cpu_busy_pct':85},{'io_pressure_pct':20},{'monthly_remaining_krw':9999}):
   y=accelerate(p,dict(m,**change),{},chain_id=42161,enabled=True)
   self.assertEqual(y['schedule_delay_seconds'],30)
  y=accelerate(p,m,{'source_min_delay':16,'source_batch_cap':64},chain_id=42161,enabled=True)
  self.assertEqual((y['schedule_delay_seconds'],y['download_batch_blocks']),(16,64))
  self.assertEqual(accelerate(p,m,{},chain_id=42161),p)

class BatchTests(unittest.TestCase):
 def test_out_of_order_response_is_bound_to_request_id(self):
  rpc=RPC();calls=[('eth_chainId',[]),('eth_getBlockByNumber',['0x1',False])]
  with patch.object(rpc,'exchange',return_value=[dict(jsonrpc='2.0',id=2,result={'number':'0x1'}),dict(jsonrpc='2.0',id=1,result='0x1237')]):
   records=rpc.batch(calls)
  self.assertEqual(result(records[0],*calls[0]),'0x1237')
  self.assertEqual(result(records[1],*calls[1]),{'number':'0x1'})
 def test_duplicate_missing_and_unrequested_results_rejected(self):
  rpc=RPC();calls=[('eth_chainId',[]),('eth_chainId',[])]
  for ids in ([1,1],[1,3],[1],[True,2]):
   with patch.object(rpc,'exchange',return_value=[dict(jsonrpc='2.0',id=i,result='0x1237') for i in ids]):
    with self.assertRaises(SourceIntegrityError):rpc.batch(calls)
  with self.assertRaises(ValueError):rpc.batch([('eth_sendRawTransaction',[])])
 def test_two_blocks_with_traces_use_three_batches(self):
  rpc=RPC();source=PublicSource();source.rpc=rpc;source.identity=envelope('eth_chainId',[],'0x1237');seen=[]
  def exchange(requests):
   seen.append(requests);response=[]
   for r in requests:
    method=r['method'];tag=r['params'][0];n=int(tag,16)
    if method=='eth_getBlockByNumber':
     value=fixture()['block']['response']['result'] if n else dict(number='0x0',hash=GENESIS,parentHash='0x'+'00'*32,transactions=[])
     if r['params'][1] is False:value=dict(value,transactions=[T] if n else [])
    elif method=='eth_getBlockReceipts':value=fixture()['receipts']['response']['result'] if n else []
    else:value=[dict(txHash=T,result=dict(type='CALL',value='0x1',**{'from':A,'to':B}))]
    response.append(dict(jsonrpc='2.0',id=r['id'],result=value))
   return list(reversed(response))
  with patch.object(rpc,'exchange',side_effect=exchange):page=source.fetch(0,1)
  self.assertEqual(page['last_block'],1);self.assertEqual(len(page['blocks'][1]['traces']),1)
  self.assertEqual(len(seen),3)

class ReceiptBatchTests(unittest.TestCase):
 def test_cache_is_scoped_to_one_batch_and_one_chain(self):
  with tempfile.TemporaryDirectory() as d:
   bridge=Bridge(d,None)
   class Client:
    identity_checked=True
    def __init__(self):self.count=0
    def result(self,method,params):
     self.count+=1;return {'n':self.count},{'method':method,'n':self.count}
   client=Client();bridge.clients[4663]=client;bridge.receipt_batch_cache={}
   records=[]
   self.assertEqual(bridge.request(4663,'eth_getTransactionReceipt',[T],records),{'n':1})
   self.assertEqual(bridge.request(4663,'eth_getTransactionReceipt',[T],records),{'n':1})
   self.assertEqual(len(records),2);self.assertEqual(client.count,1)
   bridge.receipt_batch_cache=None
   self.assertEqual(bridge.request(4663,'eth_getTransactionReceipt',[T],records),{'n':2})
   with self.assertRaises(ValueError):bridge.process(25)
   bridge.close()
 def test_exception_releases_cache(self):
  with tempfile.TemporaryDirectory() as d:
   bridge=Bridge(d,None)
   with patch.object(bridge,'_process',side_effect=ValueError('test')):
    with self.assertRaises(ValueError):bridge.process()
   self.assertIsNone(bridge.receipt_batch_cache);bridge.close()
