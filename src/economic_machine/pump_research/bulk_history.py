"""Bounded CAR range ingestion into our existing address index.

The provider's slot table is a locator, not evidence of consensus or of an
empty slot. Every admitted block is decoded with the original CID/signature
checks. Source bytes and a portable receipt reach verified Blob storage before
the address index or the durable job cursor can advance.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.parse
import uuid

from .archive import BASE
from .archive_gate import ArchiveGate
from .store import atomic, canonical

MIB = 1024**2
DEADLINE_MS = 1792351694298
MAX_RANGE = 32*MIB
MAX_BLOCK = 16*MIB
MAX_SLOTS = 4096
EPOCH_SLOTS = 432000


def plan_ranges(raw, first_slot, *, maximum=MAX_RANGE, max_blocks=16):
    if (type(first_slot) is not int or first_slot < 0 or not raw or len(raw)%12
        or len(raw)//12 > MAX_SLOTS or type(maximum) is not int or not MAX_BLOCK <= maximum <= MAX_RANGE
        or type(max_blocks) is not int or not 1 <= max_blocks <= 32
        or first_slot//EPOCH_SLOTS != (first_slot+len(raw)//12-1)//EPOCH_SLOTS):
        raise ValueError('BULK_SLOT_TABLE_BOUND')
    pages=[]; missing=[]; current=None; previous_end=None
    for n,(offset,size) in enumerate(struct.iter_unpack('<QI',raw)):
        slot=first_slot+n
        if not size:
            missing.append(slot)
            continue  # Provider absence never becomes a skipped-slot assertion.
        if size > MAX_BLOCK or offset+size > 2**63-1 or previous_end is not None and offset < previous_end:
            raise ValueError('BULK_LOCATOR_OVERLAP_OR_BOUND')
        previous_end=offset+size
        row=dict(slot=slot,offset=offset,bytes=size)
        # Only join exactly adjacent slices; do not download unexplained gaps.
        if (current is None or offset != current['offset']+current['bytes']
            or current['bytes']+size > maximum or len(current['blocks']) >= max_blocks):
            current=dict(offset=offset,bytes=size,blocks=[]);pages.append(current)
        else:current['bytes']+=size
        current['blocks'].append(row)
    return dict(pages=pages,provider_absent_slots=missing,first_slot=first_slot,
                last_slot=first_slot+len(raw)//12-1)


class RangeHTTP:
    """One reusable TLS connection; no redirect and no hidden retries."""
    def __init__(self, factory=http.client.HTTPSConnection):
        self.factory=factory;self.connection=None

    def close(self):
        if self.connection is not None:self.connection.close();self.connection=None

    def read(self,url,start,size,pin=None):
        u=urllib.parse.urlsplit(url)
        if (u.scheme!='https' or u.netloc!='files.old-faithful.net' or u.query or u.fragment
            or not re.fullmatch(r'/[0-9]+/epoch-[0-9]+(?:\.car|-slot-ranges\.raw)',u.path)
            or type(start) is not int or start<0 or type(size) is not int or not 0<size<=MAX_RANGE):
            raise ValueError('BULK_SOURCE_BOUND')
        headers={'User-Agent':'EconomicMachineResearch/0.1','Accept-Encoding':'identity',
                 'Range':f'bytes={start}-{start+size-1}'}
        if pin and pin.get('etag'):headers['If-Match']=pin['etag']
        try:
            if self.connection is None:self.connection=self.factory(u.hostname,timeout=30)
            self.connection.request('GET',u.path,headers=headers)
            r=self.connection.getresponse()
            if r.status!=206:raise urllib.error.HTTPError(url,r.status,'bulk range rejected',r.headers,None)
            match=re.fullmatch(r'bytes ([0-9]+)-([0-9]+)/([0-9]+)',r.headers.get('Content-Range',''))
            if (not match or tuple(map(int,match.groups()[:2]))!=(start,start+size-1)
                or int(match[3])<start+size or r.headers.get('Content-Encoding','identity')!='identity'):
                raise ValueError('BULK_RANGE_RESPONSE')
            source=dict(total_bytes=int(match[3]),etag=r.headers.get('ETag'))
            if source['etag'] and (len(source['etag'])>256 or source['etag'].startswith('W/')):
                raise ValueError('BULK_WEAK_SOURCE_IDENTITY')
            if pin and source!=pin:raise ValueError('BULK_SOURCE_CHANGED')
            raw=r.read(size+1)
            if len(raw)!=size:raise ValueError('BULK_SHORT_OR_LONG_RESPONSE')
            if r.will_close:self.close()
            return raw,source
        except (OSError,http.client.HTTPException) as error:
            self.close()
            if isinstance(error,urllib.error.HTTPError):raise
            raise urllib.error.URLError(type(error).__name__) from error
        except BaseException:self.close();raise


class Journal:
    def __init__(self,out,policy):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.out/'runtime.sqlite3',timeout=30);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS sources(url TEXT PRIMARY KEY,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,url TEXT,start INTEGER,bytes INTEGER,state TEXT,at_ms INTEGER);
          CREATE TABLE IF NOT EXISTS ranges(id TEXT PRIMARY KEY,url TEXT,start INTEGER,bytes INTEGER,hash TEXT,source TEXT);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,first_slot INTEGER,last_slot INTEGER,state TEXT,plan TEXT);
          CREATE TABLE IF NOT EXISTS pages(job TEXT,n INTEGER,state TEXT,body TEXT,PRIMARY KEY(job,n));
          CREATE TABLE IF NOT EXISTS indexed_blocks(slot INTEGER PRIMARY KEY,hash TEXT,transactions INTEGER,page_hash TEXT);
        ''')
        body=canonical(policy)
        with self.db:
            old=self.db.execute('SELECT body FROM identity').fetchall()
            if old and [r[0] for r in old]!=[body]:raise ValueError('BULK_POLICY_CHANGED')
            self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(body,))
        self.policy=policy
        (self.out/'ranges').mkdir(exist_ok=True);(self.out/'receipts').mkdir(exist_ok=True)

    def close(self):self.db.close()

    def guard(self):
        if time.time_ns()//1000000>=self.policy['deadline_ms']:raise ValueError('BULK_DEADLINE')

    def reserve(self,url,start,size):
        self.guard();attempt=uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            used=self.db.execute('SELECT coalesce(sum(bytes),0) FROM attempts').fetchone()[0]
            if used+size>self.policy['maximum_source_bytes']:raise ValueError('BULK_SOURCE_ALLOWANCE')
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                (attempt,url,start,size,'reserved',time.time_ns()//1000000))
        return attempt

    def source(self,url):
        row=self.db.execute('SELECT body FROM sources WHERE url=?',(url,)).fetchone()
        return json.loads(row[0]) if row else None

    def pin(self,url,source):
        with self.db:
            old=self.source(url)
            if old and old!=source:raise ValueError('BULK_SOURCE_CHANGED')
            self.db.execute('INSERT OR IGNORE INTO sources VALUES(?,?)',(url,canonical(source)))


class Reader:
    def __init__(self,journal,scope,cloud,http=None):
        self.journal=journal;self.scope=scope;self.cloud=cloud;self.http=http or RangeHTTP()
        self.gate=scope.base/'evidence/wallet-map-v1/archive-ranges'
        self.metrics=dict(network_requests=0,network_bytes=0,cache_hits=0,fetch_seconds=0.0,archive_seconds=0.0)

    def guard(self):self.journal.guard();self.scope.guard();self.cloud.guard_local(0)

    def read(self,url,start,size):
        self.guard()
        ident=hashlib.sha256(f'{url}:{start}:{size}'.encode()).hexdigest()
        target=self.journal.out/'ranges'/(ident+'.raw')
        row=self.journal.db.execute('SELECT * FROM ranges WHERE id=?',(ident,)).fetchone()
        if row:
            if target.is_symlink() or target.stat().st_size!=size:raise ValueError('BULK_CACHE_SIZE')
            raw=target.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row['hash']:raise ValueError('BULK_CACHE_HASH')
            self.metrics['cache_hits']+=1
        else:
            with ArchiveGate(self.gate,self.guard,wait=False):
                attempt=self.journal.reserve(url,start,size)
                self.scope.reserve(size,'bulk CAR route '+ident)
                self.cloud.spend.reserve('read','bulk-'+attempt,bytes_=size)
                began=time.monotonic()
                raw,source=self.http.read(url,start,size,self.journal.source(url))
                self.metrics['fetch_seconds']+=time.monotonic()-began
                self.metrics['network_requests']+=1;self.metrics['network_bytes']+=size
                self.journal.pin(url,source)
            digest=hashlib.sha256(raw).hexdigest();temp=target.with_suffix('.'+attempt+'.part')
            with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(temp,target)
            fd=os.open(target.parent,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            with self.journal.db:
                self.journal.db.execute('INSERT INTO ranges VALUES(?,?,?,?,?,?)',
                    (ident,url,start,size,digest,canonical(source)))
                self.journal.db.execute("UPDATE attempts SET state='retained' WHERE id=?",(attempt,))
            row=self.journal.db.execute('SELECT * FROM ranges WHERE id=?',(ident,)).fetchone()
        began=time.monotonic();proof=self.cloud.upload(target,row['hash'])
        self.metrics['archive_seconds']+=time.monotonic()-began
        if proof.get('sha256')!=row['hash'] or proof.get('bytes')!=size:raise ValueError('BULK_BLOB_RECEIPT')
        return raw,dict(source_url=url,offset=start,bytes=size,sha256=row['hash'],
                        source_identity=json.loads(row['source']),blob=proof)


def validate_block(block,slot):
    if (block.get('slot')!=slot or block.get('decode_errors')
        or block.get('transaction_nodes')!=len(block.get('transactions',[]))):
        raise ValueError('BULK_PARTIAL_DECODE')
    signatures=[t['signature'] for t in block['transactions']]
    orders=[t['index'] for t in block['transactions']]
    if len(set(signatures))!=len(signatures) or len(set(orders))!=len(orders):
        raise ValueError('BULK_DUPLICATE_TRANSACTION')


def process_page(journal,reader,index,job,n,page,table_receipt,parse):
    old=journal.db.execute('SELECT state FROM pages WHERE job=? AND n=?',(job,n)).fetchone()
    if old and old[0]=='indexed':return None
    reader.guard();epoch=page['blocks'][0]['slot']//EPOCH_SLOTS
    raw,receipt=reader.read(f'{BASE}/{epoch}/epoch-{epoch}.car',page['offset'],page['bytes'])
    began=time.monotonic();decoded=[]
    for row in page['blocks']:
        offset=row['offset']-page['offset'];part=raw[offset:offset+row['bytes']]
        if len(part)!=row['bytes']:raise ValueError('BULK_SLICE_BOUND')
        block=parse(part,row['slot']);validate_block(block,row['slot'])
        decoded.append((row,block,hashlib.sha256(part).hexdigest()))
    decode_seconds=time.monotonic()-began
    body=dict(schema='economic-machine.bulk-car-page.v1',job=job,page=n,raw=receipt,slot_table=table_receipt,
        blocks=[dict(**r,car_sha256=h,transactions=len(b['transactions'])) for r,b,h in decoded],
        content_hashes_verified=True,transaction_signatures_verified=True,
        independent_consensus_verified=False,complete_address_history=False,
        decode_seconds=decode_seconds,at_ms=time.time_ns()//1000000)
    # Freeze the first portable receipt. Crash replay must not invent a new identity.
    path=journal.out/'receipts'/f'{job}-{n}.json'
    if path.exists():
        prior=json.loads(path.read_bytes())
        if any(prior[k]!=body[k] for k in ('schema','job','page','raw','slot_table','blocks')):
            raise ValueError('BULK_RECEIPT_CHANGED')
        body=prior
    else:atomic(path,body)
    portable=reader.cloud.upload(path)
    reader.guard();began=time.monotonic()
    for row,block,digest in decoded:
        reader.guard()
        # Do not silently accept a different source block at an indexed slot.
        prior=index.db.execute("SELECT hash FROM blocks WHERE slot=? AND hash!=? AND coalesce(json_extract(receipt,'$.source'),'')!='ticking_retained' LIMIT 1",(row['slot'],digest)).fetchone()
        if prior:raise ValueError('BULK_EXISTING_BLOCK_CONFLICT')
        index.ingest(block,dict(source='old_faithful_bulk_car',car_sha256=digest,
            car_offset=row['offset'],car_size=row['bytes'],bulk_page=portable,
            locator=dict(job=job,page=n,blob_sha256=receipt['sha256'],slice_offset=row['offset']-page['offset']),
            content_hashes_verified=True,transaction_signatures_verified=True,independent_consensus_verified=False))
        with journal.db:
            journal.db.execute('INSERT OR IGNORE INTO indexed_blocks VALUES(?,?,?,?)',
                (row['slot'],digest,len(block['transactions']),portable['sha256']))
    body=dict(body,portable_receipt=portable,index_seconds=time.monotonic()-began)
    with journal.db:journal.db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)',(job,n,'indexed',canonical(body)))
    return body


def run_window(journal,reader,index,first,last,parse):
    if (type(first) is not int or type(last) is not int or not 0<=first<=last
        or last-first+1>MAX_SLOTS or first//EPOCH_SLOTS!=last//EPOCH_SLOTS):
        raise ValueError('BULK_WINDOW')
    reader.scope.assert_slot(first);reader.scope.assert_slot(last);reader.guard()
    job=f'{first}-{last}';epoch=first//EPOCH_SLOTS
    raw,table=reader.read(f'{BASE}/{epoch}/epoch-{epoch}-slot-ranges.raw',(first%EPOCH_SLOTS)*12,(last-first+1)*12)
    plan=plan_ranges(raw,first);body=canonical(dict(**plan,table=table))
    with journal.db:
        prior=journal.db.execute('SELECT plan FROM jobs WHERE id=?',(job,)).fetchone()
        if prior and prior[0]!=body:raise ValueError('BULK_PLAN_CHANGED')
        journal.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?)',(job,first,last,'planned',body))
    for n,page in enumerate(plan['pages']):
        process_page(journal,reader,index,job,n,page,table,parse)
        atomic(journal.out/'status.json',status(journal,reader))
    reader.guard()
    with journal.db:journal.db.execute("UPDATE jobs SET state='indexed_with_provider_gaps' WHERE id=?",(job,))
    result=status(journal,reader);atomic(journal.out/'status.json',result)
    return result


def status(journal,reader):
    counts=journal.db.execute('SELECT count(*),coalesce(sum(transactions),0) FROM indexed_blocks').fetchone()
    return dict(at_ms=time.time_ns()//1000000,state='PARTIAL_HISTORY_INDEXED',
        indexed_blocks=counts[0],decoded_transactions=counts[1],
        source_reserved_bytes=journal.db.execute('SELECT coalesce(sum(bytes),0) FROM attempts').fetchone()[0],
        indexed_pages=journal.db.execute("SELECT count(*) FROM pages WHERE state='indexed'").fetchone()[0],
        jobs=[dict(r) for r in journal.db.execute('SELECT id,first_slot,last_slot,state FROM jobs')],
        metrics=reader.metrics,deadline_ms=journal.policy['deadline_ms'],
        full_history_complete=False,independent_consensus_verified=False)
