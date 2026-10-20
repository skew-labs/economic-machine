import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from economic_machine.arbitrum_research.freezer_prepared import read_prepared
from economic_machine.arbitrum_research.freezer_source import DEADLINE
from economic_machine.pump_research.store import canonical

class PreparedTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.folder=self.root/'page';self.folder.mkdir()
  self.db=sqlite3.connect(self.root/'source.sqlite3');self.db.executescript('CREATE TABLE identity(body); CREATE TABLE attempts(bytes); CREATE TABLE ranges(id,start,bytes,sha256,proof); CREATE TABLE prepared_pages(id,body);')
  self.spend=sqlite3.connect(':memory:');self.spend.execute('CREATE TABLE objects(digest,bytes,verified)')
  self.cloud=SimpleNamespace(spend=SimpleNamespace(db=self.spend));self.blob=dict(sha256='f'*64,bytes=8)
  self.ref=dict(offset=20,bytes=8,sha256='f'*64,cloud=self.blob);key=hashlib.sha256(b'20:8').hexdigest()
  self.db.execute('insert into identity values(?)',(canonical(dict(source={'epoch':'snapshot'},deadline_ms=DEADLINE,source_limit=1024**3)),))
  self.db.execute('insert into attempts values(8)');self.db.execute('insert into ranges values(?,?,?,?,?)',(key,20,8,'f'*64,canonical(self.blob)));self.spend.execute('insert into objects values(?,?,1)',('f'*64,8))
  self.raw=(canonical(dict(chain=42161,blocks=[dict(number=5)]))+'\n').encode();(self.folder/'candidate-records.json').write_bytes(self.raw)
  self.ready=dict(job_id='job',chain=42161,start=5,count=1,binary_sha256='binary',deadline_ms=DEADLINE,folder=str(self.folder),source_identity={'epoch':'snapshot'},source_ranges=[self.ref],raw_bytes=len(self.raw),raw_sha256=hashlib.sha256(self.raw).hexdigest())
  self.commit()
 def commit(self):
  self.db.execute('delete from prepared_pages');self.db.execute('insert into prepared_pages values(?,?)',('job',canonical(self.ready)));self.db.commit();(self.folder/'acquired.json').write_text(canonical(self.ready))
 def read(self):return read_prepared(self.folder,self.root,self.cloud,chain=42161,start=5,count=1,binary_sha256='binary')
 def tearDown(self):self.db.close();self.spend.close();self.tmp.cleanup()
 def test_valid(self):self.assertEqual(self.read()[0],self.raw)
 def test_uncommitted(self):
  self.db.execute('delete from prepared_pages');self.db.commit()
  with self.assertRaisesRegex(ValueError,'UNCOMMITTED'):self.read()
 def test_scope(self):
  self.ready['start']=6;self.commit()
  with self.assertRaisesRegex(ValueError,'SCOPE_CHANGED'):self.read()
 def test_raw_tampering(self):
  (self.folder/'candidate-records.json').write_bytes(self.raw+b' ')
  with self.assertRaisesRegex(ValueError,'RAW_CHANGED'):self.read()
 def test_range_changed(self):
  self.db.execute("update ranges set sha256='changed'");self.db.commit()
  with self.assertRaisesRegex(ValueError,'RANGE_PROOF'):self.read()
 def test_unverified_blob(self):
  self.spend.execute('update objects set verified=0')
  with self.assertRaisesRegex(ValueError,'BLOB_NOT_VERIFIED'):self.read()
 def test_cost_cap(self):
  self.db.execute('insert into attempts values(?)',(1024**3,));self.db.commit()
  with self.assertRaisesRegex(ValueError,'SOURCE_LIMIT'):self.read()
 def test_wrong_payload_scope_even_with_matching_hash(self):
  raw=b'{"chain":4663,"blocks":[{"number":5}]}';(self.folder/'candidate-records.json').write_bytes(raw);self.ready.update(raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest());self.commit()
  with self.assertRaisesRegex(ValueError,'RECORD_SCOPE'):self.read()
 def test_source_identity_changed(self):
  self.ready['source_identity']={'epoch':'other'};self.commit()
  with self.assertRaisesRegex(ValueError,'SOURCE_IDENTITY'):self.read()
 def authorize(self):
  body=self.db.execute('select body from identity').fetchone()[0]
  value=dict(version=1,scope='arbitrum_freezer_source_attempt_bytes',source_cache=str(self.root),source_identity_sha256=hashlib.sha256(body.encode()).hexdigest(),original_limit_bytes=1024**3,limit_bytes=None,deadline_ms=DEADLINE,monthly_limit_krw=1000000,ledger_reset=False,authorized_at_ms=DEADLINE-100000)
  path=self.root/'owner-source-amendment-test.json';raw=canonical(value).encode();path.write_bytes(raw)
  ref=dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest());self.ready['source_cap_authorization']=ref;self.commit();return ref
 def test_owner_amendment_preserves_original_identity_above_old_cap(self):
  original=self.db.execute('select body from identity').fetchone()[0];ref=self.authorize();self.db.execute('insert into attempts values(?)',(1024**3,));self.db.commit()
  raw,_=read_prepared(self.folder,self.root,self.cloud,chain=42161,start=5,count=1,binary_sha256='binary',source_authorization=ref)
  self.assertEqual(raw,self.raw);self.assertEqual(self.db.execute('select body from identity').fetchone()[0],original)
 def test_amendment_requires_explicit_consumer_authority(self):
  self.authorize()
  with self.assertRaisesRegex(ValueError,'AUTHORIZATION'):self.read()
 def test_changed_amendment_rejected(self):
  ref=self.authorize();Path(ref['path']).write_bytes(b'{}')
  with self.assertRaisesRegex(ValueError,'AUTHORITY_CHANGED'):read_prepared(self.folder,self.root,self.cloud,chain=42161,start=5,count=1,binary_sha256='binary',source_authorization=ref)
 def test_amendment_cannot_extend_deadline(self):
  ref=self.authorize();path=Path(ref['path']);value=json.loads(path.read_bytes());value['deadline_ms']+=1;raw=canonical(value).encode();path.write_bytes(raw);ref['sha256']=hashlib.sha256(raw).hexdigest();self.ready['source_cap_authorization']=ref;self.commit()
  with self.assertRaisesRegex(ValueError,'AUTHORITY_SCOPE'):read_prepared(self.folder,self.root,self.cloud,chain=42161,start=5,count=1,binary_sha256='binary',source_authorization=ref)
 def test_amendment_cannot_increase_monthly_budget(self):
  ref=self.authorize();path=Path(ref['path']);value=json.loads(path.read_bytes());value['monthly_limit_krw']+=1;raw=canonical(value).encode();path.write_bytes(raw);ref['sha256']=hashlib.sha256(raw).hexdigest();self.ready['source_cap_authorization']=ref;self.commit()
  with self.assertRaisesRegex(ValueError,'AUTHORITY_SCOPE'):read_prepared(self.folder,self.root,self.cloud,chain=42161,start=5,count=1,binary_sha256='binary',source_authorization=ref)

if __name__=='__main__':unittest.main()
