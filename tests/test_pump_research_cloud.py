import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest.mock import patch

from economic_machine.pump_research.research_cloud import (
    Cloud,CloudFile,archive_file,retire_file,load_reference,identity,
)
from economic_machine.pump_research.research_spend import Spend
from economic_machine.pump_research.gsfa_index import GsfaIndex,ChunkStream
from economic_machine.pump_research.store import atomic
from test_pump_research_spend import setup_budget
from test_pump_gsfa_index import fixture,budget

class FakeCloud:
    def __init__(self,base):
        self.base=Path(base);self.out=self.base/'evidence/research-budget-v1';self.spend=Spend(base)
        self.remote=self.base/'fake-remote';self.remote.mkdir(exist_ok=True)
    def close(self):self.spend.close()
    def guard_local(self,extra):pass
    def upload(self,path,expected=None):
        raw=Path(path).read_bytes();h=hashlib.sha256(raw).hexdigest();p=self.remote/h
        if not p.exists():
            self.spend.reserve('upload',h,bytes_=len(raw),digest=h);p.write_bytes(raw);self.spend.verified(h)
        return dict(sha256=h,bytes=len(raw))
    def cached(self,digest,size):
        p=self.remote/digest
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest or p.stat().st_size!=size:raise ValueError('RESTORE_HASH')
        return p.open('rb')

class CloudTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.base=Path(t.name);setup_budget(self.base)
        self.cloud=FakeCloud(self.base);self.addCleanup(self.cloud.close)
        self.patch=patch('economic_machine.pump_research.research_cloud.Cloud',FakeCloud);self.patch.start();self.addCleanup(self.patch.stop)
    def source(self,raw):
        p=self.base/'original';p.write_bytes(raw)
        return p,self.base/'reference.json',dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    def test_ram_cache_keeps_byte_parity_and_rechecks_verification(self):
        from economic_machine.pump_research.memory_cache import VerifiedCache
        cache=VerifiedCache(1024,lambda c,l:l)
        raw=b'abcdefgh'*20;p,ref,expected=self.source(raw)
        archive_file(self.cloud,p,ref,expected)
        with patch('economic_machine.pump_research.memory_cache.shared_cache',return_value=cache):
            reader=CloudFile(self.base,ref,expected)
            try:
                self.assertEqual(reader.read(3,110),raw[3:113])
                self.assertEqual(reader.read(7,90),raw[7:97])
                self.assertEqual(cache.loads,1);self.assertEqual(cache.hits,1)
                with self.cloud.spend.db:self.cloud.spend.db.execute('UPDATE objects SET verified=0')
                with self.assertRaisesRegex(ValueError,'UNVERIFIED_OBJECT'):reader.read(0,4)
            finally:reader.close()
    def test_multiblock_read_and_retirement_preserve_bytes(self):
        raw=b'0123456789'*50;p,ref,expected=self.source(raw)
        with patch('economic_machine.pump_research.research_cloud.BLOCK',64):archive_file(self.cloud,p,ref,expected)
        self.assertEqual(retire_file(self.cloud,p,ref,expected),len(raw));self.assertFalse(p.exists())
        reader=CloudFile(self.base,ref,expected)
        try:
            self.assertEqual(reader.read(60,140),raw[60:200]);self.assertEqual(reader.read(500,0),b'')
        finally:reader.close()
    def test_source_hash_mismatch_never_publishes_reference(self):
        p,ref,expected=self.source(b'raw');expected['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED'):archive_file(self.cloud,p,ref,expected)
        self.assertTrue(p.exists());self.assertFalse(ref.exists())
    def test_changed_original_blocks_retirement(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected);p.write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'RETIRE_CHANGED'):retire_file(self.cloud,p,ref,expected)
        self.assertTrue(p.exists())
    def test_unverified_object_blocks_retirement(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected)
        with self.cloud.spend.db:self.cloud.spend.db.execute('UPDATE objects SET verified=0')
        with self.assertRaisesRegex(ValueError,'RETIRE_UNVERIFIED'):retire_file(self.cloud,p,ref,expected)
        self.assertTrue(p.exists())
    def test_missing_catalog_receipt_blocks_retirement(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected);ref.with_suffix('.receipt.json').unlink()
        with self.assertRaises(FileNotFoundError):retire_file(self.cloud,p,ref,expected)
        self.assertTrue(p.exists())
    def test_malformed_block_offsets_rejected(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected)
        value=json.loads(ref.read_bytes());value['blocks'][0]['offset']=1;atomic(ref,value)
        with self.assertRaisesRegex(ValueError,'BLOCK'):load_reference(ref,expected)
    def test_missing_cloud_object_is_explicit_failure(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected);retire_file(self.cloud,p,ref,expected)
        (self.cloud.remote/expected['sha256']).unlink();reader=CloudFile(self.base,ref,expected)
        try:
            with self.assertRaises(FileNotFoundError):reader.read(0,3)
        finally:reader.close()
    def test_index_parity_across_cloud_only_linked_log(self):
        d=self.base/'gsfa';wallet,_,raw,_=fixture(d)
        with GsfaIndex(d,685,budget()) as index:before=index.page(wallet,limit=2)
        p=d/'linked-log';ref=d/'linked-log-cloud.json';expected=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        archive_file(self.cloud,p,ref,expected);retire_file(self.cloud,p,ref,expected)
        with GsfaIndex(d,685,budget(),linked_reader=CloudFile(self.base,ref,expected)) as index:
            after=index.page(wallet,limit=2);self.assertEqual(before,after)
            self.assertEqual(len(index.page(wallet,cursor=after['next_cursor'])['candidates']),1)
    def test_retired_compressed_chunk_stream_keeps_combined_hash(self):
        folder=self.base/'evidence/address-epochs-v2/epochs/1009';(folder/'chunks').mkdir(parents=True)
        (folder/'cloud-chunks').mkdir();raw=b'compressed-placeholder';p=folder/'chunks/part';p.write_bytes(raw)
        row=dict(path='part',start=0,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        ref=folder/'cloud-chunks/part.json';archive_file(self.cloud,p,ref,row);retire_file(self.cloud,p,ref,row)
        manifest=folder/'download-manifest.json';atomic(manifest,dict(source=dict(size=len(raw)),chunks=[row],sha256=row['sha256']))
        stream=ChunkStream(manifest,budget())
        try:
            self.assertEqual(stream.read(100),raw);self.assertEqual(stream.read(100),b'');self.assertTrue(stream.verified)
        finally:stream.close()
    def test_restart_reuses_uploaded_blocks(self):
        p,ref,expected=self.source(b'raw');archive_file(self.cloud,p,ref,expected)
        count=self.cloud.spend.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        archive_file(self.cloud,p,ref,expected)
        self.assertEqual(self.cloud.spend.db.execute('SELECT count(*) FROM operations').fetchone()[0],count)
    def test_research_storage_guard_precedes_staging(self):
        c=Cloud.__new__(Cloud);c.base=self.base;c.spend=self.cloud.spend;c.last_storage_check=0;c.local_bytes=0
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=c.spend.grant['local_storage_bytes']):
            with self.assertRaisesRegex(ValueError,'LOCAL_STORAGE_LIMIT'):c.guard_local(1)
    def test_host_free_floor_is_preserved(self):
        c=Cloud.__new__(Cloud);c.base=self.base;c.spend=self.cloud.spend;c.last_storage_check=0;c.local_bytes=0
        with patch('economic_machine.pump_research.history_core.used_bytes',return_value=0),patch(
                'economic_machine.pump_research.research_cloud.shutil.disk_usage',return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError,'LOCAL_FREE_SPACE'):c.guard_local(1)
    def test_epoch_worker_migrates_and_resumes_without_rewriting_import(self):
        from economic_machine.pump_research.research_expansion import ExpansionScope
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'products/pump_research'))
        from cloud_history import migrate_epoch
        folder=self.base/'evidence/address-epochs-v2/epochs/1009';folder.mkdir(parents=True)
        d=folder/'gsfa-index-1009';wallet,_,_,_=fixture(d,epoch=1009)
        rows=[dict(name=p.name,bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in d.iterdir()]
        with GsfaIndex(d,1009,budget()) as index:
            query=index.page(wallet,limit=8);identity_=index.identity;root=index.root_cid
        (folder/'chunks').mkdir();raw=b'retained compressed fixture';(folder/'chunks/part').write_bytes(raw)
        sha=hashlib.sha256(raw).hexdigest();atomic(folder/'download-manifest.json',dict(sha256=sha,
            chunks=[dict(path='part',bytes=len(raw),sha256=sha,start=0)],source=dict(size=len(raw))))
        receipt=dict(epoch=1009,index_query_verified=True,directory=d.name,source_sha256=sha,index_identity=identity_,
            root_cid=root,extraction=dict(files=rows),queries=[query])
        atomic(folder/'import-receipt.json',receipt);before=(folder/'import-receipt.json').read_bytes();events=[]
        with patch('economic_machine.pump_research.research_continuation.shutil.disk_usage',return_value=SimpleNamespace(free=400*1024**3)):
            scope=ExpansionScope(self.base)
            try:
                migrate_epoch(self.base,1009,scope,self.cloud,lambda state,**body:events.append((state,body)))
                self.assertEqual(events[-1][0],'EPOCH_CLOUD_SEARCH_READY')
                self.assertFalse((d/'linked-log').exists());self.assertFalse((folder/'chunks/part').exists())
                self.assertTrue((d/'pubkey-to-offset-and-size.index').exists())
                self.assertEqual((folder/'import-receipt.json').read_bytes(),before)
                migrate_epoch(self.base,1009,scope,self.cloud,lambda state,**body:events.append((state,body)))
                self.assertEqual(events[-1][1]['duplicate_bytes_retired_this_run'],0)
            finally:scope.close()

if __name__=='__main__':unittest.main()
