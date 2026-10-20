"""Future-only source working copies backed by immutable verified Blob bytes.

Legacy ranges are never retired. The writer owns the SQLite connection. Only
new unique working files may be removed, after the range and portable reference
commit together. Failed/ambiguous working files remain for audit.
"""
from collections import OrderedDict
import hashlib,json,os,re,stat,time,uuid
from pathlib import Path
from economic_machine.pump_research.research_cloud import identity
from economic_machine.pump_research.store import atomic,canonical
DEADLINE=1792351694298
MAX_RANGE=8*1024**2
CACHE_BYTES=64*1024**2

def digest(raw):return hashlib.sha256(raw).hexdigest()
def hash_ok(value):return type(value) is str and re.fullmatch("[0-9a-f]{64}",value) is not None
def enabled():
    value=os.environ.get("EM_FREEZER_SOURCE_CLOUD_FIRST","0")
    if value not in ("0","1"):raise ValueError("RANGE_CLOUD_FIRST_SETTING")
    return value=="1"

class PortableRanges:
    def __init__(self,root,db,cloud):
        self.root=Path(root).resolve();self.db=db;self.cloud=cloud
        self.work=self.root/"working-ranges";self.manifests=self.root/"portable-ranges"
        for p in (self.work,self.manifests):
            if p.is_symlink():raise ValueError("PORTABLE_RANGE_DIRECTORY")
            p.mkdir(exist_ok=True)
        self.db.execute("create table if not exists portable_ranges(id text primary key,body text not null)")
        self.db.commit()
        self.ledger_identity=self.db.execute("select body from identity").fetchone()[0]
        self.identity_hash=digest(self.ledger_identity.encode())
        self.cache=OrderedDict();self.cache_bytes=0
        self.metrics=dict(new_working_ranges=0,retired_working_ranges=0,retired_working_bytes=0,restores=0,memory_hits=0)
    def guard(self,extra=0):
        if time.time_ns()//1000000>=DEADLINE:raise ValueError("PORTABLE_RANGE_DEADLINE")
        if self.db.execute("select body from identity").fetchall()!=[(self.ledger_identity,)]:
            raise ValueError("PORTABLE_RANGE_SOURCE_IDENTITY")
        self.cloud.guard_local(extra)
    def verified(self,proof,size,sha):
        if proof!={"sha256":sha,"bytes":size} or not hash_ok(sha) or type(size) is not int or not 0<size<=MAX_RANGE:
            raise ValueError("PORTABLE_RANGE_BLOB_PROOF")
        if self.cloud.spend.db.execute("select bytes,verified from objects where digest=?",(sha,)).fetchone()!=(size,1):
            raise ValueError("PORTABLE_RANGE_BLOB_UNVERIFIED")
    def stage(self,key,raw,legacy):
        self.guard(len(raw)*2)
        if not hash_ok(key) or type(raw) is not bytes or not 0<len(raw)<=MAX_RANGE:raise ValueError("PORTABLE_RANGE_STAGE_BOUND")
        legacy=Path(legacy)
        if legacy.exists() or legacy.is_symlink():
            # A retained old partial is original evidence. Keep its old path
            # and original lifetime; it cannot become a future-only disposable.
            if legacy.is_symlink() or legacy.read_bytes()!=raw:raise ValueError("FREEZER_PARTIAL_CHANGED")
            return legacy,None
        path=self.work/(key+"-"+uuid.uuid4().hex+".raw")
        with path.open("xb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
        fd=os.open(self.work,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        self.metrics["new_working_ranges"]+=1
        return path,identity(path)
    def prepare(self,key,item,proof):
        self.guard()
        self.verified(proof,item["size"],item["digest"])
        path=Path(item["working_path"])
        if path.parent!=self.work or identity(path)!=item["working_identity"]:
            raise ValueError("PORTABLE_RANGE_WORK_CHANGED")
        raw=path.read_bytes()
        if len(raw)!=item["size"] or digest(raw)!=item["digest"]:raise ValueError("PORTABLE_RANGE_WORK_HASH")
        body=dict(schema="economic-machine.portable-freezer-range.v1",id=key,offset=item["absolute"],
                  bytes=item["size"],sha256=item["digest"],cloud=proof,deadline_ms=DEADLINE,
                  source_ledger_identity_sha256=self.identity_hash,budget_sha256=self.cloud.spend.hash,
                  complete_cloud_readback=True,source_preserved_in_cloud=True,
                  independent_consensus_verified=False)
        if key!=digest(f'{body["offset"]}:{body["bytes"]}'.encode()):raise ValueError("PORTABLE_RANGE_KEY")
        manifest=self.manifests/(key+".json")
        if manifest.exists():
            if manifest.is_symlink() or manifest.stat().st_size>65536 or json.loads(manifest.read_bytes())!=body:raise ValueError("PORTABLE_RANGE_MANIFEST_CHANGED")
        else:atomic(manifest,body)
        packed=manifest.read_bytes();manifest_hash=digest(packed)
        reference=self.cloud.upload(manifest,manifest_hash)
        if reference!={"sha256":manifest_hash,"bytes":len(packed)}:raise ValueError("PORTABLE_RANGE_REFERENCE_PROOF")
        if self.cloud.spend.db.execute("select bytes,verified from objects where digest=?",(manifest_hash,)).fetchone()!=(len(packed),1):
            raise ValueError("PORTABLE_RANGE_REFERENCE_UNVERIFIED")
        return dict(manifest=manifest.name,manifest_sha256=manifest_hash,reference=reference,range=body)
    def commit(self,key,item,proof,record):
        self.guard()
        self.verified(proof,item["size"],item["digest"])
        if identity(Path(item["working_path"]))!=item["working_identity"]:raise ValueError("PORTABLE_RANGE_WORK_CHANGED")
        expected=(item["absolute"],item["size"],item["digest"],canonical(proof))
        encoded=canonical(record)
        with self.db:
            prior=self.db.execute("select start,bytes,sha256,proof from ranges where id=?",(key,)).fetchone()
            if prior and prior!=expected:raise ValueError("PORTABLE_RANGE_LEDGER_CHANGED")
            old=self.db.execute("select body from portable_ranges where id=?",(key,)).fetchone()
            if old and old[0]!=encoded:raise ValueError("PORTABLE_RANGE_REFERENCE_CHANGED")
            if prior and not old:raise ValueError("PORTABLE_RANGE_LEGACY_COLLISION")
            self.db.execute("insert or ignore into ranges values(?,?,?,?,?)",(key,*expected))
            self.db.execute("insert or ignore into portable_ranges values(?,?)",(key,encoded))
            self.db.executemany("update attempts set state='received' where id=?",[(i,) for i in item["ids"]])
    def metadata(self,key,offset,size,sha,proof):
        self.guard()
        self.verified(proof,size,sha)
        row=self.db.execute("select start,bytes,sha256,proof from ranges where id=?",(key,)).fetchone()
        if row!=(offset,size,sha,canonical(proof)):raise ValueError("PORTABLE_RANGE_LEDGER_CHANGED")
        got=self.db.execute("select body from portable_ranges where id=?",(key,)).fetchone()
        if not got:raise ValueError("PORTABLE_RANGE_REFERENCE_MISSING")
        if len(got[0])>65536:raise ValueError("PORTABLE_RANGE_REFERENCE_BOUND")
        record=json.loads(got[0]);name=record.get("manifest","")
        if name!=key+".json" or not hash_ok(record.get("manifest_sha256")):raise ValueError("PORTABLE_RANGE_MANIFEST_IDENTITY")
        path=self.manifests/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:raise ValueError("PORTABLE_RANGE_MANIFEST_BOUND")
        raw=path.read_bytes()
        if digest(raw)!=record["manifest_sha256"] or json.loads(raw)!=record["range"]:raise ValueError("PORTABLE_RANGE_MANIFEST_HASH")
        r=record["range"]
        expected=dict(schema="economic-machine.portable-freezer-range.v1",id=key,offset=offset,bytes=size,
                      sha256=sha,cloud=proof,deadline_ms=DEADLINE,source_ledger_identity_sha256=self.identity_hash,
                      budget_sha256=self.cloud.spend.hash,complete_cloud_readback=True,
                      source_preserved_in_cloud=True,independent_consensus_verified=False)
        if r!=expected or key!=digest(f"{offset}:{size}".encode()):raise ValueError("PORTABLE_RANGE_SCOPE")
        reference=record["reference"]
        if reference!={"sha256":record["manifest_sha256"],"bytes":len(raw)}:raise ValueError("PORTABLE_RANGE_REFERENCE_PROOF")
        if self.cloud.spend.db.execute("select bytes,verified from objects where digest=?",(reference["sha256"],)).fetchone()!=(reference["bytes"],1):
            raise ValueError("PORTABLE_RANGE_REFERENCE_UNVERIFIED")
        return record
    def remember(self,sha,raw):
        old=self.cache.pop(sha,None)
        if old is not None:self.cache_bytes-=len(old)
        self.cache[sha]=raw;self.cache_bytes+=len(raw)
        while self.cache_bytes>CACHE_BYTES:
            _,old=self.cache.popitem(last=False);self.cache_bytes-=len(old)
    def retire_new(self,key,item,proof):
        self.metadata(key,item["absolute"],item["size"],item["digest"],proof)
        path=Path(item["working_path"])
        if path.parent!=self.work or identity(path)!=item["working_identity"]:raise ValueError("PORTABLE_RANGE_RETIRE_CHANGED")
        raw=path.read_bytes()
        if len(raw)!=item["size"] or digest(raw)!=item["digest"]:raise ValueError("PORTABLE_RANGE_RETIRE_HASH")
        # A complete verified reference and the atomic range ledger already
        # exist. No legacy ranges path or failed/ambiguous file is touched.
        if identity(path)!=item["working_identity"]:raise ValueError("PORTABLE_RANGE_RETIRE_CHANGED")
        path.unlink()
        self.remember(item["digest"],raw)
        self.metrics["retired_working_ranges"]+=1;self.metrics["retired_working_bytes"]+=len(raw)
    def read(self,key,offset,size,sha,proof):
        self.metadata(key,offset,size,sha,proof)
        if sha in self.cache:
            raw=self.cache.pop(sha);self.cache[sha]=raw;self.metrics["memory_hits"]+=1
        else:
            with self.cloud.cached(sha,size) as stream:raw=stream.read(size+1)
            if len(raw)!=size or digest(raw)!=sha:raise ValueError("PORTABLE_RANGE_RESTORE_HASH")
            self.remember(sha,raw);self.metrics["restores"]+=1
        return raw
