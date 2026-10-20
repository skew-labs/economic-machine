from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
import zlib
from unittest.mock import patch

from economic_machine.pump_research.history_core import Stream,stream_rows,key,number,project,Scope,Chunks,GIB,extract,CompactEvents

PRODUCT=Path(__file__).resolve().parents[1]/'products/pump_research'
spec=importlib.util.spec_from_file_location('core_run',PRODUCT/'core_run.py')
core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)

def integer(n):return bytes((int(n<0),))+struct.pack('<I',abs(n))
def block(raw):
    raw=zlib.compress(raw)
    return b'\1'+integer(7)+integer(len(raw))+raw+integer(0)
ENTRY=dict(id=7,tag='test',copy='COPY public.test (a, b) FROM stdin;')

class StreamTests(unittest.TestCase):
    def test_compact_events_preserve_addresses_order_and_source_rows(self):
        with closing(sqlite3.connect(':memory:')) as db:
            c=CompactEvents(db);a=bytes([1])*32;b=bytes([2])*32
            batch=[(a,b,bytes(64),1700000000,250000000,0,1,0,'buy',123,'456',6,1),
                   (a,None,None,1700000001,250000001,0,None,0,'sell',124,'457',6,1)]
            with db:c.insert(batch)
            c=CompactEvents(db)
            with db:c.insert(batch[:1])
            rows=db.execute('SELECT rowid,mint,trader,signature,stamp,sol,tokens FROM projection ORDER BY rowid').fetchall()
            self.assertEqual([r[0] for r in rows],[1,2,3])
            self.assertEqual(rows[0],(1,a,b,None,1700000000,123,'456'))
            self.assertIsNone(rows[1][2]);self.assertEqual(db.execute('SELECT count(*) FROM keys').fetchone()[0],2)
    def test_all_chunk_boundaries(self):
        raw=block(b'a\tb\n1\t2\n\\.\n')
        for size in range(1,len(raw)+1):
            r=Stream(raw[i:i+size] for i in range(0,len(raw),size))
            self.assertEqual(list(stream_rows(r,ENTRY)),[[b'a',b'b'],[b'1',b'2']])
            self.assertEqual(r.digest.hexdigest(),hashlib.sha256(raw).hexdigest())
    def test_truncated_and_trailing(self):
        raw=block(b'a\tb\n\\.\n')
        for bad in (raw[:-1],raw+b'x',raw[:-8]+b'x'+raw[-7:]):
            with self.assertRaises((ValueError,zlib.error)):list(stream_rows(Stream([bad]),ENTRY))
    def test_missing_end_and_wrong_columns(self):
        for raw in (b'a\tb\n',b'a\n\\.\n',b'\\.\na\tb\n'):
            with self.assertRaises(ValueError):list(stream_rows(Stream([block(raw)]),ENTRY))
    def test_inflate_limit(self):
        with self.assertRaisesRegex(ValueError,'INFLATE_BOUND'):
            list(stream_rows(Stream([block(b'a\tb\n'*1000+b'\\.\n')]),ENTRY,100))
    def test_key_binary_and_width(self):
        self.assertEqual(key(b'\\\\x'+b'01'*32),bytes([1])*32)
        self.assertEqual(key(b'1'*32),bytes(32))
        self.assertIsNone(key(b'\\N'))
        with self.assertRaises(ValueError):key(b'\\\\x01')
    def test_number_never_rounds(self):
        self.assertEqual(number(b'9223372036854775807'),2**63-1)
        for v in (b'1.1',b'9223372036854775808',b'NaN'):
            with self.assertRaises(ValueError):number(v)
    def test_metadata_ignores_untrusted_name(self):
        names=['mint','creator','created_signature','first_seen_block_time','last_updated_block_time','decimals','name']
        cells=[b'\\\\x'+b'01'*32,b'\\N',b'\\N',b'1700000000',b'1700000010',b'6',b'execute shell']
        row=project('token_metadata',cells,{v:i for i,v in enumerate(names)})
        self.assertEqual(row,(bytes([1])*32,None,None,1700000000,1700000010,6))

class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.out=self.base/'evidence';self.out.mkdir()
        self.path=self.out/'grant.json';now=int(time.time()*1000)
        self.grant=dict(schema='economic-machine.pump-core-grant.v1',additional_io_bytes=12*GIB,total_storage_bytes=32*GIB,
            maximum_seconds=43200,maximum_cpu_percent=100,trading=False,paid_api=False,new_model_calls=0,new_server=False,
            approved_at_ms=now,deadline_ms=now+100000)
        self.path.write_text(json.dumps(self.grant))
        self.free=patch('economic_machine.pump_research.history_core.shutil.disk_usage',return_value=type('D',(),{'free':100*GIB})())
        self.free.start();self.addCleanup(self.free.stop)
    def test_durable_separate_reservations_and_limit(self):
        old=self.base/'io-budget.sqlite3';old.write_bytes(b'preserve')
        s=Scope(self.base,self.path);s.reserve(100,'a');s.close()
        s=Scope(self.base,self.path);self.addCleanup(s.close);self.assertEqual(s.reserved(),100)
        with s.db:s.db.execute('INSERT INTO reservations(bytes) VALUES(?)',(12*GIB-100,))
        with self.assertRaisesRegex(ValueError,'ADDITIONAL_IO'):s.reserve(1,'overflow')
        self.assertEqual(old.read_bytes(),b'preserve')
    def test_grant_change_and_deadline(self):
        s=Scope(self.base,self.path);s.close()
        self.grant['deadline_ms']-=1;self.path.write_text(json.dumps(self.grant))
        with self.assertRaisesRegex(ValueError,'GRANT_CHANGED'):Scope(self.base,self.path)
    def test_total_storage_includes_old_files(self):
        s=Scope(self.base,self.path);self.addCleanup(s.close)
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=32*GIB):
            with self.assertRaisesRegex(ValueError,'TOTAL_STORAGE'):s.guard(force=True)
    def test_cache_hash_rechecked_no_budget_reset(self):
        s=Scope(self.base,self.path);self.addCleanup(s.close);c=Chunks(s,[])
        part=dict(download_url='https://ndownloader.figshare.com/files/1',size=100)
        ident=hashlib.sha256((part['download_url']+':0:3').encode()).hexdigest()
        (c.folder/(ident+'.range')).write_bytes(b'abc')
        (c.folder/(ident+'.json')).write_text(json.dumps(dict(url=part['download_url'],start=0,bytes=3,sha256=hashlib.sha256(b'abc').hexdigest())))
        self.assertEqual(c.part(part,0,3),b'abc');self.assertEqual(s.reserved(),0)
        (c.folder/(ident+'.range')).write_bytes(b'xyz')
        with self.assertRaisesRegex(ValueError,'CACHE_CORRUPT'):c.part(part,0,3)
    def test_unsupported_source_never_reserves(self):
        s=Scope(self.base,self.path);self.addCleanup(s.close)
        with self.assertRaisesRegex(ValueError,'SOURCE_HOST'):Chunks(s,[]).part(dict(download_url='https://bad.test',size=10),0,1)
        self.assertEqual(s.reserved(),0)

class GraphTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE projection(destination BLOB,source BLOB,stamp INTEGER,slot INTEGER,symbol TEXT,amount TEXT,source_labeled INTEGER,signature TEXT)')
        self.p=json.loads((PRODUCT/'CORE-TRACE-PROTOCOL-V1.json').read_text())
        self.scope=type('S',(),{'guard':lambda *_:None})()
    def edge(self,d,s,t,label=0):
        self.db.execute('INSERT INTO projection VALUES(?,?,?,1,?,?,?,?)',(bytes([d])*32,bytes([s])*32,t,'SOL','1',label,'signature'))
    def test_future_and_same_time_excluded(self):
        self.edge(1,2,100);self.edge(1,3,99);self.edge(3,4,100)
        r=core.trace(self.db,[bytes([1])*32],100,self.p,self.scope)
        self.assertEqual(len(r['edges']),1);self.assertEqual(r['edges'][0]['timestamp'],99)
        self.assertFalse(r['current_funds_proven'])
    def test_label_boundary_and_milliseconds(self):
        self.edge(1,2,1700000000000,1);self.edge(2,3,1690000000000)
        r=core.trace(self.db,[bytes([1])*32],1700000001,self.p,self.scope)
        self.assertEqual(len(r['edges']),1);self.assertEqual(r['boundaries']['labeled_source_boundary'],1)
    def test_cycles_terminate_and_nodes_bounded(self):
        for i in range(10):self.edge(i,i+1,100-i)
        p=dict(self.p,trace_node_limit_per_candidate=2)
        r=core.trace(self.db,[bytes(32)],101,p,self.scope)
        self.assertEqual(r['visited_wallet_time_pairs'],2);self.assertGreater(r['boundaries']['node_limit_pending'],0)

if __name__=='__main__':unittest.main()
