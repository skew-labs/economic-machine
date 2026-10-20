import gzip
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from economic_machine.arbitrum_research.transport import read_body
from economic_machine.arbitrum_research.source import PublicSource,SourceIntegrityError,SourceLimit
from test_arbitrum_source import Response,Opener,raw,block
import tokyo_worker as worker
import parallel_history as controller
from test_arbitrum_parallel import page

class TransportTests(unittest.TestCase):
 def response(self,data,encoding='gzip'):
  return Response(data,headers={'Content-Encoding':encoding})
 def test_full_gzip_and_identity_produce_same_page(self):
  body=raw([block(0),block(1)])
  a=PublicSource(opener=Opener(self.response(body,'identity'))).fetch(0,1)
  b=PublicSource(compressed=True,opener=Opener(self.response(gzip.compress(body)))).fetch(0,1)
  self.assertEqual(a['raw'],b['raw']);self.assertEqual(a['sha256'],b['sha256'])
  self.assertEqual(b['response']['compression_integrity'],'gzip_crc_complete')
  self.assertLess(b['response']['wire_bytes'],b['response']['decoded_bytes'])
 def test_bad_crc_incomplete_and_extra_members_rejected(self):
  body=gzip.compress(b'hello');bad=bytearray(body);bad[-5]^=1
  for data in (bytes(bad),body[:-1],body+b'x',body+body):
   with self.assertRaises(SourceIntegrityError):read_body(self.response(data),1024)
 def test_decoded_bomb_is_bounded_without_claiming_crc(self):
  response=self.response(gzip.compress(b'x'*100000))
  data,truncated,info=read_body(response,1024)
  self.assertEqual(data,b'x'*1024);self.assertTrue(truncated)
  self.assertEqual(info['decoded_bytes'],1025)
  self.assertEqual(info['compression_integrity'],'bounded_prefix_only')
  self.assertLessEqual(sum(response.read_limits),1025)
 def test_partial_ndjson_admits_only_complete_lines(self):
  body=raw([block(0),block(1),block(2)])
  limit=len(raw([block(0)]))+10
  a,partial,_=read_body(self.response(gzip.compress(body)),limit)
  from economic_machine.arbitrum_research.source import parse_page
  parsed=parse_page(a,0,2,truncated=partial)
  self.assertEqual(parsed['last_block'],0)
 def test_wire_header_bomb_and_unknown_encoding_rejected(self):
  # gzip FNAME header without a terminating byte cannot consume unbounded input.
  data=b'\x1f\x8b\x08\x08'+b'\x00'*6+b'x'*10000
  with self.assertRaises(SourceLimit):read_body(self.response(data),1024)
  with self.assertRaises(SourceIntegrityError):read_body(self.response(b'x','br'),1024)

class WorkerStageTests(unittest.TestCase):
 def job(self):return dict(id='a'*32,start=22207818,end=22207820)
 def test_notice_before_graph_and_final_package_still_validated(self):
  job=self.job();events=[]
  class Source:
   def fetch(self,start,end):return page(start,end)
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder)
   def notice(value):
    self.assertTrue((root/job['id']/'source.ndjson').is_file())
    self.assertFalse((root/job['id']/'index.sqlite3').exists());events.append(value)
   value=worker.prepare(job,root,Source(),notice)
   stream=io.BytesIO((json.dumps(events[0])+'\n'+json.dumps(value)+'\n').encode())
   released=[];header,size,retained=controller.read_worker_header(stream,job,lambda:released.append(True))
   self.assertEqual(released,[True]);self.assertEqual(header['source_sha256'],retained['source_sha256'])
   self.assertGreater(size,0);self.assertIn('index_seconds',header['timings'])
   for part in header['parts']:
    controller.unpack((root/job['id']/part['name']).read_bytes(),part,256*1024**2)
 def test_foreign_or_changed_notice_never_advances(self):
  job=self.job();notice=dict(event='SOURCE_RETAINED',job_id=job['id'],source_sha256='b'*64,
    first_block=job['start'],last_block=job['end'])
  for change in ({'job_id':'c'*32},{'first_block':0},{'last_block':job['end']+1},{'source_sha256':'bad'}):
   called=[]
   with self.assertRaises(ValueError):controller.read_worker_header(io.BytesIO((json.dumps(dict(notice,**change))+'\n').encode()),job,lambda:called.append(1))
   self.assertFalse(called)
  header=dict(source_sha256='d'*64,first_block=job['start'],last_block=job['end'])
  with self.assertRaisesRegex(ValueError,'MISMATCH'):
   controller.read_worker_header(io.BytesIO((json.dumps(notice)+'\n'+json.dumps(header)+'\n').encode()),job,lambda:None)
 def test_old_worker_header_remains_compatible(self):
  result=dict(schema='economic-machine.tokyo-public-shard.v1',job=self.job())
  called=[];header,_,notice=controller.read_worker_header(io.BytesIO((json.dumps(result)+'\n').encode()),self.job(),lambda:called.append(1))
  self.assertEqual(header,result);self.assertFalse(called);self.assertIsNone(notice)
