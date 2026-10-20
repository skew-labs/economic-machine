"""Resumable free GSFA archive download with a separate immutable IO ledger.

Uses the specifically approved epoch, aggregate storage and original deadline.
Transport-complete is not a validated address index or canonical chain history.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from economic_machine.pump_research.archive import NoRedirect
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.research_extension import extension
from economic_machine.pump_research.store import atomic

CHUNK=64*1024**2

def checked_range(value,start,size,total=None):
    m=re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)',value or '')
    if not m or (int(m[1]),int(m[2]))!=(start,start+size-1):raise ValueError('EPOCH_RANGE_NOT_HONORED')
    found=int(m[3])
    if found<start+size or (total is not None and found!=total):raise ValueError('EPOCH_OBJECT_SIZE_CHANGED')
    return found

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    grant=extension(base,scope.hash,scope.grant['deadline_ms'])
    if grant is None:raise ValueError('EPOCH_APPROVAL_REQUIRED')
    folder=base/'evidence/address-epoch-v1';chunks=folder/'chunks';chunks.mkdir(exist_ok=True)
    lock=(folder/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=sqlite3.connect(folder/'io.sqlite3');db.row_factory=sqlite3.Row
    db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,start INTEGER,bytes INTEGER,state TEXT,sha256 TEXT,at REAL); CREATE TABLE IF NOT EXISTS chunks(start INTEGER PRIMARY KEY,bytes INTEGER,sha256 TEXT,path TEXT);')
    identity=hashlib.sha256((folder/'grant.json').read_bytes()).hexdigest()
    old=db.execute('SELECT hash FROM identity').fetchall()
    if old and [r[0] for r in old]!=[identity]:raise ValueError('EPOCH_GRANT_CHANGED')
    with db:db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(identity,))
    status=dict(state='STARTING',epoch=685,deadline_ms=grant['deadline_ms'],paid_api=False,
        address_index_usable=False,whole_archive_validated=False,independent_consensus_verified=False)
    url='https://files.old-faithful.net/685/epoch-685-gsfa.index.tar.zstd'
    descriptor=folder/'source.json'
    meta=json.loads(descriptor.read_bytes()) if descriptor.exists() else None
    def report(**values):
        status.update(values,updated_ms=time.time_ns()//1000000,
            reserved_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM requests').fetchone()[0],
            downloaded_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM chunks').fetchone()[0])
        atomic(folder/'status.json',status)
    def fetch(start,size,total=None):
        cooldown=base/'evidence/wallet-map-v1/archive-ranges/network-state.json'
        while cooldown.exists():
            until=json.loads(cooldown.read_bytes()).get('not_before',0)
            if until<=time.time():break
            scope.guard();time.sleep(max(0,min(1,until-time.time())))
        scope.guard(size,force=True)
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT coalesce(sum(bytes),0) FROM requests').fetchone()[0]+size>grant['additional_io_bytes']:
                raise ValueError('EPOCH_IO_LIMIT')
            ident=db.execute('INSERT INTO requests(start,bytes,state,at) VALUES(?,?,?,?)',(start,size,'reserved',time.time())).lastrowid
        path=chunks/f'request-{ident:06d}.part';h=hashlib.sha256();headers={}
        req=urllib.request.Request(url,headers={'User-Agent':'EconomicMachineResearch/0.1','Accept-Encoding':'identity','Range':f'bytes={start}-{start+size-1}'})
        with urllib.request.build_opener(NoRedirect).open(req,timeout=60) as response:
            if response.status!=206:raise ValueError('EPOCH_HTTP_RANGE_REQUIRED')
            length=checked_range(response.headers.get('Content-Range'),start,size,total)
            headers={k:response.headers.get(k) for k in ('ETag','Last-Modified')}
            if meta and any(meta.get(k)!=v for k,v in headers.items()):raise ValueError('EPOCH_SOURCE_IDENTITY_CHANGED')
            with path.open('xb') as f:
                left=size
                while left:
                    scope.guard();data=response.read(min(left,1024**2))
                    if not data:raise ValueError('EPOCH_TRUNCATED_RESPONSE')
                    f.write(data);h.update(data);left-=len(data)
                if response.read(1):raise ValueError('EPOCH_OVERSIZED_RESPONSE')
                f.flush();os.fsync(f.fileno())
        final=path.with_suffix('.range');os.replace(path,final)
        with db:db.execute("UPDATE requests SET state='complete',sha256=? WHERE id=?",(h.hexdigest(),ident))
        return dict(size=length,url=url,**headers),final,h.hexdigest()
    attempts=0
    try:
        while True:
            scope.guard(128*1024**2,force=True)
            try:
                if meta is None:
                    meta,path,digest=fetch(0,1)
                    if meta['size']>grant['additional_io_bytes']-1024**2:raise ValueError('EPOCH_FILE_EXCEEDS_IO_GRANT')
                    atomic(descriptor,dict(meta,probe_sha256=digest,probe_file=path.name))
                    meta=json.loads(descriptor.read_bytes())
                rows=db.execute('SELECT * FROM chunks ORDER BY start').fetchall();position=0
                for row in rows:
                    if row['start']!=position:raise ValueError('EPOCH_CHUNK_GAP')
                    file=chunks/row['path']
                    if file.stat().st_size!=row['bytes']:raise ValueError('EPOCH_LOCAL_CHUNK_SIZE')
                    position+=row['bytes']
                if position==meta['size']:break
                size=min(CHUNK,meta['size']-position)
                report(state='DOWNLOADING',total_bytes=meta['size'],next_offset=position)
                _,path,digest=fetch(position,size,meta['size'])
                with db:db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(position,size,digest,path.name))
                attempts=0;report(state='DOWNLOADING',next_offset=position+size)
                for _ in range(15):scope.guard();time.sleep(1)
            except (urllib.error.URLError,TimeoutError) as e:
                if isinstance(e,urllib.error.HTTPError) and e.code not in (429,502,503,504):raise
                attempts+=1;delay=min(1800,60*2**min(attempts-1,5))
                value=e.headers.get('Retry-After','') if isinstance(e,urllib.error.HTTPError) and e.headers else ''
                try:delay=max(delay,int(value) if value.isdigit() else int(parsedate_to_datetime(value).timestamp()-time.time()))
                except (ValueError,TypeError,OverflowError):pass
                cooldown=base/'evidence/wallet-map-v1/archive-ranges/network-state.json'
                prior=json.loads(cooldown.read_bytes()) if cooldown.exists() else {}
                atomic(cooldown,dict(not_before=max(prior.get('not_before',0),time.time()+delay),
                    rate_limit_attempts=attempts,last_status=e.code if isinstance(e,urllib.error.HTTPError) else None,failed_reservation_retained=True))
                report(state='SOURCE_BACKOFF',error_type=type(e).__name__,http_status=e.code if isinstance(e,urllib.error.HTTPError) else None,retry_at_ms=time.time_ns()//1000000+delay*1000)
                for _ in range(delay):scope.guard();time.sleep(1)
        report(state='VERIFYING_DOWNLOADED_BYTES')
        combined=hashlib.sha256()
        manifest=[]
        for row in db.execute('SELECT * FROM chunks ORDER BY start'):
            h=hashlib.sha256()
            with (chunks/row['path']).open('rb') as f:
                while data:=f.read(1024**2):scope.guard();h.update(data);combined.update(data)
            if h.hexdigest()!=row['sha256']:raise ValueError('EPOCH_LOCAL_CHUNK_HASH')
            manifest.append(dict(row))
        atomic(folder/'download-manifest.json',dict(source=meta,chunks=manifest,sha256=combined.hexdigest(),
            local_chunk_hashes_verified=True,publisher_checksum_verified=False,archive_format_verified=False,
            address_index_usable=False,independent_consensus_verified=False))
        report(state='DOWNLOADED_PENDING_IMPORT',sha256=combined.hexdigest())
    except BaseException as e:
        report(state='STOPPED_ERROR',error_type=type(e).__name__,error=str(e)[:160]);raise
    finally:db.close();scope.close();lock.close()

if __name__=='__main__':main()
