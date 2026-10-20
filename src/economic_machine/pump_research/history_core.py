"""Scoped streaming projection of three pinned public research tables.

This module does not execute archive SQL, use model credentials, reset the old
IO ledger, or write to the production archive. Cached compressed ranges are
retained. Resumption replays local compressed bytes and skips committed rows.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import urllib.request
import zlib

from .history_dump import columns, copy_value
from .store import atomic, decode

GIB = 1024**3
CHUNK = 16*1024**2
TABLES = ('token_metadata', 'arkm_funding', 'pump_event_default')
SCHEMAS = {
 'token_metadata': 'mint BLOB,creator BLOB,created_signature BLOB,first_seen INTEGER,last_updated INTEGER,decimals INTEGER',
 'arkm_funding': 'destination BLOB,source BLOB,signature TEXT,stamp INTEGER,slot INTEGER,symbol TEXT,amount TEXT,source_labeled INTEGER,destination_labeled INTEGER,transfer_id TEXT',
 'pump_event_default': 'mint BLOB,trader BLOB,signature BLOB,stamp INTEGER,slot INTEGER,outer_ix INTEGER,inner_ix INTEGER,ordinal INTEGER,side TEXT,sol INTEGER,tokens TEXT,decimals INTEGER,parse_version INTEGER',
}
INDEXES = {
 'token_metadata': [('mint', 'mint'), ('creator', 'creator,first_seen')],
 'arkm_funding': [('destination', 'destination,stamp'), ('source', 'source,stamp')],
 'pump_event_default': [('mint', 'mint,stamp,slot,outer_ix,inner_ix,ordinal')],
}


def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def used_bytes(base):
    total=0
    for p in Path(base).rglob('*'):
        try:
            if p.is_file():total+=p.stat().st_size
        except FileNotFoundError:pass  # Concurrent SQLite journal removal.
    return total


class CompactEvents:
    """Address dictionary + source-row locator; original COPY bytes are retained.

    Signatures are intentionally not duplicated in this research projection.
    A row is recoverable by (compressed source SHA, COPY row number). It is not
    represented as a transaction receipt until the original is reconstructed.
    """
    def __init__(self, db):
        self.db=db;self.cache={}
        db.executescript('''
          CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY,key BLOB UNIQUE);
          CREATE TABLE IF NOT EXISTS events(mint INTEGER,trader INTEGER,stamp INTEGER,slot INTEGER,
            outer_ix INTEGER,inner_ix INTEGER,ordinal INTEGER,side TEXT,sol INTEGER,tokens TEXT,
            decimals INTEGER,parse_version INTEGER);
          CREATE VIEW IF NOT EXISTS projection AS SELECT e.rowid AS rowid,m.key AS mint,t.key AS trader,
            NULL AS signature,e.stamp,e.slot,e.outer_ix,e.inner_ix,e.ordinal,e.side,e.sol,e.tokens,
            e.decimals,e.parse_version FROM events e LEFT JOIN keys m ON m.id=e.mint
            LEFT JOIN keys t ON t.id=e.trader;
        ''')
    def insert(self, batch):
        missing={v for r in batch for v in r[:2] if v is not None and v not in self.cache}
        self.db.executemany('INSERT OR IGNORE INTO keys(key) VALUES(?)',((v,) for v in missing))
        missing=list(missing)
        for start in range(0,len(missing),500):
            part=missing[start:start+500]
            for ident,key in self.db.execute('SELECT id,key FROM keys WHERE key IN ('+','.join('?' for _ in part)+')',part):self.cache[key]=ident
        self.db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ((self.cache.get(r[0]),self.cache.get(r[1]),*r[3:]) for r in batch))
        if len(self.cache)>131072:self.cache.clear()


class Scope:
    def __init__(self, base, grant):
        self.base, self.path = Path(base), Path(grant)
        self.raw = self.path.read_bytes()
        self.hash = hashlib.sha256(self.raw).hexdigest()
        self.grant = json.loads(self.raw)
        g = self.grant
        if (g['schema'] != 'economic-machine.pump-core-grant.v1' or
            g['additional_io_bytes'] != 12*GIB or g['total_storage_bytes'] != 32*GIB or
            g['maximum_seconds'] != 43200 or g['maximum_cpu_percent'] != 100 or
            g['trading'] is not False or g['paid_api'] is not False or
            g['new_model_calls'] != 0 or g['new_server'] is not False or
            not g['approved_at_ms'] < g['deadline_ms'] <= g['approved_at_ms']+43200000):
            raise ValueError('CORE_GRANT_SCOPE')
        self.out = self.path.parent
        self.db = sqlite3.connect(self.out/'io-extension.sqlite3')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY,bytes INTEGER,purpose TEXT,at REAL);')
        identities = self.db.execute('SELECT hash FROM identity').fetchall()
        if identities and identities != [(self.hash,)]:
            self.db.close()
            raise ValueError('CORE_GRANT_CHANGED')
        with self.db: self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)', (self.hash,))
        self.expansion=None
        try:
            from .research_extension import extension
            added=extension(self.base,self.hash,self.grant['deadline_ms'])
            self.storage_limit=added['total_storage_bytes'] if added else self.grant['total_storage_bytes']
            from .research_expansion import authority,ExpansionScope
            self.expansion=ExpansionScope(self.base,self.hash,self.grant['deadline_ms']) if authority(self.base,self.hash,self.grant['deadline_ms']) else None
            if self.expansion:self.storage_limit=self.expansion.grant['total_storage_bytes']
            self.continuation=self.expansion.continuation if self.expansion else None
            self.effective_deadline_ms=self.expansion.effective_deadline_ms if self.expansion else self.grant['deadline_ms']
            self.last_guard = 0
            self.guard(force=True)
        except BaseException:
            if self.expansion:self.expansion.close()
            self.db.close()
            raise

    def close(self):
        if self.expansion:self.expansion.close()
        self.db.close()

    def guard(self, extra=0, force=False):
        if self.expansion:
            self.expansion.guard(extra,force);self.used=self.expansion.used;return
        if time.time()*1000 >= self.grant['deadline_ms']: raise ValueError('CORE_DEADLINE')
        if force or time.monotonic()-self.last_guard > 2:
            # The entire research tree, including older evidence and leftovers.
            self.used = used_bytes(self.base)
            self.last_guard = time.monotonic()
        if self.used+extra+128*1024**2 > self.storage_limit:
            raise ValueError('CORE_TOTAL_STORAGE_LIMIT')
        if shutil.disk_usage(self.base).free-extra < 48*GIB:
            raise ValueError('CORE_HOST_FREE_SPACE')

    def reserved(self):
        old=self.db.execute('SELECT coalesce(sum(bytes),0) FROM reservations').fetchone()[0]
        return old+(self.expansion.reserved() if self.expansion else 0)

    def remaining_io(self):
        if self.expansion:return self.expansion.remaining_io()
        return self.grant['additional_io_bytes']-self.reserved()

    def reserve(self, size, purpose):
        if type(size) is not int or not 0 < size <= CHUNK: raise ValueError('CORE_REQUEST_BOUND')
        if self.expansion:return self.expansion.reserve(size,purpose)
        self.guard(size, force=True)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.reserved()+size > self.grant['additional_io_bytes']:
                raise ValueError('CORE_ADDITIONAL_IO_LIMIT')
            self.db.execute('INSERT INTO reservations(bytes,purpose,at) VALUES(?,?,?)', (size,purpose,time.time()))


class Chunks:
    def __init__(self, scope, parts):
        self.scope, self.parts = scope, parts
        self.folder = scope.out/'ranges';self.folder.mkdir(exist_ok=True)

    def part(self, part, start, size):
        url = part['download_url']
        if not re.fullmatch(r'https://ndownloader\.figshare\.com/files/[0-9]+', url):
            raise ValueError('CORE_SOURCE_HOST')
        ident = hashlib.sha256(f'{url}:{start}:{size}'.encode()).hexdigest()
        path, meta = self.folder/(ident+'.range'), self.folder/(ident+'.json')
        if path.exists() and meta.exists():
            info=json.loads(meta.read_bytes())
            if (info['url'] != url or info['start'] != start or info['bytes'] != size or
                path.stat().st_size != size or sha(path) != info['sha256']):
                raise ValueError('CORE_CACHE_CORRUPT')
            return path.read_bytes()
        if path.exists() or meta.exists(): raise ValueError('CORE_INCOMPLETE_CACHE_PRESERVED')
        self.scope.reserve(size,ident)
        req=urllib.request.Request(url,headers={'Range':f'bytes={start}-{start+size-1}',
            'Accept-Encoding':'identity','User-Agent':'Skew-Pump-Core-Research/1.0'})
        partial=path.with_suffix('.partial')
        if partial.exists(): raise ValueError('CORE_PARTIAL_PRESERVED')
        digest=hashlib.sha256()
        with urllib.request.urlopen(req,timeout=45) as r:
            if r.status != 206 or r.headers.get('Content-Range') != f'bytes {start}-{start+size-1}/{part["size"]}':
                raise ValueError('CORE_RANGE_NOT_HONORED')
            with partial.open('xb') as f:
                left=size
                while left:
                    self.scope.guard()
                    raw=r.read(min(left,1024**2))
                    if not raw:raise ValueError('CORE_RANGE_TRUNCATED')
                    f.write(raw);digest.update(raw);left-=len(raw)
                if r.read(1):raise ValueError('CORE_RANGE_OVERSIZE')
                f.flush();os.fsync(f.fileno())
            info=dict(url=url,start=start,bytes=size,sha256=digest.hexdigest(),at=time.time(),
                etag=r.headers.get('ETag'),whole_part_checksum_verified=False,chain_verified=False)
        # Commit manifest first; an interruption is an explicit recoverable error.
        atomic(meta,info);os.replace(partial,path)
        return path.read_bytes()

    def range(self, start, size):
        for p in self.parts:
            if start>=p['size']:start-=p['size'];continue
            while start<p['size'] and size:
                n=min(size,p['size']-start,CHUNK)
                yield self.part(p,start,n)
                start+=n;size-=n
            start=0
            if not size:return
        raise ValueError('CORE_RANGE_OUTSIDE_SOURCE')


class Stream:
    def __init__(self, chunks):
        self.chunks=iter(chunks);self.buffer=b'';self.pos=0;self.bytes=0
        self.digest=hashlib.sha256()

    def take(self,n):
        if not 0<=n<=4*1024**2:raise ValueError('CORE_BLOCK_BOUND')
        out=[]
        while n:
            if self.pos==len(self.buffer):
                try:self.buffer=next(self.chunks)
                except StopIteration:raise ValueError('CORE_TRUNCATED') from None
                self.pos=0;self.bytes+=len(self.buffer);self.digest.update(self.buffer)
            m=min(n,len(self.buffer)-self.pos)
            out.append(self.buffer[self.pos:self.pos+m]);self.pos+=m;n-=m
        return b''.join(out)

    def integer(self):
        raw=self.take(5)
        if raw[0] not in (0,1):raise ValueError('CORE_INTEGER_SIGN')
        return (-1 if raw[0] else 1)*int.from_bytes(raw[1:],'little')


def stream_rows(reader, entry, max_inflated=256*GIB):
    names=columns(entry)
    if reader.take(1)!=b'\x01' or reader.integer()!=entry['id']:raise ValueError('CORE_BLOCK_ID')
    z=zlib.decompressobj();pending=b'';inflated=0;end=False
    while True:
        n=reader.integer()
        if not n:break
        if not 0<n<=4*1024**2 or z.eof:raise ValueError('CORE_COMPRESSED_BLOCK')
        raw=reader.take(n)
        while raw:
            out=z.decompress(raw,1024**2);raw=z.unconsumed_tail;inflated+=len(out)
            if inflated>max_inflated or z.unused_data:raise ValueError('CORE_INFLATE_BOUND')
            lines=(pending+out).split(b'\n');pending=lines.pop()
            if len(pending)>8*1024**2:raise ValueError('CORE_LINE_BOUND')
            for line in lines:
                if len(line)>8*1024**2:raise ValueError('CORE_LINE_BOUND')
                if end:
                    if line:raise ValueError('CORE_DATA_AFTER_END')
                    continue
                if line==b'\\.':end=True;continue
                cells=line.split(b'\t')
                if len(cells)!=len(names):raise ValueError('CORE_COLUMN_COUNT')
                yield cells
    if not z.eof or pending or not end:raise ValueError('CORE_INCOMPLETE_COPY')
    if reader.pos!=len(reader.buffer) or next(reader.chunks,None) is not None:
        raise ValueError('CORE_TRAILING_BYTES')


def key(raw, size=32):
    v=copy_value(raw)
    if v is None:return None
    out=bytes.fromhex(v[2:]) if v.startswith('\\x') else decode(v)
    if len(out)!=size:raise ValueError('CORE_KEY_WIDTH')
    return out


def number(raw):
    if raw==b'\\N':return None
    if not re.fullmatch(rb'-?[0-9]{1,20}',raw):raise ValueError('CORE_INTEGER')
    n=int(raw)
    if not -(2**63)<=n<2**63:raise ValueError('CORE_INTEGER_OVERFLOW')
    return n


def project(name, cells, positions):
    v=lambda name:cells[positions[name]]
    k=lambda name:key(v(name))
    i=lambda name:number(v(name))
    t=lambda name:copy_value(v(name))
    if name=='token_metadata':
        return k('mint'),k('creator'),key(v('created_signature'),64),i('first_seen_block_time'),i('last_updated_block_time'),i('decimals')
    if name=='arkm_funding':
        labeled=lambda prefix:any(v(prefix+s) not in (b'\\N',b'',b'{}') for s in ('known_entity','entity_name','entity_id','arkm_label_entity_name','tags','owner_entity_name','owner_entity_label'))
        return k('addr_pubkey'),k('fund_addr_pubkey'),t('tx_hash'),i('block_timestamp'),i('block_number'),t('token_symbol'),t('token_unit_value'),int(labeled('fund_addr_')),int(labeled('addr_')),t('arkham_transfer_id')
    if name=='pump_event_default':
        return k('mint'),k('trader'),key(v('signature'),64),i('block_time'),i('slot'),i('outer_ix_index'),i('inner_ix_index'),i('event_ordinal'),t('side'),i('sol_amount_lamports'),t('token_amount_raw'),i('decimals'),i('parse_version')
    raise ValueError('CORE_TABLE_NOT_AUTHORIZED')


def extract(scope, source, entry, length, notify):
    name=entry['tag'];path=scope.out/(name+'.sqlite3')
    if name not in TABLES:raise ValueError('CORE_TABLE_NOT_AUTHORIZED')
    done=scope.out/(name+'-extraction.json')
    if path.exists():
        r=json.loads(done.read_bytes())
        if sha(path)!=r['projection_sha256']:raise ValueError('CORE_PROJECTION_HASH')
        return r
    partial=path.with_suffix('.partial')
    with closing(sqlite3.connect(partial)) as db:
        db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA temp_store=FILE');db.execute('PRAGMA cache_size=-32768');db.execute('PRAGMA threads=1')
        db.execute('PRAGMA max_page_count=3500000')
        compact=CompactEvents(db) if name=='pump_event_default' else None
        if not compact:db.execute('CREATE TABLE IF NOT EXISTS projection('+SCHEMAS[name]+')')
        db.execute('CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1),rows INTEGER,hash TEXT)')
        checkpoint=db.execute('SELECT rows,hash FROM checkpoint WHERE id=1').fetchone()
        committed=checkpoint[0] if checkpoint else 0
        target='events' if compact else 'projection'
        if db.execute('SELECT count(*) FROM '+target).fetchone()[0]!=committed:raise ValueError('CORE_RESUME_ROWS')
        reader=Stream(source.range(entry['offset'],length));positions={n:i for i,n in enumerate(columns(entry))}
        n=0;digest=hashlib.sha256();batch=[];started=time.time();last=0
        def commit():
            nonlocal last
            scope.guard(64*1024**2,force=True)
            with db:
                if compact:compact.insert(batch)
                else:db.executemany('INSERT INTO projection VALUES('+','.join('?' for _ in batch[0])+')',batch)
                db.execute('INSERT OR REPLACE INTO checkpoint VALUES(1,?,?)',(n,digest.hexdigest()))
            batch.clear()
            if time.monotonic()-last>10:
                notify(table=name,rows=n,compressed_consumed=reader.bytes,phase='PROJECTING');last=time.monotonic()
        for cells in stream_rows(reader,entry):
            digest.update(b'\t'.join(cells)+b'\n');n+=1
            if n<=committed:
                if n%10000==0:scope.guard()
                if n==committed and digest.hexdigest()!=checkpoint[1]:raise ValueError('CORE_RESUME_HASH')
                continue
            batch.append(project(name,cells,positions))
            if len(batch)>=10000:commit()
        if batch:commit()
        if n<committed:raise ValueError('CORE_RESUME_TRUNCATION')
        indexes=([('mint','mint,stamp,slot,outer_ix,inner_ix,ordinal'),('trader','trader,stamp,slot,outer_ix,inner_ix,ordinal')] if compact else INDEXES[name])
        for label,fields in indexes:
            scope.guard(256*1024**2,force=True)
            notify(table=name,rows=n,phase='INDEXING',index=label)
            db.set_progress_handler(lambda: (scope.guard() or 0),100000)
            db.execute('CREATE INDEX IF NOT EXISTS idx_'+label+' ON '+target+'('+fields+')');db.commit()
        result=dict(table=name,rows=n,started=started,finished=time.time(),compressed_bytes=reader.bytes,
            compressed_sha256=reader.digest.hexdigest(),copy_rows_sha256=digest.hexdigest(),
            schema=SCHEMAS[name],sql_from_archive_executed=False,chain_verified=False,grant_sha256=scope.hash)
        if compact:result.update(projection_format='dictionary-events-v2',signature_omitted_in_projection=True,
            source_locator='compressed_sha256 + one-based COPY row number (projection.rowid)',address_dictionary_rows=db.execute('SELECT count(*) FROM keys').fetchone()[0])
    result.update(projection_bytes=partial.stat().st_size,projection_sha256=sha(partial))
    atomic(done,result);os.replace(partial,path)
    return result
