import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from economic_machine.arbitrum_research.freezer_cloud_index import load_manifest, parity, resolve, restore, shard_lease, retire_derived, derived_expectations


class FakeStorage:
    def __init__(self, raw):
        self.raw=raw;self.cloud=self
    def guard_local(self,n):pass
    def read(self,proof):return self.raw
    def close(self):pass


class CloudIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.raw=b'source';self.digest=hashlib.sha256(self.raw).hexdigest()
        self.manifest=dict(schema='economic-machine.freezer-cloud-index.v1',chain_id=42161,bytes=len(self.raw),index_sha256=self.digest,
                           parts=[dict(offset=0,proof=dict(raw_bytes=len(self.raw)))])

    def write_manifest(self):
        p=self.base/'manifest.json';p.write_text(json.dumps(self.manifest));return p,hashlib.sha256(p.read_bytes()).hexdigest()

    def test_manifest_scope_and_hash(self):
        p,digest=self.write_manifest();self.assertEqual(load_manifest(p,digest,self.digest),self.manifest)
        with self.assertRaisesRegex(ValueError,'MANIFEST_HASH'):load_manifest(p,'a'*64,self.digest)
        with self.assertRaisesRegex(ValueError,'MANIFEST_SCOPE'):load_manifest(p,digest,'b'*64)

    def test_manifest_gap_rejected(self):
        self.manifest['parts'][0]['offset']=1;p,digest=self.write_manifest()
        with self.assertRaisesRegex(ValueError,'MANIFEST_PARTS'):load_manifest(p,digest,self.digest)

    def test_restore_entire_hash_required(self):
        p=self.base/'restored';restore(FakeStorage(self.raw),self.manifest,p);self.assertEqual(p.read_bytes(),self.raw)
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'EXISTING_CHANGED'):restore(FakeStorage(self.raw),self.manifest,p)

    def test_failed_restore_preserves_partial(self):
        p=self.base/'restored'
        with self.assertRaisesRegex(ValueError,'RESTORE_HASH'):restore(FakeStorage(b'wrong'),self.manifest,p)
        self.assertFalse(p.exists());self.assertTrue(p.with_name('restored.partial').exists())

    def test_existing_partial_not_overwritten(self):
        p=self.base/'restored';p.with_name('restored.partial').write_bytes(b'previous')
        restore(FakeStorage(self.raw),self.manifest,p)
        self.assertEqual(p.with_name('restored.partial').read_bytes(),b'previous')

    def test_old_local_registry_unchanged(self):
        row=dict(path=str(self.base/'local'),body='{}')
        with resolve(self.base,row) as p:self.assertEqual(p,Path(row['path']))

    def test_cloud_route_used_with_original_preserved(self):
        p,digest=self.write_manifest();(self.base/'evidence/freezer-search-v1').mkdir(parents=True)
        original=self.base/'local';original.write_bytes(self.raw)
        row=dict(path=str(original),id=self.digest,chain=42161,body=json.dumps(dict(prefer_cloud_index=True,cloud_index=dict(path=str(p),sha256=digest))))
        with patch('economic_machine.arbitrum_research.freezer_cloud_index.Storage',return_value=FakeStorage(self.raw)):
            with resolve(self.base,row) as restored:
                self.assertNotEqual(restored,original);self.assertEqual(restored.read_bytes(),self.raw)
        self.assertTrue(original.exists())

    def test_chain_mismatch_fail_closed(self):
        p,digest=self.write_manifest();row=dict(path=str(self.base/'x'),id=self.digest,chain=4663,body=json.dumps(dict(prefer_cloud_index=True,cloud_index=dict(path=str(p),sha256=digest))))
        with self.assertRaisesRegex(ValueError,'CHAIN'):
            with resolve(self.base,row):pass

    def test_retirement_waits_for_live_reader(self):
        path=self.base/'index.sqlite3';path.write_bytes(self.raw);entered=threading.Event();finished=threading.Event()
        def retirement():
            entered.set()
            with shard_lease(path,exclusive=True):path.unlink();finished.set()
        with shard_lease(path):
            worker=threading.Thread(target=retirement);worker.start();self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(.05));self.assertTrue(path.exists())
        worker.join(timeout=1);self.assertTrue(finished.is_set());self.assertFalse(path.exists())

    def test_stale_local_row_refreshes_to_published_cloud_route(self):
        p,digest=self.write_manifest();root=self.base/'evidence/freezer-search-v1';root.mkdir(parents=True)
        original=self.base/'local';old=dict(path=str(original),id=self.digest,chain=42161,body='{}')
        latest=dict(old,body=json.dumps(dict(prefer_cloud_index=True,cloud_index=dict(path=str(p),sha256=digest))))
        db=sqlite3.connect(root/'catalogue.sqlite3');db.execute('create table shards(path text,id text,chain integer,body text)');db.execute('insert into shards values(?,?,?,?)',tuple(latest[k] for k in ('path','id','chain','body')));db.commit();db.close()
        with patch('economic_machine.arbitrum_research.freezer_cloud_index.Storage',return_value=FakeStorage(self.raw)):
            with resolve(self.base,old) as restored:self.assertEqual(restored.read_bytes(),self.raw)

    def test_query_parity_detects_different_results(self):
        paths=[self.base/'a.sqlite3',self.base/'b.sqlite3']
        for i,p in enumerate(paths):
            db=sqlite3.connect(p)
            db.executescript("create table addresses(address text);insert into addresses values('a');create table edges(id integer,block integer,sender text,recipient text);create table interactions(id integer,block integer,sender text,recipient text);")
            db.execute("insert into edges values(?,?,?,?)",(1,i,'a','b'));db.commit();db.close()
        with self.assertRaisesRegex(ValueError,'QUERY_PARITY'):parity(*paths)


class DerivedProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.folder=self.base/'evidence/freezer-continuous-v1/batch-1-2/window-1-2';self.folder.mkdir(parents=True)
        binary=self.base/'bin/freezer-decode';binary.parent.mkdir();binary.write_bytes(b'pinned binary')
        self.binary_sha=hashlib.sha256(binary.read_bytes()).hexdigest();self.raw=b'raw input';self.decoded=b'complete output'
        proof=dict(raw_sha256=hashlib.sha256(self.raw).hexdigest(),raw_bytes=len(self.raw),complete_cloud_readback=True,
                   source=dict(binary_sha256=self.binary_sha))
        self.report=dict(blocks=2,graph=dict(chain_id=42161),source_proof=proof,
                         derived_reconstruction=dict(schema='economic-machine.freezer-derived-provenance.v1',binary_sha256=self.binary_sha,
                         native_input_sha256=proof['raw_sha256'],decoded_sha256=hashlib.sha256(self.decoded).hexdigest(),decoded_bytes=len(self.decoded),
                         native_root_checks=True,blocks=2,chain_id=42161))
        (self.folder/'candidate-records.json').write_bytes(self.raw);(self.folder/'candidate-decoded.json').write_bytes(self.decoded)
        (self.folder/'cloud-index-verification.json').write_text(json.dumps(dict(original_retired=True)))
        registry=self.base/'evidence/freezer-search-v1';registry.mkdir();self.registry=registry/'catalogue.sqlite3'
        db=sqlite3.connect(self.registry);db.execute('create table shards(path text,body text)');db.close();self.bind()

    def bind(self):
        p=self.folder/'verification.json';p.write_text(json.dumps(self.report))
        db=sqlite3.connect(self.registry);db.execute('delete from shards');db.execute('insert into shards values(?,?)',
            (str(self.folder/'index.sqlite3'),json.dumps(dict(report_sha256=hashlib.sha256(p.read_bytes()).hexdigest()))));db.commit();db.close()

    def fake(self):return patch('economic_machine.arbitrum_research.freezer_cloud_index.Storage',return_value=FakeStorage(self.raw))

    def assert_retained(self):
        self.assertTrue((self.folder/'candidate-records.json').exists());self.assertTrue((self.folder/'candidate-decoded.json').exists())

    def test_new_proof_uses_original_roots_full_input_and_whole_output_hash(self):
        with self.fake(),patch('economic_machine.arbitrum_research.freezer_cloud_index.subprocess.run',side_effect=AssertionError('duplicate native execution')):
            result=retire_derived(self.base,self.folder)
        self.assertTrue(result['retired']);self.assertFalse(result['native_root_replay']);self.assertTrue(result['native_root_verified_in_original_extraction'])
        self.assertFalse((self.folder/'candidate-decoded.json').exists())

    def test_report_mutation_blocks_retirement(self):
        (self.folder/'verification.json').write_bytes(b'changed')
        with self.fake(),self.assertRaisesRegex(ValueError,'REPORT_CHANGED'):retire_derived(self.base,self.folder)
        self.assert_retained()

    def test_other_binary_blocks_retirement(self):
        (self.base/'bin/freezer-decode').write_bytes(b'different')
        with self.fake(),self.assertRaisesRegex(ValueError,'BINARY_CHANGED'):retire_derived(self.base,self.folder)
        self.assert_retained()

    def test_partial_output_blocks_retirement(self):
        (self.folder/'candidate-decoded.json').write_bytes(b'partial')
        with self.fake(),self.assertRaisesRegex(ValueError,'PARITY'):retire_derived(self.base,self.folder)
        self.assert_retained()

    def test_missing_blob_blocks_retirement(self):
        storage=FakeStorage(self.raw)
        storage.read=lambda proof:(_ for _ in ()).throw(ValueError('BLOB_MISSING'))
        with patch('economic_machine.arbitrum_research.freezer_cloud_index.Storage',return_value=storage),self.assertRaisesRegex(ValueError,'BLOB_MISSING'):
            retire_derived(self.base,self.folder)
        self.assert_retained()

    def test_changed_provenance_root_input_and_output_fail_closed(self):
        for key,value in [('native_root_checks',False),('native_input_sha256','a'*64),('decoded_sha256','b'*64),('decoded_bytes',1)]:
            original=self.report['derived_reconstruction'][key];self.report['derived_reconstruction'][key]=value;self.bind()
            with self.fake(),self.assertRaises(ValueError):retire_derived(self.base,self.folder)
            self.assert_retained();self.report['derived_reconstruction'][key]=original

    def test_reader_lease_defers_derived_retirement(self):
        entered=threading.Event();done=threading.Event();errors=[]
        def retire_worker():
            entered.set()
            try:retire_derived(self.base,self.folder)
            except BaseException as e:errors.append(e)
            finally:done.set()
        with self.fake():
            with shard_lease(self.folder/'index.sqlite3'):
                worker=threading.Thread(target=retire_worker);worker.start();self.assertTrue(entered.wait(1));self.assertFalse(done.wait(.05));self.assert_retained()
            worker.join(1);self.assertTrue(done.is_set());self.assertEqual(errors,[])

    def test_legacy_report_requires_replay(self):
        del self.report['derived_reconstruction']
        self.assertIsNone(derived_expectations(self.report,self.binary_sha,self.raw))


if __name__=='__main__':unittest.main()
