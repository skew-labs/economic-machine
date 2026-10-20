from historical_scope import HistoricalScopeTestCase
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from economic_machine.pump_research.bulk_history import (
    plan_ranges,RangeHTTP,Journal,Reader,run_window,validate_block,DEADLINE_MS,MAX_BLOCK)
from economic_machine.pump_research.address_index import AddressIndex,parse_index_block,search
from test_pump_gsfa_history import make_block,Metadata,SLOT
from test_pump_gsfa_history import compress
from economic_machine.pump_research.bulk_decode import decompress as fast_decompress,MAX_METADATA
from economic_machine.pump_research.archive import decompress as reference_decompress


class DecodeTests(HistoricalScopeTestCase):
    def test_exact_output_at_varied_sizes_and_buffer_reuse(self):
        for size in (1,15,1024,65536,MAX_METADATA,2):
            raw=b'x'*size;frame=compress(raw)
            self.assertEqual(fast_decompress(frame),reference_decompress(frame))
    def test_truncated_corrupt_and_bomb(self):
        frame=compress(b'hello'*1000)
        for bad in (b'bad',frame[:-1],frame[:6],compress(b'x'*(MAX_METADATA+1)),compress(b'')):
            with self.assertRaises(ValueError):fast_decompress(bad)
    def test_full_signed_block_semantic_parity(self):
        for failed in (False,True):
            for loaded in (False,True):
                f=make_block(failed=failed,loaded=loaded)
                self.assertEqual(parse_index_block(f['raw'],SLOT,Metadata),
                    parse_index_block(f['raw'],SLOT,Metadata,metadata_decompress=fast_decompress))


class Response(io.BytesIO):
    def __init__(self,body,start=0,total=100,status=206,etag='"one"',encoding='identity'):
        super().__init__(body);self.status=status;self.will_close=False
        self.headers={'Content-Range':f'bytes {start}-{start+len(body)-1}/{total}',
                      'ETag':etag,'Content-Encoding':encoding,'Retry-After':'99'}

class Connection:
    def __init__(self,responses):self.responses=responses;self.closed=False;self.requests=[]
    def request(self,*a,**kw):self.requests.append((a,kw))
    def getresponse(self):return self.responses.pop(0)
    def close(self):self.closed=True


class PlannerTests(HistoricalScopeTestCase):
    def test_adjacent_only_and_provider_holes(self):
        raw=b''.join(struct.pack('<QI',o,n) for o,n in [(100,10),(110,5),(0,0),(115,7),(130,9)])
        p=plan_ranges(raw,100)
        self.assertEqual([(x['offset'],x['bytes']) for x in p['pages']],[(100,22),(130,9)])
        self.assertEqual(p['provider_absent_slots'],[102])
    def test_overlap_huge_and_cross_epoch_rejected(self):
        for raw,first in [(struct.pack('<QI',1,MAX_BLOCK+1),0),
                          (struct.pack('<QI',2**64-1,10),0),
                          (struct.pack('<QI',10,20)+struct.pack('<QI',29,2),0),
                          (bytes(24),431999),(b'bad',0)]:
            with self.assertRaises(ValueError):plan_ranges(raw,first)
    def test_memory_and_block_count_caps(self):
        raw=b''.join(struct.pack('<QI',i*MAX_BLOCK,MAX_BLOCK) for i in range(3))
        self.assertEqual(len(plan_ranges(raw,0)['pages']),2)
        raw=b''.join(struct.pack('<QI',i*10,10) for i in range(17))
        self.assertEqual(len(plan_ranges(raw,0)['pages']),2)


class TransportTests(HistoricalScopeTestCase):
    url='https://files.old-faithful.net/685/epoch-685.car'
    def test_connection_reuse_exact_ranges_source_pin(self):
        c=Connection([Response(b'abc'),Response(b'def',start=3)])
        h=RangeHTTP(lambda *a,**k:c)
        raw,pin=h.read(self.url,0,3);self.assertEqual(raw,b'abc')
        self.assertEqual(h.read(self.url,3,3,pin)[0],b'def')
        self.assertEqual(c.requests[1][1]['headers']['If-Match'],'"one"')
    def test_source_change_short_bad_encoding_and_wrong_range(self):
        cases=[(Response(b'abc',etag='"two"'),{'total_bytes':100,'etag':'"one"'}),
               (Response(b'abc',total=99),{'total_bytes':100,'etag':'"one"'}),
               (Response(b'abc',encoding='gzip'),None),(Response(b'abc',start=1),None)]
        for r,pin in cases:
            c=Connection([r]);h=RangeHTTP(lambda *a,**k:c)
            with self.assertRaises(ValueError):h.read(self.url,0,3,pin)
            self.assertTrue(c.closed)
        r=Response(b'ab');r.headers['Content-Range']='bytes 0-2/100'
        with self.assertRaises(ValueError):RangeHTTP(lambda *a,**k:Connection([r])).read(self.url,0,3)
    def test_http_error_is_not_retried_or_followed(self):
        for status in [301,403,429,503]:
            c=Connection([Response(b'',status=status)]);h=RangeHTTP(lambda *a,**k:c)
            with self.assertRaises(urllib.error.HTTPError) as e:h.read(self.url,0,3)
            self.assertEqual(e.exception.code,status);self.assertEqual(len(c.requests),1)
            self.assertEqual(e.exception.headers['Retry-After'],'99')
    def test_host_and_request_bounds(self):
        for url,start,n in [('https://evil.test/685/epoch-685.car',0,1),
                            (self.url,-1,1),(self.url,0,0),(self.url,0,33*1024**2)]:
            with self.assertRaises(ValueError):RangeHTTP().read(url,start,n)


class Cloud:
    def __init__(self):
        self.objects={};self.fail=False;self.spend=SimpleNamespace(reserve=lambda *a,**k:None)
    def guard_local(self,n):pass
    def upload(self,path,expected=None):
        if self.fail:raise RuntimeError('readback unavailable')
        raw=Path(path).read_bytes();h=hashlib.sha256(raw).hexdigest()
        if expected and h!=expected:raise ValueError('cloud hash')
        self.objects[h]=raw;return dict(sha256=h,bytes=len(raw))


class IntegrationTests(HistoricalScopeTestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.base=Path(t.name)
        self.policy=dict(deadline_ms=DEADLINE_MS,maximum_source_bytes=1024**2)
        self.j=Journal(self.base/'bulk',self.policy);self.addCleanup(self.j.close)
        self.index=AddressIndex(self.base/'index.sqlite3');self.addCleanup(self.index.close)
        self.cloud=Cloud();self.fixtures=[make_block(slot=SLOT+i,failed=i==1) for i in range(3)]
        self.car=b''.join(f['raw'] for f in self.fixtures);offset=1000;rows=[]
        for f in self.fixtures:rows.append(struct.pack('<QI',offset,len(f['raw'])));offset+=len(f['raw'])
        self.table=b''.join(rows);self.calls=[]
        def read(url,start,size,pin=None):
            self.calls.append((url,start,size))
            if url.endswith('.raw'):raw=self.table;total=432000*12
            else:raw=self.car[start-1000:start-1000+size];total=1000+len(self.car)
            return raw,dict(total_bytes=total,etag='"fixed"')
        self.scope=SimpleNamespace(base=self.base,guard=lambda:None,reserve=lambda *a:None,assert_slot=lambda *a:None)
        self.reader=Reader(self.j,self.scope,self.cloud,SimpleNamespace(read=read))
        self.parse=lambda raw,slot:parse_index_block(raw,slot,Metadata)

    def run_window(self):return run_window(self.j,self.reader,self.index,SLOT,SLOT+2,self.parse)

    def test_real_signatures_membership_search_and_replay(self):
        result=self.run_window();self.assertEqual(result['indexed_blocks'],3)
        self.assertEqual(result['decoded_transactions'],3);self.assertEqual(len(self.calls),2)
        rows=search(self.index.path,self.fixtures[0]['payer'])['transactions']
        self.assertEqual(len(rows),3);self.assertTrue(all(r['signature_verification'] for r in rows))
        self.assertEqual(sum(bool(r['transfers']) for r in rows),2)
        before=self.j.db.execute('select body from pages').fetchone()[0]
        self.run_window();self.assertEqual(len(self.calls),2)
        self.assertEqual(self.j.db.execute('select body from pages').fetchone()[0],before)
        self.assertEqual(self.index.db.execute('select count(*) from transactions').fetchone()[0],3)

    def test_blob_failure_never_advances_index_or_job(self):
        self.cloud.fail=True
        with self.assertRaises(RuntimeError):self.run_window()
        self.assertEqual(self.index.db.execute('select count(*) from blocks').fetchone()[0],0)
        self.assertEqual(self.j.db.execute('select count(*) from pages').fetchone()[0],0)
        self.cloud.fail=False;self.run_window();self.assertEqual(len(self.calls),2)

    def test_crash_after_one_block_replays_without_duplicate(self):
        original=self.index.ingest;calls=[]
        def fail(block,receipt):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('crash')
            return original(block,receipt)
        with patch.object(self.index,'ingest',side_effect=fail):
            with self.assertRaises(RuntimeError):self.run_window()
        self.assertEqual(self.index.db.execute('select count(*) from blocks').fetchone()[0],1)
        self.run_window();self.assertEqual(len(self.calls),2)
        self.assertEqual(self.index.db.execute('select count(*) from blocks').fetchone()[0],3)

    def test_bad_signature_and_cid_do_not_publish_partial_page(self):
        for tamper in ['signature','cid']:
            bad=make_block(slot=SLOT,tamper=tamper=='signature')['raw']
            if tamper=='cid':bad=bad[:-1]+bytes([bad[-1]^1])
            with self.assertRaises(ValueError):validate_block(self.parse(bad,SLOT),SLOT)
        with patch.object(self,'parse',return_value=dict(slot=SLOT,transactions=[],transaction_nodes=1,decode_errors=['bad'])):
            with self.assertRaisesRegex(ValueError,'PARTIAL_DECODE'):self.run_window()
        self.assertEqual(self.index.db.execute('select count(*) from blocks').fetchone()[0],0)

    def test_budget_deadline_and_mutated_cache_stop_before_fetch(self):
        self.j.policy['maximum_source_bytes']=1
        with self.assertRaisesRegex(ValueError,'ALLOWANCE'):self.run_window()
        self.assertFalse(self.calls);self.j.policy['maximum_source_bytes']=1024**2
        with patch('economic_machine.pump_research.bulk_history.time.time_ns',return_value=DEADLINE_MS*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):self.run_window()
        self.assertFalse(self.calls);self.run_window()
        path=next((self.j.out/'ranges').glob('*.raw'));path.write_bytes(b'bad')
        # Explicit read validates cached source even after a completed job.
        row=self.j.db.execute('select * from ranges where id=?',(path.stem,)).fetchone()
        with self.assertRaisesRegex(ValueError,'CACHE_'):self.reader.read(row['url'],row['start'],row['bytes'])

    def test_provider_hole_is_preserved_and_source_pin_cannot_change(self):
        self.table=struct.pack('<QI',0,0)*3
        result=self.run_window();self.assertEqual(result['indexed_blocks'],0)
        plan=json.loads(self.j.db.execute('select plan from jobs').fetchone()[0])
        self.assertEqual(plan['provider_absent_slots'],[SLOT,SLOT+1,SLOT+2])
        self.assertFalse(result['full_history_complete'])
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):self.j.pin(self.calls[0][0],dict(total_bytes=1,etag='"changed"'))


if __name__=='__main__':unittest.main()
