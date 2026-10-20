from historical_scope import HistoricalScopeTestCase

from concurrent.futures import Future
import hashlib,io,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from economic_machine.arbitrum_research.freezer_range_store import PortableRanges,digest,enabled,DEADLINE
from economic_machine.arbitrum_research.freezer_source import Snapshot
from economic_machine.pump_research.store import canonical
from test_freezer_source_pipeline import PipelinedSourceTests

class Cloud:
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.execute('create table objects(digest primary key,bytes,verified)')
        self.spend=SimpleNamespace(db=self.db,hash='budget',reserve=lambda *a,**kw:None)
        self.data={};self.fail=False;self.guard_calls=[]
    def guard_local(self,n=0):self.guard_calls.append(n)
    def upload(self,path,sha=None):
        if self.fail:raise ValueError('BLOB_FAILED')
        raw=Path(path).read_bytes();h=digest(raw)
        if sha and sha!=h:raise ValueError('HASH')
        self.data[h]=raw
        with self.db:self.db.execute('insert or replace into objects values(?,?,1)',(h,len(raw)))
        return {'sha256':h,'bytes':len(raw)}
    def cached(self,sha,size):return io.BytesIO(self.data[sha])

class PortableTests(HistoricalScopeTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'ranges').mkdir()
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('create table identity(body);insert into identity values("original");create table ranges(id primary key,start,bytes,sha256,proof);create table attempts(id primary key,bytes,state);')
        self.cloud=Cloud();self.p=PortableRanges(self.root,self.db,self.cloud)
    def tearDown(self):self.db.close();self.cloud.db.close();self.tmp.cleanup()
    def stage(self,raw=b'evidence',offset=8):
        key=digest(f'{offset}:{len(raw)}'.encode());path,ident=self.p.stage(key,raw,self.root/'ranges'/(key+'.raw'))
        with self.db:self.db.execute('insert into attempts values(?,?,?)',(key,len(raw),'reserved'))
        item=dict(absolute=offset,size=len(raw),digest=digest(raw),working_path=str(path),working_identity=ident,ids=[key])
        proof=self.cloud.upload(path)
        return key,item,proof
    def committed(self):
        key,item,proof=self.stage();record=self.p.prepare(key,item,proof);self.p.commit(key,item,proof,record)
        return key,item,proof
    def test_fresh_commit_retire_and_restored_bytes(self):
        k,i,p=self.committed();self.p.retire_new(k,i,p)
        self.assertFalse(Path(i['working_path']).exists());self.assertFalse(list((self.root/'ranges').iterdir()))
        reopened=PortableRanges(self.root,self.db,self.cloud)
        self.assertEqual(reopened.read(k,i['absolute'],i['size'],i['digest'],p),b'evidence')
        self.assertEqual(self.db.execute('select state from attempts').fetchone()[0],'received')
    def test_failure_manifest_upload_retains_work_and_reservation(self):
        k,i,p=self.stage();self.cloud.fail=True
        with self.assertRaisesRegex(ValueError,'BLOB_FAILED'):self.p.prepare(k,i,p)
        self.assertTrue(Path(i['working_path']).exists())
        self.assertEqual(self.db.execute('select count(*) from ranges').fetchone()[0],0)
        self.assertEqual(self.db.execute('select state from attempts').fetchone()[0],'reserved')
    def test_commit_interruption_rolls_back_both_tables(self):
        k,i,p=self.stage();r=self.p.prepare(k,i,p)
        self.db.execute("create trigger reject_portable before insert on portable_ranges begin select raise(ABORT,'crash'); end")
        with self.assertRaises(sqlite3.IntegrityError):self.p.commit(k,i,p,r)
        self.assertEqual(self.db.execute('select count(*) from ranges').fetchone()[0],0)
        self.assertEqual(self.db.execute('select state from attempts').fetchone()[0],'reserved')
        self.assertTrue(Path(i['working_path']).exists())
    def test_commit_crash_before_retirement_keeps_work_and_readable_proof(self):
        k,i,p=self.committed();self.assertTrue(Path(i['working_path']).exists())
        reopened=PortableRanges(self.root,self.db,self.cloud)
        self.assertEqual(reopened.read(k,i['absolute'],i['size'],i['digest'],p),b'evidence')
        self.assertTrue(Path(i['working_path']).exists())
    def test_deadline_during_upload_prevents_commit(self):
        k,i,p=self.stage();r=self.p.prepare(k,i,p)
        with patch('economic_machine.arbitrum_research.freezer_range_store.time.time_ns',return_value=DEADLINE*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):self.p.commit(k,i,p,r)
        self.assertEqual(self.db.execute('select count(*) from ranges').fetchone()[0],0)
        self.assertTrue(Path(i['working_path']).exists())
    def test_deadline_prevents_retirement(self):
        k,i,p=self.committed()
        with patch('economic_machine.arbitrum_research.freezer_range_store.time.time_ns',return_value=DEADLINE*1000000):
            with self.assertRaisesRegex(ValueError,'DEADLINE'):self.p.retire_new(k,i,p)
        self.assertTrue(Path(i['working_path']).exists())
    def test_changed_identity_during_upload_prevents_commit(self):
        k,i,p=self.stage();r=self.p.prepare(k,i,p)
        self.db.execute("update identity set body='other'");self.db.commit()
        with self.assertRaisesRegex(ValueError,'SOURCE_IDENTITY'):self.p.commit(k,i,p,r)
    def test_missing_manifest_prevents_read(self):
        k,i,p=self.committed();(self.p.manifests/(k+'.json')).unlink()
        with self.assertRaisesRegex(ValueError,'MANIFEST_BOUND'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_missing_reference_prevents_read(self):
        k,i,p=self.committed();self.db.execute('delete from portable_ranges');self.db.commit()
        with self.assertRaisesRegex(ValueError,'REFERENCE_MISSING'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_tampered_manifest_prevents_read(self):
        k,i,p=self.committed();(self.p.manifests/(k+'.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError,'MANIFEST_HASH'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_unverified_source_object_prevents_read(self):
        k,i,p=self.committed();self.cloud.db.execute('update objects set verified=0 where digest=?',(i['digest'],));self.cloud.db.commit()
        with self.assertRaisesRegex(ValueError,'BLOB_UNVERIFIED'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_unverified_portable_object_prevents_read(self):
        k,i,p=self.committed();self.cloud.db.execute('update objects set verified=0 where digest!=?',(i['digest'],));self.cloud.db.commit()
        with self.assertRaisesRegex(ValueError,'REFERENCE_UNVERIFIED'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_wrong_restored_bytes_prevents_read(self):
        k,i,p=self.committed();self.cloud.data[i['digest']]=b'corrupt!'
        with self.assertRaisesRegex(ValueError,'RESTORE_HASH'):self.p.read(k,i['absolute'],i['size'],i['digest'],p)
    def test_changed_work_never_retired(self):
        k,i,p=self.committed();Path(i['working_path']).write_bytes(b'changed!')
        with self.assertRaisesRegex(ValueError,'RETIRE_CHANGED'):self.p.retire_new(k,i,p)
        self.assertTrue(Path(i['working_path']).exists())
    def test_legacy_partial_preserved(self):
        raw=b'legacy';k=digest(f'8:{len(raw)}'.encode());path=self.root/'ranges'/(k+'.raw');path.write_bytes(raw)
        self.assertEqual(self.p.stage(k,raw,path),(path,None));self.assertEqual(path.read_bytes(),raw)
    def test_legacy_partial_conflict_preserved(self):
        path=self.root/'ranges'/'old.raw';path.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError,'PARTIAL_CHANGED'):self.p.stage('a'*64,b'new',path)
        self.assertEqual(path.read_bytes(),b'old')
    def test_memory_cache_bound(self):
        with patch('economic_machine.arbitrum_research.freezer_range_store.CACHE_BYTES',4):
            self.p.remember('a',b'123');self.p.remember('b',b'45')
            self.assertEqual(self.p.cache_bytes,2);self.assertEqual(list(self.p.cache),['b'])
    def test_legacy_snapshot_reads_portable_even_feature_off(self):
        k,i,p=self.committed();self.p.retire_new(k,i,p)
        s=Snapshot.__new__(Snapshot);s.out=self.root;s.db=self.db;s.cloud=self.cloud;s.parts=[dict(offset=0,bytes=100,url='example')]
        s.guard=lambda n=0:None;s.last_refs=[]
        with patch.dict(os.environ,{'EM_FREEZER_SOURCE_CLOUD_FIRST':'0'}):
            self.assertEqual(s.read(8,8),b'evidence')
    def test_setting_strict(self):
        for value in ['','yes','2']:
            with patch.dict(os.environ,{'EM_FREEZER_SOURCE_CLOUD_FIRST':value}):
                with self.assertRaises(ValueError):enabled()

class PortablePipelineTests(HistoricalScopeTestCase, PipelinedSourceTests):
    def setUp(self):
        super().setUp()
        self.s.db.execute('create table identity(body)')
        self.s.db.execute("insert into identity values('original')");self.s.db.commit()
        self.c=Cloud();self.s.cloud=self.c;self.s.cloud_first=True
        self.s.portable=PortableRanges(self.s.out,self.s.db,self.c)
    def tearDown(self):self.c.db.close();super().tearDown()
    def finish(self,index):
        future,path=self.s.uploads.jobs[index];future.set_result(self.c.upload(path))
    # Inherited tests explicitly cover legacy lifetimes, use their original mode.
    def test_blob_failure_preserves_reservation_and_raw(self):
        self.s.cloud_first=False;super().test_blob_failure_preserves_reservation_and_raw()
    def test_unverified_range_never_commits(self):
        self.s.cloud_first=False;super().test_unverified_range_never_commits()
    def test_duplicate_pending_read_does_not_reserve_twice(self):
        self.s.cloud_first=False;super().test_duplicate_pending_read_does_not_reserve_twice()
    def test_two_pending_limit_commits_oldest_before_third(self):
        self.s.cloud_first=False;super().test_two_pending_limit_commits_oldest_before_third()
    def test_pipeline_cloud_first_commit_and_rollback_read(self):
        data=self.s.read(8,32);self.finish(0);self.s.drain()
        self.assertEqual(list((self.s.out/'ranges').iterdir()),[])
        self.assertEqual(list((self.s.out/'working-ranges').iterdir()),[])
        self.s.cloud_first=False
        self.assertEqual(self.s.read(8,32),data)
        self.assertEqual(len(self.s.uploads.jobs),1)
    def test_pipeline_failed_blob_retains_only_new_working_copy(self):
        self.s.read(8,32);self.s.uploads.finish(0,error=ValueError('readback failed'))
        with self.assertRaises(ValueError):self.s.drain()
        self.assertEqual(len(list((self.s.out/'working-ranges').iterdir())),1)
        self.assertEqual(self.s.db.execute('select count(*) from ranges').fetchone()[0],0)
