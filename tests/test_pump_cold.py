import ctypes
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import zlib
from economic_machine.pump_research.originals import Originals,HEADER
from economic_machine.pump_research.store import Store,canonical
from economic_machine.pump_research.reference_compact import compact
from economic_machine.pump_research.io_budget import reserve,LIMIT
from economic_machine.pump_research.ticking import consume

def compress(raw):
    lib=ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_compress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    lib.ZSTD_compress.restype=ctypes.c_size_t
    out=ctypes.create_string_buffer(len(raw)+1024)
    n=lib.ZSTD_compress(out,len(out),raw,len(raw),9)
    if n>len(out):raise ValueError('test compressor')
    return out.raw[:n]

class ColdTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'ticking'
        self.folder=self.root/'data/result-archive/originals/00000001';self.folder.mkdir(parents=True)
        self.s=Store(Path(self.tmp.name)/'research',reserve=0)
        self.raws=[b'first'*100,b'second'*200,b'third']
        self.hot=b'';self.cold=b'';self.refs=[]
        for seq,raw in enumerate(self.raws,1):
            packed=zlib.compress(raw) if seq==2 else raw;magic=b'SLZ1' if seq==2 else b'SLI1'
            head=HEADER.pack(magic,len(packed),seq,seq*100,zlib.crc32(raw));start=len(self.hot)
            self.hot+=head+packed;self.cold+=head+struct.pack('<I',len(raw))+raw
            self.refs.append(dict(root=str(self.root),segment='00000001',start=start,end=len(self.hot),sequence=seq))
        (self.folder/'inputs.sli').write_bytes(self.hot)
        self.r=Originals(self.root)
    def tearDown(self):
        self.r.close()
        if self.s._originals:self.s._originals.close()
        self.s.db.close();self.tmp.cleanup()
    def sealed(self,canonical_raw=None):
        raw=self.cold if canonical_raw is None else canonical_raw;packed=compress(raw)
        (self.folder/'inputs.sli').unlink();(self.folder/'inputs.slc').write_bytes(packed)
        v=dict(schema=1,codec='zstd-canonical-frames-v1',segment='00000001',canonicalBytes=len(raw),sourceBytes=len(self.hot),
               frames=3,canonicalSHA256=hashlib.sha256(raw).hexdigest(),capsuleSHA256=hashlib.sha256(packed).hexdigest())
        (self.folder/'capsule.json').write_text(canonical(dict(value=v,sha256=hashlib.sha256(canonical(v).encode()).hexdigest())))
        self.r.client=NS(read_compressed=lambda folder,v:(folder/'inputs.slc').read_bytes())
        return v,packed
    def assert_frames(self,reader):
        for ref,expected in zip(self.refs,self.raws):
            self.assertEqual(reader.read_reference(ref,hashlib.sha256(expected).hexdigest(),len(expected)),expected)
    def test_hot_exact_logical_positions(self):self.assert_frames(self.r)
    def test_cold_exact_offsets_cached_once(self):
        self.sealed();self.assert_frames(self.r);self.assert_frames(self.r);self.assertEqual(self.r.cold_loads,1)
    def test_cold_frame_cursor_must_be_boundary(self):
        self.sealed()
        with self.assertRaisesRegex(ValueError,'CURSOR'):self.r.frame('00000001',1,len(self.hot))
    def test_cold_limit_must_not_split_frame(self):
        self.sealed()
        with self.assertRaisesRegex(ValueError,'BOUNDARY'):self.r.frame('00000001',0,self.refs[0]['end']-1)
    def test_capsule_corruption(self):
        self.sealed();(self.folder/'inputs.slc').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'HASH'):self.r.size('00000001')
    def test_capsule_trailing_data_rejected_even_if_hash_resealed(self):
        v,p=self.sealed();p+=b'trailing';v['capsuleSHA256']=hashlib.sha256(p).hexdigest()
        (self.folder/'inputs.slc').write_bytes(p)
        (self.folder/'capsule.json').write_text(canonical(dict(value=v,sha256=hashlib.sha256(canonical(v).encode()).hexdigest())))
        with self.assertRaisesRegex(ValueError,'FRAME_BOUND'):self.r.size('00000001')
    def test_crc_validates_all_frames_before_exposing_first(self):
        self.sealed(self.cold[:-1]+b'X')
        with self.assertRaisesRegex(ValueError,'CRC'):self.r.frame('00000001',0,len(self.hot))
    def test_manifest_corruption(self):
        self.sealed();p=self.folder/'capsule.json';v=json.loads(p.read_text());v['value']['sourceBytes']+=1;p.write_text(canonical(v))
        with self.assertRaisesRegex(ValueError,'MANIFEST'):self.r.size('00000001')
    def test_no_missing_source_skip(self):
        (self.folder/'inputs.sli').unlink()
        with self.assertRaisesRegex(ValueError,'PRESERVE_CURSOR'):self.r.size('00000001')
    def test_seq_hash_and_length_bound_reference(self):
        raw=self.raws[0];h=hashlib.sha256(raw).hexdigest()
        for ref,digest,size in [(dict(self.refs[0],sequence=2),h,len(raw)),(self.refs[0],'0'*64,len(raw)),(self.refs[0],h,len(raw)+1)]:
            with self.assertRaisesRegex(ValueError,'REFERENCE'):self.r.read_reference(ref,digest,size)
    def test_client_version_pinned(self):
        self.sealed();self.r.client=None;(self.root/'blob_capsule.py').write_text('raise Exception("must not execute")')
        with self.assertRaisesRegex(ValueError,'CLIENT_CHANGED'):self.r.size('00000001')
    def test_remote_reserves_before_fetch_and_failure_not_refunded(self):
        v,packed=self.sealed();(self.folder/'inputs.slc').unlink()
        self.r.reserve=lambda n,p:reserve(self.s,n,p)
        def fail(*_):raise OSError('broker unavailable')
        self.r.client=NS(reference=lambda *_:dict(bytes=len(packed)),read_compressed=fail)
        for _ in range(2):
            with self.assertRaises(OSError):self.r.size('00000001')
        import sqlite3
        with sqlite3.connect(self.s.root/'io-budget.sqlite3') as db:
            self.assertEqual(db.execute('SELECT sum(bytes) FROM reservations').fetchone()[0],2*len(packed))
    def test_remote_budget_required(self):
        self.sealed();(self.folder/'inputs.slc').unlink();self.r.client=NS(reference=lambda *_:dict(bytes=100))
        with self.assertRaisesRegex(ValueError,'RESERVATION_REQUIRED'):self.r.size('00000001')
    def test_budget_imports_legacy_once_and_survives_ingest_rollback(self):
        with self.s.db:self.s.set_state('archive_network_reserved',LIMIT-10)
        with self.assertRaises(RuntimeError):
            with self.s.db:reserve(self.s,5,'read');raise RuntimeError('rollback')
        self.assertEqual(reserve(self.s,5,'retry'),LIMIT)
        with self.assertRaisesRegex(ValueError,'CUMULATIVE'):reserve(self.s,1,'retry')
    def test_new_references_avoid_duplicate_cas(self):
        for ref,raw in zip(self.refs,self.raws):
            with self.s.db:self.s.evidence('ticking',f"00000001:{ref['start']}:{ref['sequence']}",raw,ref)
        self.assertEqual(list((self.s.root/'objects').iterdir()),[])
        self.assertEqual(self.s.state('object_bytes'),0)
        for raw in self.raws:self.assertEqual(self.s.read_object(hashlib.sha256(raw).hexdigest()),raw)
    def test_wrong_locator_not_referenced(self):
        with self.assertRaisesRegex(ValueError,'LOCATOR'):self.s.evidence('ticking','wrong',self.raws[0],self.refs[0])
    def legacy(self):
        with self.s.db:
            for ref,raw in zip(self.refs,self.raws):self.s.evidence('ticking',f"00000001:{ref['start']}:{ref['sequence']}",raw)
    def test_compaction_verified_and_idempotent(self):
        self.legacy();r=compact(self.s,self.root)
        self.assertEqual(r['exact_objects_verified'],3);self.assertEqual(r['raw_identity_before'],r['raw_identity_after'])
        self.assertTrue((self.folder/'inputs.sli').exists());self.assertEqual(list((self.s.root/'objects').iterdir()),[])
        self.assertEqual(compact(self.s,self.root)['exact_objects_verified'],0)
        for raw in self.raws:self.assertEqual(self.s.read_object(hashlib.sha256(raw).hexdigest()),raw)
    def test_compaction_mismatch_preserves_copy(self):
        self.legacy();(self.folder/'inputs.sli').write_bytes(b'bad'+self.hot[3:])
        with self.assertRaises(ValueError):compact(self.s,self.root)
        self.assertEqual(len(list((self.s.root/'objects').iterdir())),3)
    def test_crash_after_reference_commit_keeps_readable_and_recovers(self):
        self.legacy()
        with patch.object(Path,'unlink',side_effect=OSError('injected interruption')):
            with self.assertRaises(OSError):compact(self.s,self.root)
        self.s.db.execute('DROP TABLE ref_work')
        for raw in self.raws:self.assertEqual(self.s.read_object(hashlib.sha256(raw).hexdigest()),raw)
        self.assertEqual(compact(self.s,self.root)['crash_duplicates_recovered'],3)
    def setup_tail(self):
        folder=self.folder.parent.parent
        (folder/'durable-tail.json').write_text(canonical(dict(segment='00000001',offset=len(self.hot))))
        (folder/'status.json').write_text(canonical(dict(lastSlot=123,unrepairedGapWitnesses=1,completeGapRecoveryVerified=False)))
        return NS(SubscribeUpdate=NS(FromString=lambda raw:NS(WhichOneof=lambda _:None)),SLOT_DEAD=6)
    def test_consumer_restart_preserves_cold_logical_offset(self):
        pb=self.setup_tail();self.sealed();self.s._originals=self.r
        a=consume(self.s,self.root,pb,max_frames=1);self.assertEqual(a['offset'],self.refs[0]['end'])
        b=consume(self.s,self.root,pb,max_frames=2);self.assertEqual(b['offset'],len(self.hot));self.assertEqual(b['frames'],3)
    def test_consumer_error_leaves_committed_cursor(self):
        pb=self.setup_tail();consume(self.s,self.root,pb,max_frames=1);before=self.s.state('ticking')
        with patch.object(self.s._originals,'frame',side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):consume(self.s,self.root,pb,max_frames=2)
        self.assertEqual(self.s.state('ticking'),before)

if __name__=='__main__':unittest.main()
