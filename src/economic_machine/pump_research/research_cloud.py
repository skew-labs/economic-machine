"""Content-addressed private research objects with full native readback.

Only the research namespace is writable. Cloud objects are never deleted.
The cache contains replaceable, hash-checked copies, not source evidence.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid
from urllib.error import URLError

from .research_spend import Spend, FOLDER, authority
from .store import atomic

BLOCK=8*1024**2
CACHE_BYTES=512*1024**2
FILES={'linked-log','manifest','pubkey-to-offset-and-size.index'}

def identity(path):
    s=Path(path).lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise ValueError('RESEARCH_CLOUD_LOCAL_TYPE')
    return dict(bytes=s.st_size,device=s.st_dev,inode=s.st_ino,mtime_ns=s.st_mtime_ns,ctime_ns=s.st_ctime_ns)

def digest_file(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def document(path,maximum=16*1024**2):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>maximum:raise ValueError('RESEARCH_CLOUD_DOCUMENT')
    value=json.loads(path.read_bytes())
    if not isinstance(value,dict):raise ValueError('RESEARCH_CLOUD_DOCUMENT')
    return value

class Cloud:
    def __init__(self,base):
        self.base=Path(base);self.out=self.base/FOLDER;self.spend=Spend(base)
        c=document(self.out/'cloud-config.json',8192);g=self.spend.grant
        if (c.get('account')!=g['account'] or c.get('container')!=g['container']
            or c.get('objectPrefix')!=g['object_prefix'] or c.get('auth')!='bearer_file'
            or c.get('bearerFile')!='/run/ticking-blob/bearer' or c.get('maxBytesPerSecond')!=32*1024**2):
            raise ValueError('RESEARCH_CLOUD_CONFIG')
        self.config=self.out/'cloud-config.json';self.binary=self.base/'build/research-blob-v1/blob_archive'
        pin=(self.out/'binary.sha256').read_text().strip()
        if self.binary.is_symlink() or digest_file(self.binary)!=pin:raise ValueError('RESEARCH_CLOUD_BINARY')
        self.cache=self.out/'cache';self.cache.mkdir(exist_ok=True)
        (self.out/'receipts').mkdir(exist_ok=True)
        self.last_storage_check=0;self.local_bytes=0

    def close(self):self.spend.close()

    def guard_local(self,extra):
        from .history_core import used_bytes
        if time.monotonic()-self.last_storage_check>2:
            self.local_bytes=used_bytes(self.base);self.last_storage_check=time.monotonic()
        if self.local_bytes+extra+128*1024**2>self.spend.grant['local_storage_bytes']:
            raise ValueError('RESEARCH_CLOUD_LOCAL_STORAGE_LIMIT')
        if shutil.disk_usage(self.base).free-extra<self.spend.grant['minimum_host_free_bytes']:
            raise ValueError('RESEARCH_CLOUD_LOCAL_FREE_SPACE')

    def upload(self,path,expected=None):
        self.guard_local(1024**2)
        path=Path(path);before=identity(path);size=before['bytes']
        if not 0<size<=64*1024**2:raise ValueError('RESEARCH_CLOUD_OBJECT_BOUND')
        digest=digest_file(path)
        if expected is not None and digest!=expected:raise ValueError('RESEARCH_CLOUD_SOURCE_HASH')
        row=self.spend.db.execute('SELECT bytes,verified FROM objects WHERE digest=?',(digest,)).fetchone()
        if row and row==(size,1):return dict(sha256=digest,bytes=size)
        attempt=uuid.uuid4().hex;self.spend.reserve('upload',attempt,bytes_=size,digest=digest)
        receipt=self.out/'receipts'/f'{attempt}.json'
        run=subprocess.run([str(self.binary),str(self.config),str(path),str(receipt)],capture_output=True,timeout=150)
        if run.returncode:raise URLError('RESEARCH_CLOUD_UPLOAD_FAILED')
        proof=json.loads(run.stdout)
        if (proof.get('sha256')!=digest or proof.get('bytes')!=size or proof.get('fullDownloadVerified') is not True
            or proof.get('account')!=self.spend.grant['account'] or proof.get('container')!=self.spend.grant['container']
            or proof.get('object')!=self.spend.grant['object_prefix']+'/'+digest[:2]+'/'+digest
            or identity(path)!=before):raise ValueError('RESEARCH_CLOUD_READBACK_MISMATCH')
        self.spend.verified(digest);self.spend.publish()
        return dict(sha256=digest,bytes=size)

    def cached(self,digest,size):
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or type(size) is not int or not 0<size<=64*1024**2:
            raise ValueError('RESEARCH_CLOUD_REFERENCE')
        row=self.spend.db.execute('SELECT bytes,verified FROM objects WHERE digest=?',(digest,)).fetchone()
        if row!=(size,1):raise ValueError('RESEARCH_CLOUD_UNVERIFIED_OBJECT')
        with (self.cache/'cache.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            target=self.cache/digest
            if target.exists():
                if identity(target)['bytes']!=size or digest_file(target)!=digest:raise ValueError('RESEARCH_CLOUD_CACHE_HASH')
            else:
                self.guard_local(size*2)
                entries=[]
                for p in self.cache.iterdir():
                    if re.fullmatch('[0-9a-f]{64}',p.name):entries.append((p.stat().st_mtime_ns,p,identity(p)['bytes']))
                total=sum(r[2] for r in entries)
                for _,p,n in sorted(entries):
                    if total+size<=CACHE_BYTES:break
                    p.unlink();total-=n  # Verified remote originals remain immutable.
                if total+size>CACHE_BYTES or shutil.disk_usage(self.base).free-size<self.spend.grant['minimum_host_free_bytes']:
                    raise ValueError('RESEARCH_CLOUD_CACHE_CAPACITY')
                self.spend.reserve('read',uuid.uuid4().hex,bytes_=size,digest=digest)
                run=subprocess.run([str(self.binary),'--restore',str(self.config),digest,str(target),str(size)],capture_output=True,timeout=90)
                if run.returncode:raise URLError('RESEARCH_CLOUD_RESTORE_FAILED')
                proof=json.loads(run.stdout)
                if proof.get('sha256')!=digest or proof.get('bytes')!=size or proof.get('fullDownloadVerified') is not True:
                    raise ValueError('RESEARCH_CLOUD_RESTORE_RECEIPT')
                if digest_file(target)!=digest:raise ValueError('RESEARCH_CLOUD_RESTORE_HASH')
                self.spend.publish()
            # Open while still locked: later eviction does not revoke this FD.
            return os.fdopen(os.open(target,os.O_RDONLY|os.O_NOFOLLOW),'rb')

def load_reference(path,expected=None):
    value=document(path)
    if value.get('schema')!='economic-machine.research-cloud-file.v1' or not isinstance(value.get('blocks'),list):
        raise ValueError('RESEARCH_CLOUD_FILE_SCHEMA')
    if (value.get('complete_cloud_readback') is not True or value.get('source_preserved_in_cloud') is not True
        or not isinstance(value.get('original_local_identity'),dict)):
        raise ValueError('RESEARCH_CLOUD_FILE_UNVERIFIED')
    if not isinstance(value.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',value['sha256']):raise ValueError('RESEARCH_CLOUD_FILE_HASH')
    end=0
    for block in value['blocks']:
        if (block.get('offset')!=end or type(block.get('bytes')) is not int or not 0<block['bytes']<=64*1024**2
            or not isinstance(block.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',block['sha256'])):
            raise ValueError('RESEARCH_CLOUD_BLOCK')
        end+=block['bytes']
    if not value['blocks'] or end!=value.get('bytes'):raise ValueError('RESEARCH_CLOUD_FILE_SIZE')
    if expected and (value['sha256']!=expected['sha256'] or value['bytes']!=expected['bytes']):raise ValueError('RESEARCH_CLOUD_FILE_IDENTITY')
    return value

class CloudFile:
    def __init__(self,base,path,expected=None):
        self.value=load_reference(path,expected);self.size=self.value['bytes'];self.cloud=Cloud(base)
        if self.value.get('budget_sha256')!=self.cloud.spend.hash:
            self.cloud.close();raise ValueError('RESEARCH_CLOUD_FILE_AUTHORITY')
        self.offsets=[r['offset'] for r in self.value['blocks']]
    def close(self):self.cloud.close()
    def read(self,offset,size):
        from bisect import bisect_right
        if type(offset) is not int or type(size) is not int or offset<0 or not 0<=size<=16*1024**2 or offset+size>self.size:
            raise ValueError('RESEARCH_CLOUD_READ_BOUND')
        pieces=[]
        while size:
            b=self.value['blocks'][bisect_right(self.offsets,offset)-1];take=min(size,b['offset']+b['bytes']-offset)
            from .memory_cache import shared_cache
            cache=shared_cache()
            if cache.limit:
                # Recheck durable verification even on a RAM hit. Immutable
                # cached bytes never stand in for spending/source authority.
                found=self.cloud.spend.db.execute('SELECT bytes,verified FROM objects WHERE digest=?',(b['sha256'],)).fetchone()
                if found!=(b['bytes'],1):raise ValueError('RESEARCH_CLOUD_UNVERIFIED_OBJECT')
                def load():
                    with self.cloud.cached(b['sha256'],b['bytes']) as f:return f.read(b['bytes']+1)
                raw=cache.read(self.cloud.spend.hash,b['sha256'],b['bytes'],load)
                data=raw[offset-b['offset']:offset-b['offset']+take]
            else:
                with self.cloud.cached(b['sha256'],b['bytes']) as f:f.seek(offset-b['offset']);data=f.read(take)
            if len(data)!=take:raise ValueError('RESEARCH_CLOUD_SHORT_READ')
            pieces.append(data);offset+=take;size-=take
        return b''.join(pieces)

def archive_file(cloud,path,reference_path,expected,progress=None,*,workers=None):
    """Resume block uploads; keep the original until caller verifies and retires."""
    path=Path(path);reference_path=Path(reference_path)
    if reference_path.exists():return load_reference(reference_path,expected)
    before=identity(path)
    if before['bytes']!=expected['bytes']:raise ValueError('RESEARCH_CLOUD_SOURCE_SIZE')
    workers=int(os.environ.get('EM_ARCHIVE_UPLOAD_WORKERS','1')) if workers is None else workers
    if type(workers) is not int or not 1<=workers<=4:raise ValueError('ARCHIVE_WORKERS')
    staging=cloud.out/'staging';staging.mkdir(exist_ok=True)
    h=hashlib.sha256();blocks=[];offset=0
    if workers>1 and before['bytes']>BLOCK:
        from .parallel_archive import upload_parts
        cloud.guard_local(workers*(BLOCK+1024**2))
        blocks=upload_parts(cloud.base,path,before['bytes'],BLOCK,workers,Cloud,progress,guard=cloud.guard_local)
        offset=sum(r['bytes'] for r in blocks)
        # Whole-file identity is independent of lane order and completion order.
        with path.open('rb') as f:
            while raw:=f.read(BLOCK):cloud.guard_local(0);h.update(raw)
    else:
        with path.open('rb') as f:
            while raw:=f.read(BLOCK):
                cloud.guard_local(BLOCK+1024**2)
                h.update(raw);temporary=staging/('upload-'+uuid.uuid4().hex+'.part')
                with temporary.open('xb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
                row=cloud.upload(temporary);blocks.append(dict(offset=offset,**row));offset+=len(raw)
                temporary.unlink()
                if progress:progress(offset,before['bytes'])
    if h.hexdigest()!=expected['sha256'] or identity(path)!=before:raise ValueError('RESEARCH_CLOUD_SOURCE_CHANGED')
    value=dict(schema='economic-machine.research-cloud-file.v1',sha256=h.hexdigest(),bytes=offset,blocks=blocks,
        original_local_identity=before,budget_sha256=cloud.spend.hash,source_preserved_in_cloud=True,
        complete_cloud_readback=True,at_ms=time.time_ns()//1000000)
    # Upload the portable reference before publishing it locally.
    draft=staging/('reference-'+uuid.uuid4().hex+'.tmp');atomic(draft,value);proof=cloud.upload(draft)
    atomic(reference_path,value);atomic(reference_path.with_suffix('.receipt.json'),proof);draft.unlink()
    return value

def retire_file(cloud,path,reference_path,expected):
    """Remove one duplicate only; the immutable cloud reference is already durable."""
    path=Path(path);value=load_reference(reference_path,expected)
    if not path.exists():return 0
    if identity(path)!=value['original_local_identity']:raise ValueError('RESEARCH_CLOUD_RETIRE_CHANGED')
    proof=document(Path(reference_path).with_suffix('.receipt.json'),65536)
    if proof['sha256']!=digest_file(reference_path):raise ValueError('RESEARCH_CLOUD_CATALOG_HASH')
    if value.get('budget_sha256')!=cloud.spend.hash:raise ValueError('RESEARCH_CLOUD_FILE_AUTHORITY')
    for row in [*value['blocks'],proof]:
        found=cloud.spend.db.execute('SELECT bytes,verified FROM objects WHERE digest=?',(row['sha256'],)).fetchone()
        if found!=(row['bytes'],1):raise ValueError('RESEARCH_CLOUD_RETIRE_UNVERIFIED')
    n=value['bytes'];path.unlink()
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return n
