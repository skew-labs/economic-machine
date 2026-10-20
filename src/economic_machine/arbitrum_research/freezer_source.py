"""Seek bounded ancient records in a pinned multipart Nitro snapshot.

The index is an address into publisher data, not consensus evidence. Successful
decoding must verify header, transaction and receipt roots before graph admission.
"""
import hashlib
import fcntl
import json
from pathlib import Path
import sqlite3
import struct
import time
import uuid
from economic_machine.arbitrum_research.snapshot_ranges import HTTP, split_range, tar_header
from economic_machine.pump_research.archive_gate import ArchiveGate
from economic_machine.pump_research.store import canonical

DEADLINE=1792351694298
TABLES={'headers':'c','bodies':'c','receipts':'c','hashes':'r'}

def table_prefix(files):
    prefixes=[p[:-len('/headers.cidx')] for p in files
        if p.endswith(('/ancient/headers.cidx','/ancient/chain/headers.cidx'))]
    if len(prefixes)!=1:raise ValueError('FREEZER_TABLE_LOCATION_PENDING')
    return prefixes[0]

def segments(rows,start,size):
    if type(size) is not int or not 0<size<=8*1024**2:raise ValueError('FREEZER_SEGMENT_BOUND')
    result=[]
    for offset in range(start,start+size,1024**2):
        for row,relative,n in split_range(rows,offset,min(1024**2,start+size-offset)):
            if result and result[-1][0]==row and result[-1][1]+result[-1][2]==relative:
                old=result[-1];result[-1]=(row,old[1],old[2]+n)
            else:result.append((row,relative,n))
    return result

def metadata(raw):
    # Metadata is an RLP list of two or three small unsigned integers.
    if not raw or len(raw)>64 or not 0xc0<=raw[0]<=0xf7 or raw[0]-0xc0!=len(raw)-1:
        raise ValueError('FREEZER_META_FORMAT')
    items=[];pos=1
    while pos<len(raw):
        tag=raw[pos];pos+=1
        if tag==0:raise ValueError('FREEZER_META_INTEGER')
        if tag<0x80: value=tag
        elif 0x80<=tag<=0x88:
            size=tag-0x80;part=raw[pos:pos+size];pos+=size
            if len(part)!=size or (size and (part[0]==0 or size==1 and part[0]<128)):
                raise ValueError('FREEZER_META_INTEGER')
            value=int.from_bytes(part,'big')
        else:raise ValueError('FREEZER_META_INTEGER')
        items.append(value)
    if not items or (items[0]==1 and len(items)!=2) or (items[0]==2 and len(items)!=3) or items[0] not in (1,2):
        raise ValueError('FREEZER_META_VERSION')
    return dict(version=items[0],tail=items[1],flush=items[2] if len(items)==3 else None)

def bounds(raw,*,first_item=False):
    if len(raw)<12 or len(raw)%6:raise ValueError('FREEZER_INDEX_SIZE')
    entries=list(struct.iter_unpack('>HI',raw))
    if first_item:entries[0]=(entries[0][0],0)
    output=[]
    for a,b in zip(entries,entries[1:]):
        if b[0]<a[0] or b[0]>a[0]+1:raise ValueError('FREEZER_FILE_ORDER')
        start=a[1] if a[0]==b[0] else 0;size=b[1]-start
        if size<0 or size>16*1024**2:raise ValueError('FREEZER_RECORD_BOUND')
        output.append((b[0],start,size))
    return output

class Snapshot:
    def __init__(self,catalogue,out,cloud,*,source_limit=1024**3,read_ahead=False):
        self.catalogue=Path(catalogue);self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.reader_lock=(self.out/'reader.lock').open('a+b');fcntl.flock(self.reader_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        (self.out/'ranges').mkdir(exist_ok=True);self.cloud=cloud;self.limit=source_limit
        cat=sqlite3.connect('file:'+str(self.catalogue/'runtime.sqlite3')+'?mode=ro',uri=True)
        self.identity=json.loads(cat.execute('select body from identity').fetchone()[0])
        self.parts=self.identity['parts'];self.total=sum(p['bytes'] for p in self.parts)
        self.files={}
        for row in cat.execute('select raw,body from headers'):
            entry=json.loads(row[1])
            if entry.get('end_marker'):continue
            if tar_header(bytes(row[0]),entry['header_offset'],self.total)!=entry:raise ValueError('FREEZER_CATALOGUE_HEADER')
            if entry['name'] in self.files:raise ValueError('FREEZER_DUPLICATE_PATH')
            self.files[entry['name']]=entry
        self.pins={u:json.loads(b) for u,b in cat.execute('select url,body from sources')};cat.close()
        self.prefix=table_prefix(self.files);self.http=HTTP();self.calls=0;self.network_bytes=0
        self.db=sqlite3.connect(self.out/'source.sqlite3',timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,bytes INTEGER,state TEXT);
        CREATE TABLE IF NOT EXISTS ranges(id TEXT PRIMARY KEY,start INTEGER,bytes INTEGER,sha256 TEXT,proof TEXT);
        CREATE TABLE IF NOT EXISTS sources(url TEXT PRIMARY KEY,body TEXT);''')
        identity=canonical(dict(source=self.identity,deadline_ms=DEADLINE,source_limit=source_limit))
        old=self.db.execute('select body from identity').fetchall()
        if old and old!=[(identity,)]:raise ValueError('FREEZER_IDENTITY_CHANGED')
        with self.db:self.db.execute('insert or ignore into identity values(?)',(identity,))
        self.last_refs=[];self.read_ahead=read_ahead
    def guard(self,n=0):
        if time.time_ns()//1000000>=DEADLINE:raise ValueError('FREEZER_DEADLINE')
        if self.db.execute('select coalesce(sum(bytes),0) from attempts').fetchone()[0]+n>self.limit:raise ValueError('FREEZER_SOURCE_LIMIT')
        self.cloud.guard_local(max(n*2,1024**2))
    def read(self,start,size):
        self.guard()
        if size<0 or size>32*1024**2:raise ValueError('FREEZER_READ_BOUND')
        if not size:return b''
        output=[]
        for offset in range(start,start+size,8*1024**2):
            length=min(8*1024**2,start+size-offset)
            for part,relative,n in segments(self.parts,offset,length):
                absolute=part['offset']+relative;key=hashlib.sha256(f'{absolute}:{n}'.encode()).hexdigest();path=self.out/'ranges'/(key+'.raw')
                row=self.db.execute('select sha256,proof from ranges where id=?',(key,)).fetchone()
                if row:
                    if not path.exists() and self.db.execute("select 1 from sqlite_master where type='table' and name='portable_ranges'").fetchone():
                        from .freezer_range_store import PortableRanges
                        if not hasattr(self,'portable'):self.portable=PortableRanges(self.out,self.db,self.cloud)
                        data=self.portable.read(key,absolute,n,row[0],json.loads(row[1]))
                    else:data=path.read_bytes()
                    if len(data)!=n or hashlib.sha256(data).hexdigest()!=row[0]:raise ValueError('FREEZER_CACHE_CHANGED')
                else:
                    with ArchiveGate(self.catalogue/'source-gate',self.guard,wait=True):
                        ids=[]
                        def reserve():
                            self.guard(n);i=uuid.uuid4().hex;ids.append(i)
                            with self.db:self.db.execute('insert into attempts values(?,?,?)',(i,n,'reserved'))
                            self.cloud.spend.reserve('read','freezer-'+i,bytes_=n)
                        reserve();pinrow=self.db.execute('select body from sources where url=?',(part['url'],)).fetchone()
                        pin=json.loads(pinrow[0]) if pinrow else self.pins.get(part['url'])
                        data,source=self.http.read(part,relative,n,pin,before_redirect=reserve,reuse_redirect=True)
                    # The provider lease protects the complete source response.
                    # Blob work uses its own admission and must not hold that lease.
                    digest=hashlib.sha256(data).hexdigest()
                    if path.exists() and path.read_bytes()!=data:raise ValueError('FREEZER_PARTIAL_CHANGED')
                    if not path.exists():
                        import os
                        with path.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
                    proof=self.cloud.upload(path)
                    with self.db:
                        self.db.execute('insert into ranges values(?,?,?,?,?)',(key,absolute,n,digest,canonical(proof)))
                        self.db.execute('insert or ignore into sources values(?,?)',(part['url'],canonical(source)))
                        self.db.executemany("update attempts set state='received' where id=?",[(i,) for i in ids])
                    self.calls+=len(ids);self.network_bytes+=n
                    row=(digest,canonical(proof))
                self.last_refs.append(dict(offset=absolute,bytes=n,sha256=row[0],cloud=json.loads(row[1])));output.append(data)
        return b''.join(output)
    def member(self,name,offset,size):
        path=self.prefix+'/'+name
        if path not in self.files:raise ValueError('FREEZER_MEMBER_PENDING:'+name)
        entry=self.files[path]
        if entry['directory'] or offset<0 or size<0 or offset+size>entry['bytes']:raise ValueError('FREEZER_MEMBER_RANGE')
        if getattr(self,'read_ahead',False) and size and not name.endswith('.meta'):
            # Fixed aligned ranges are reused by consecutive windows. No reads
            # outside a verified tar member, and every prefetched byte is charged.
            unit=256*1024 if name.endswith(('idx',)) else 8*1024**2
            pieces=[];end=offset+size
            for begin in range(offset//unit*unit,end,unit):
                raw=self.read(entry['data_offset']+begin,min(unit,entry['bytes']-begin))
                pieces.append(raw[max(offset-begin,0):min(end-begin,len(raw))])
            return b''.join(pieces)
        return self.read(entry['data_offset']+offset,size)
    def table(self,name,start,count):
        if name not in TABLES or type(start) is not int or start<0 or type(count) is not int or not 1<=count<=16384:
            raise ValueError('FREEZER_TABLE_SCOPE')
        ext=TABLES[name];index=name+'.'+ext+'idx';entry=self.files.get(self.prefix+'/'+index)
        if not entry:raise ValueError('FREEZER_MEMBER_PENDING:'+index)
        first=self.member(index,0,6);file,removed=struct.unpack('>HI',first)
        mentry=self.files.get(self.prefix+'/'+name+'.meta')
        if not mentry or mentry['bytes']>64:raise ValueError('FREEZER_META_MISSING')
        meta=metadata(self.member(name+'.meta',0,mentry['bytes']))
        stop=(start-removed+count+1)*6
        if start<max(removed,meta['tail']) or entry['bytes']%6 or stop>entry['bytes'] or meta['flush'] is not None and stop>meta['flush']:
            raise ValueError('FREEZER_HISTORY_OUTSIDE')
        positions=bounds(self.member(index,(start-removed)*6,(count+1)*6),first_item=start==removed)
        records=[None]*count;i=0
        while i<count:
            file,offset,size=positions[i];end=offset+size;j=i+1
            while j<count and positions[j][0]==file and positions[j][1]==end and end+positions[j][2]-offset<=32*1024**2:
                end+=positions[j][2];j+=1
            raw=self.member(f'{name}.{file:04d}.{ext}dat',offset,end-offset)
            for k in range(i,j):
                _,pos,n=positions[k];records[k]=raw[pos-offset:pos-offset+n]
            i=j
        return records
    def close(self):self.http.close();self.db.close();self.reader_lock.close()
