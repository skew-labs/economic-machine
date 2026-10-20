"""Additive, metered public GSFA acquisition; never downloads complete CARs.

One epoch is downloaded, fully inspected and imported before the next archive
is admitted. Published epoch availability is a provider claim, not finality.
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
from economic_machine.pump_research.archive_gate import ArchiveGate
from economic_machine.pump_research.research_expansion import ExpansionScope
from economic_machine.pump_research.store import atomic,canonical

MIB=1024**2
CHUNK=64*MIB-1  # The overflow byte is reserved in the same <=64 MiB request.
CATALOG='https://raw.githubusercontent.com/rpcpool/yellowstone-faithful/gha-report/docs/CAR-REPORT.md'


def source_url(epoch):
    if type(epoch) is not int or not 0<=epoch<=100000:raise ValueError('EPOCH_ARGUMENT')
    return f'https://files.old-faithful.net/{epoch}/epoch-{epoch}-gsfa.index.tar.zstd'


def parse_catalog(raw):
    if not isinstance(raw,bytes) or len(raw)>MIB:raise ValueError('EPOCH_CATALOG_BOUND')
    rows=[];seen=set()
    for line in raw.decode('utf-8').splitlines():
        m=re.match(r'^\|\s*(\d+)\s*\|',line)
        if not m:continue
        epoch=int(m[1]);source_url(epoch)
        if epoch in seen:raise ValueError('EPOCH_CATALOG_DUPLICATE')
        seen.add(epoch)
        available=f'https://files.old-faithful.net/{epoch}/epoch-{epoch}.car' in line
        rows.append(dict(epoch=epoch,car_provider_available=available,gsfa_available_unverified=True,
            provider_state='available' if available else ('ongoing' if 'ongoing' in line else 'missing'),
            row_sha256=hashlib.sha256(line.encode()).hexdigest()))
    if not rows:raise ValueError('EPOCH_CATALOG_EMPTY')
    return rows


def checked_range(value,start,size,total):
    m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',value or '')
    if not m or tuple(map(int,m.groups()))!=(start,start+size-1,total):
        raise ValueError('EPOCH_RANGE_IDENTITY')


def execution_authority(scope):
    """Execution may continue; the original artifact identity never changes."""
    effective=getattr(scope,'effective_deadline_ms',scope.deadline)
    continuation=getattr(scope,'continuation',None)
    if continuation is not None:
        if (continuation.parent_expansion_hash!=scope.hash or
            not re.fullmatch(r'[0-9a-f]{64}',continuation.hash) or
            continuation.effective_deadline_ms!=effective):
            raise ValueError('EPOCH_CONTINUATION_PARENT')
    elif effective is None:
        raise ValueError('EPOCH_CONTINUATION_REQUIRED')
    return dict(original_grant_sha256=scope.hash,original_deadline_ms=scope.deadline,
        effective_deadline_ms=effective,
        continuation_grant_sha256=continuation.hash if continuation is not None else None)


class EpochDownloader:
    def __init__(self,base,scope,opener=None):
        self.base,self.scope=Path(base),scope
        self.out=self.base/'evidence/address-epochs-v2';self.out.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.out/'planner.sqlite3');self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS epochs(epoch INTEGER PRIMARY KEY,state TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS catalogue(epoch INTEGER PRIMARY KEY,body TEXT,source_sha256 TEXT);
          CREATE TABLE IF NOT EXISTS demands(epoch INTEGER,source TEXT,body TEXT,PRIMARY KEY(epoch,source));
          CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,epoch INTEGER,start INTEGER,bytes INTEGER,
            reservation INTEGER,kind TEXT,state TEXT,path TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS chunks(epoch INTEGER,start INTEGER,bytes INTEGER,sha256 TEXT,path TEXT,
            PRIMARY KEY(epoch,start));
          CREATE TABLE IF NOT EXISTS execution_authorities(
            hash TEXT PRIMARY KEY,body TEXT NOT NULL,first_observed_ms INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS request_authorities(
            request_id INTEGER PRIMARY KEY,grant_sha256 TEXT NOT NULL,ledger_path TEXT NOT NULL);''')
        old=[r[0] for r in self.db.execute('SELECT hash FROM identity')]
        if old and old!=[scope.hash]:self.db.close();raise ValueError('EPOCH_EXPANSION_CHANGED')
        with self.db:self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(scope.hash,))
        try:
            authority=execution_authority(scope)
            if authority['continuation_grant_sha256'] is not None:
                scope.guard()
                digest=authority['continuation_grant_sha256'];body=canonical(authority)
                old=self.db.execute('SELECT body FROM execution_authorities WHERE hash=?',(digest,)).fetchone()
                if old and old[0]!=body:raise ValueError('EPOCH_EXECUTION_AUTHORITY_CHANGED')
                with self.db:self.db.execute('INSERT OR IGNORE INTO execution_authorities VALUES(?,?,?)',
                    (digest,body,time.time_ns()//1000000))
        except BaseException:
            self.db.close();raise
        self.opener=opener or urllib.request.build_opener(NoRedirect)
        self.cooldown=self.base/'evidence/wallet-map-v1/archive-ranges/network-state.json'
        self.catalogue_cooldown=self.out/'catalogue-network-state.json'
    def close(self):self.db.close()
    def folder(self,epoch):
        source_url(epoch);p=self.out/'epochs'/str(epoch)
        p.mkdir(parents=True,exist_ok=True);(p/'chunks').mkdir(exist_ok=True)
        if p.is_symlink() or (p/'chunks').is_symlink():raise ValueError('EPOCH_DIRECTORY_LINK')
        return p
    def state(self,epoch,state,**body):
        with self.db:self.db.execute('INSERT INTO epochs VALUES(?,?,?) ON CONFLICT(epoch) DO UPDATE SET state=excluded.state,body=excluded.body',
            (epoch,state,canonical(body)))
        atomic(self.out/'status.json',self.snapshot())
    def wait_source(self,path=None):
        path=path or self.cooldown
        while path.exists():
            value=json.loads(path.read_bytes())
            # The legacy reader's 15-second successful-read spacing is not a
            # provider Retry-After. Actual errors/unknown cooldowns are honored.
            if value.get('last_status')==206:return
            left=value.get('not_before',0)-time.time()
            if left<=0:return
            self.scope.guard();time.sleep(min(1,left))
    def backoff(self,error,path=None):
        path=path or self.catalogue_cooldown
        prior=json.loads(path.read_bytes()) if path.exists() else {}
        attempts=prior.get('rate_limit_attempts',0)+1;delay=min(1800,60*2**min(attempts-1,5))
        retry=error.headers.get('Retry-After','') if isinstance(error,urllib.error.HTTPError) and error.headers else ''
        try:delay=max(delay,float(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp()-time.time())
        except (ValueError,TypeError,OverflowError):pass
        path.parent.mkdir(parents=True,exist_ok=True)
        atomic(path,dict(not_before=max(prior.get('not_before',0),time.time()+delay),
            rate_limit_attempts=attempts,last_status=getattr(error,'code',None),failed_reservation_retained=True))
    def reserve(self,epoch,start,size,kind):
        continuation=getattr(self.scope,'continuation',None)
        if continuation is not None and epoch>=0:continuation.assert_epoch(epoch)
        reservation=self.scope.reserve(size,f'epochs-v2:{epoch}:{kind}:{start}')
        authority=execution_authority(self.scope)
        continuation=authority['continuation_grant_sha256']
        with self.db:
            ident=self.db.execute('INSERT INTO requests(epoch,start,bytes,reservation,kind,state,body) VALUES(?,?,?,?,?,?,?)',
                (epoch,start,size,reservation,kind,'reserved','{}')).lastrowid
            self.db.execute('INSERT INTO request_authorities VALUES(?,?,?)',(ident,continuation or self.scope.hash,
                'evidence/solana-continuation-v1/io.sqlite3' if continuation else 'evidence/solana-expansion-v2/io.sqlite3'))
        return ident
    def catalogue(self):
        receipt=self.out/'catalogue-receipt.json'
        if getattr(self.scope,'continuation',None) is not None and not receipt.exists():
            raise ValueError('EPOCH_CONTINUATION_CATALOGUE_REQUIRED')
        if receipt.exists():
            saved=json.loads(receipt.read_bytes());p=self.out/saved['path']
            if p.name!=saved['path'] or p.is_symlink():raise ValueError('EPOCH_CATALOG_PATH')
            raw=p.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=saved['sha256']:raise ValueError('EPOCH_CATALOG_HASH')
        else:
            while True:
                self.wait_source(self.catalogue_cooldown);ident=self.reserve(-1,0,MIB+1,'catalogue')
                try:
                    with self.opener.open(urllib.request.Request(CATALOG,headers={'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'}),timeout=30) as r:
                        if r.status!=200:raise ValueError('EPOCH_CATALOG_STATUS')
                        raw=r.read(MIB+1)
                    break
                except (urllib.error.URLError,TimeoutError) as error:
                    if isinstance(error,urllib.error.HTTPError) and error.code!=429 and not 500<=error.code<=599:raise
                    self.backoff(error)
                    with self.db:self.db.execute("UPDATE requests SET state='failed_retained',body=? WHERE id=?",
                        (canonical(dict(error_type=type(error).__name__,http_status=getattr(error,'code',None))),ident))
            parse_catalog(raw);digest=hashlib.sha256(raw).hexdigest();p=self.out/f'catalogue-{digest}.md'
            if p.exists():
                if p.is_symlink() or p.read_bytes()!=raw:raise ValueError('EPOCH_CATALOG_HASH')
            else:
                with p.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            saved=dict(path=p.name,sha256=digest,bytes=len(raw),url=CATALOG,at_ms=time.time_ns()//1000000)
            atomic(receipt,saved)
            with self.db:self.db.execute("UPDATE requests SET state='complete',body=? WHERE id=?",(canonical(saved),ident))
        rows=parse_catalog(raw)
        with self.db:
            for row in rows:
                self.db.execute('INSERT OR IGNORE INTO catalogue VALUES(?,?,?)',(row['epoch'],canonical(row),saved['sha256']))
                self.db.execute('INSERT OR IGNORE INTO epochs VALUES(?,?,?)',(row['epoch'],
                    'LEGACY_IMPORT_REUSED' if row['epoch']==685 else ('QUEUED' if row['car_provider_available'] else 'PROVIDER_UNAVAILABLE'),canonical(row)))
        return saved
    def demand(self):
        continuation=getattr(self.scope,'continuation',None)
        def initial_state(epoch):
            if epoch==685:return 'LEGACY_IMPORT_REUSED'
            if continuation is not None and epoch not in continuation.inventory_epochs:
                return 'OUTSIDE_CONTINUATION_INVENTORY'
            return 'QUEUED'
        for epoch in self.scope.grant['priority_epochs']:
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO epochs VALUES(?,?,?)',(epoch,initial_state(epoch),'{}'))
                self.db.execute('INSERT OR IGNORE INTO demands VALUES(?,?,?)',(epoch,'owner_priority','{}'))
        path=self.base/'evidence/wallet-map-v1/graph.sqlite3'
        if not path.exists():return
        db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
        try:
            rows=db.execute('''SELECT slot/432000 AS epoch,count(DISTINCT root),count(DISTINCT wallet)
                FROM frontier WHERE slot BETWEEN 1 AND 1000000000 GROUP BY slot/432000''').fetchall()
            with self.db:
                for epoch,roots,wallets in rows:
                    self.db.execute('INSERT OR IGNORE INTO epochs VALUES(?,?,?)',(epoch,initial_state(epoch),'{}'))
                    self.db.execute('INSERT OR IGNORE INTO demands VALUES(?,?,?)',(epoch,'retained_frontier',
                        canonical(dict(roots=roots,wallets=wallets,slot_provenance='existing_anchor_not_global_history_proof'))))
        finally:db.close()
    def pending(self):
        priorities={e:n for n,e in enumerate(self.scope.grant['priority_epochs'])}
        demanded={r[0] for r in self.db.execute('SELECT DISTINCT epoch FROM demands')}
        rows=[r[0] for r in self.db.execute("SELECT epoch FROM epochs WHERE epoch != 685 AND state NOT IN ('IMPORTED','LEGACY_IMPORT_REUSED','PROVIDER_UNAVAILABLE')")]
        continuation=getattr(self.scope,'continuation',None)
        if continuation is not None:rows=[epoch for epoch in rows if epoch in continuation.inventory_epochs]
        return sorted(rows,key=lambda e:(priorities.get(e,999),0 if e in demanded else 1,-e))
    def describe(self,epoch):
        if epoch==685:raise ValueError('EPOCH_LEGACY_IMPORT_PRESERVED')
        continuation=getattr(self.scope,'continuation',None)
        if continuation is not None:continuation.assert_epoch(epoch)
        folder=self.folder(epoch);p=folder/'source.json'
        if p.exists():
            value=json.loads(p.read_bytes())
            if value.get('url')!=source_url(epoch) or type(value.get('size')) is not int or value['size']<=0:
                raise ValueError('EPOCH_SOURCE_DESCRIPTOR')
            return value
        while True:
            req=urllib.request.Request(source_url(epoch),method='HEAD',headers={'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'})
            ident=None
            try:
                with ArchiveGate(self.cooldown.parent,self.scope.guard,wait=True,success_status=200):
                    ident=self.reserve(epoch,0,4097,'metadata')
                    with self.opener.open(req,timeout=30) as r:
                        raw=r.read(4097)
                        if r.status!=200 or len(raw)>4096:raise ValueError('EPOCH_METADATA_RESPONSE')
                        size=r.headers.get('Content-Length','')
                        if not size.isdigit() or int(size)<=0:raise ValueError('EPOCH_SOURCE_SIZE')
                        value=dict(url=source_url(epoch),size=int(size),etag=r.headers.get('ETag'),last_modified=r.headers.get('Last-Modified'),
                            supports_ranges=r.headers.get('Accept-Ranges')=='bytes',at_ms=time.time_ns()//1000000)
                break
            except (urllib.error.URLError,TimeoutError) as error:
                if isinstance(error,urllib.error.HTTPError) and error.code!=429 and not 500<=error.code<=599:raise
                with self.db:self.db.execute("UPDATE requests SET state='failed_retained',body=? WHERE id=?",
                    (canonical(dict(error_type=type(error).__name__,http_status=getattr(error,'code',None))),ident))
        atomic(p,value)
        with self.db:self.db.execute("UPDATE requests SET state='complete',body=? WHERE id=?",(canonical(value),ident))
        return value
    def retained(self,epoch):
        folder=self.folder(epoch);position=0;combined=hashlib.sha256()
        for row in self.db.execute('SELECT * FROM chunks WHERE epoch=? ORDER BY start',(epoch,)):
            if row['start']!=position or not 0<row['bytes']<=CHUNK:raise ValueError('EPOCH_CHUNK_GAP')
            p=folder/'chunks'/row['path']
            if p.name!=row['path'] or p.is_symlink() or p.stat().st_size!=row['bytes']:raise ValueError('EPOCH_CHUNK_IDENTITY')
            h=hashlib.sha256()
            with p.open('rb') as f:
                while raw:=f.read(MIB):self.scope.guard();h.update(raw);combined.update(raw)
            if h.hexdigest()!=row['sha256']:raise ValueError('EPOCH_CHUNK_HASH')
            position+=row['bytes']
        return position,combined
    def chunk(self,epoch,source,start,size,combined):
        if not 0<size<=CHUNK or start<0 or start+size>source['size']:raise ValueError('EPOCH_CHUNK_BOUND')
        with ArchiveGate(self.cooldown.parent,self.scope.guard,wait=True):
            return self._chunk(epoch,source,start,size,combined)
    def _chunk(self,epoch,source,start,size,combined):
        self.scope.guard(size,force=True)
        ident=self.reserve(epoch,start,size+1,'range');folder=self.folder(epoch)
        path=folder/'chunks'/f'request-{ident:07}.part';h=hashlib.sha256();next_combined=combined.copy()
        request=urllib.request.Request(source['url'],headers={'Range':f'bytes={start}-{start+size-1}','Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'})
        try:
            with self.opener.open(request,timeout=60) as r:
                if r.status!=206:raise ValueError('EPOCH_RANGE_STATUS')
                checked_range(r.headers.get('Content-Range'),start,size,source['size'])
                if any(source[k] is not None and source[k]!=r.headers.get(header) for k,header in [('etag','ETag'),('last_modified','Last-Modified')]):
                    raise ValueError('EPOCH_SOURCE_CHANGED')
                with path.open('xb') as f:
                    left=size
                    while left:
                        self.scope.guard();raw=r.read(min(left,MIB))
                        if not raw:raise EOFError('EPOCH_RANGE_TRUNCATED')
                        f.write(raw);h.update(raw);next_combined.update(raw);left-=len(raw)
                    if r.read(1):raise ValueError('EPOCH_RANGE_OVERSIZED')
                    f.flush();os.fsync(f.fileno())
            final=path.with_suffix('.range');os.rename(path,final)
            with self.db:
                self.db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)',(epoch,start,size,h.hexdigest(),final.name))
                self.db.execute("UPDATE requests SET state='complete',path=?,body=? WHERE id=?",(final.name,canonical(dict(sha256=h.hexdigest(),response_bytes=size)),ident))
            return next_combined
        except BaseException as error:
            with self.db:self.db.execute("UPDATE requests SET state='failed_retained',path=?,body=? WHERE id=?",
                (path.name,canonical(dict(error_type=type(error).__name__,retained_bytes=path.stat().st_size if path.exists() else 0)),ident))
            raise
    def download(self,epoch):
        continuation=getattr(self.scope,'continuation',None)
        if continuation is not None:continuation.assert_epoch(epoch)
        source=self.describe(epoch);folder=self.folder(epoch);position,combined=self.retained(epoch)
        remaining=source['size']-position
        if remaining<0:raise ValueError('EPOCH_SOURCE_SIZE_CHANGED')
        self.scope.guard(remaining,force=True)
        allowance=(self.scope.remaining_io() if callable(getattr(self.scope,'remaining_io',None)) else
            self.scope.grant['additional_io_bytes']-self.scope.reserved())
        if allowance is None and getattr(self.scope,'continuation',None) is None:
            raise ValueError('EPOCH_CONTINUATION_REQUIRED')
        if allowance is not None and remaining+(remaining//CHUNK+1)>allowance:
            raise ValueError('CORE_ADDITIONAL_IO_LIMIT')
        while position<source['size']:
            self.state(epoch,'DOWNLOADING',received_bytes=position,total_bytes=source['size'])
            size=min(CHUNK,source['size']-position)
            try:combined=self.chunk(epoch,source,position,size,combined)
            except (urllib.error.URLError,TimeoutError,EOFError) as error:
                if isinstance(error,urllib.error.HTTPError) and error.code!=429 and not 500<=error.code<=599:raise
                self.state(epoch,'SOURCE_BACKOFF',received_bytes=position,total_bytes=source['size'],error_type=type(error).__name__)
                continue
            position+=size
            self.state(epoch,'DOWNLOADING',received_bytes=position,total_bytes=source['size'])
        chunks=[{k:r[k] for k in ('start','bytes','sha256','path')} for r in self.db.execute('SELECT * FROM chunks WHERE epoch=? ORDER BY start',(epoch,))]
        value=dict(source=source,chunks=chunks,sha256=combined.hexdigest(),local_chunk_hashes_verified=True,
            publisher_checksum_verified=False,archive_format_verified=False,address_index_usable=False,independent_consensus_verified=False)
        p=folder/'download-manifest.json'
        if p.exists() and json.loads(p.read_bytes())!=value:raise ValueError('EPOCH_MANIFEST_CHANGED')
        if not p.exists():atomic(p,value)
        self.state(epoch,'DOWNLOADED_PENDING_INSPECTION',compressed_bytes=position,sha256=value['sha256'])
        return p
    def snapshot(self,reason=None):
        states=dict(self.db.execute('SELECT state,count(*) FROM epochs GROUP BY state'))
        pending=self.pending()
        catalogue_count=self.db.execute('SELECT count(*) FROM catalogue').fetchone()[0]
        inventory_exhausted=bool(catalogue_count) and not pending
        authority=execution_authority(self.scope)
        continuation=getattr(self.scope,'continuation',None)
        outside=([r[0] for r in self.db.execute('SELECT epoch FROM epochs ORDER BY epoch')
            if r[0] not in continuation.inventory_epochs] if continuation is not None else [])
        return dict(schema='economic-machine.epoch-acquisition-status.v2',at_ms=time.time_ns()//1000000,
            grant_sha256=self.scope.hash,deadline_ms=authority['effective_deadline_ms'],**authority,
            requested_epochs=sum(states.values()),catalogued_epochs=catalogue_count,states=states,
            downloaded_epochs=self.db.execute('''SELECT count(DISTINCT epoch) FROM chunks WHERE epoch IN
                (SELECT epoch FROM epochs WHERE state IN ('DOWNLOADED_PENDING_INSPECTION','INSPECTING_COMPLETE_ARCHIVE',
                'INSPECTED','EXTRACTING_VERIFIED_MEMBERS','EXTRACTED_WAITING_TARGET_QUERY','IMPORTED'))''').fetchone()[0],
            imported_epochs=states.get('IMPORTED',0),legacy_imports_reused=states.get('LEGACY_IMPORT_REUSED',0),
            active_epochs=[dict(epoch=r['epoch'],state=r['state'],**json.loads(r['body'])) for r in self.db.execute(
                "SELECT epoch,state,body FROM epochs WHERE state NOT IN ('QUEUED','PROVIDER_UNAVAILABLE','LEGACY_IMPORT_REUSED','IMPORTED','OUTSIDE_CONTINUATION_INVENTORY') ORDER BY epoch LIMIT 8")],
            pending_epochs=pending,pending_reason=reason,reserved_bytes=self.scope.reserved(),
            inventory_exhausted=inventory_exhausted,inventory_scope='frozen_provider_catalogue',
            unavailable_epochs=[r[0] for r in self.db.execute("SELECT epoch FROM epochs WHERE state='PROVIDER_UNAVAILABLE' ORDER BY epoch")],
            outside_continuation_inventory_epochs=outside,
            history_coverage='NOT_PROVEN_COMPLETE',
            whole_wallet_history=False,independent_consensus_verified=False,complete_car_downloads=0)


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    scope=ExpansionScope(a.base);worker=EpochDownloader(a.base,scope);lock=(worker.out/'worker.lock').open('a')
    reason=None
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);worker.demand();worker.catalogue()
        from epochs_import import inspect_and_import
        for epoch in worker.pending():
            scope.guard()
            try:
                manifest=worker.download(epoch)
                receipt=inspect_and_import(worker.base,epoch,scope,manifest,
                    notify=lambda state,**body:worker.state(epoch,state,**body))
                worker.state(epoch,'IMPORTED',receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
            except urllib.error.HTTPError as error:
                if error.code==404:worker.state(epoch,'PROVIDER_UNAVAILABLE',http_status=404);continue
                raise
            atomic(worker.out/'status.json',worker.snapshot())
    except BaseException as error:
        reason=str(error)[:160];raise
    finally:
        atomic(worker.out/'status.json',worker.snapshot(reason));lock.close();worker.close();scope.close()


if __name__=='__main__':main()
