"""Old Faithful sparse HTTP ranges; no archive RPC subscription or full CAR copy.

Hash/link integrity is distinct from independent Solana consensus finality.
"""
import ctypes
import hashlib
import io
import json
import re
import struct
import time
import urllib.request
import cbor2
from .store import canonical, encode
from .wire import varint, raw_transaction, extract

BASE='https://files.old-faithful.net'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**kw): return None

def get(url, maximum, byte_range=None):
    if not url.startswith((BASE+'/', 'https://frontend-api-v3.pump.fun/coins')): raise ValueError('SOURCE_NOT_ALLOWED')
    headers={'User-Agent':'EconomicMachineResearch/0.1','Accept-Encoding':'identity'}
    if byte_range: headers['Range']=f'bytes={byte_range[0]}-{byte_range[1]}'
    with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url,headers=headers),timeout=20) as r:
        if byte_range:
            expected=f'bytes {byte_range[0]}-{byte_range[1]}/'
            if r.status!=206 or not r.headers.get('Content-Range','').startswith(expected): raise ValueError('RANGE_NOT_HONORED')
        raw=r.read(maximum+1)
    if len(raw)>maximum or (byte_range and len(raw)!=byte_range[1]-byte_range[0]+1): raise ValueError('RESPONSE_BYTE_BOUND')
    return raw

def nodes(raw):
    out={};pos=0
    while pos<len(raw):
        n,pos=varint(raw,pos)
        if not 36<n<=8*1024**2 or pos+n>len(raw): raise ValueError('CAR_SECTION_BOUND')
        section=raw[pos:pos+n];pos+=n;q=0;cid_fields=[]
        for _ in range(4): v,q=varint(section,q);cid_fields.append(v)
        if cid_fields!=[1,0x71,0x12,32]: raise ValueError('UNSUPPORTED_CID')
        cid=section[:q+32];digest=section[q:q+32];body=section[q+32:]
        if hashlib.sha256(body).digest()!=digest: raise ValueError('CAR_CID_MISMATCH')
        stream=io.BytesIO(body);value=cbor2.CBORDecoder(stream).decode()
        if stream.tell()!=len(body) or not isinstance(value,list) or not value: raise ValueError('CBOR_NODE')
        if cid in out and out[cid]!=value: raise ValueError('CID_CONFLICT')
        out[cid]=value
        if len(out)>100000: raise ValueError('CAR_NODE_BOUND')
    return out

def link(v):
    if not isinstance(v,cbor2.CBORTag) or v.tag!=42 or not isinstance(v.value,bytes) or v.value[0]!=0: raise ValueError('CID_LINK')
    return v.value[1:]

CRC_TABLE=[]
for b in range(256):
    n=b
    for _ in range(8): n=(n>>1)^(0xd800000000000000 if n&1 else 0)
    CRC_TABLE.append(n)

def checksum(raw):
    c=0xffffffffffffffff
    for b in raw: c=CRC_TABLE[(c^b)&255]^(c>>8)
    return c^0xffffffffffffffff

def dataframe(frame, index):
    pending=[frame];parts={};visited=set();total=None;expected=frame[1] if len(frame)>1 else None
    while pending:
        f=pending.pop(0)
        if not isinstance(f,list) or len(f)<5 or f[0]!=6 or not isinstance(f[4],bytes): raise ValueError('DATAFRAME_SHAPE')
        i=f[2] if f[2] is not None else 0
        if i in parts: raise ValueError('DATAFRAME_DUPLICATE')
        parts[i]=f[4]
        if f[3] is not None:
            if total is not None and total!=f[3]: raise ValueError('DATAFRAME_TOTAL')
            total=f[3]
        for tag in (f[5] if len(f)>5 else None) or []:
            cid=link(tag)
            if cid in visited or cid not in index: raise ValueError('DATAFRAME_LINK')
            visited.add(cid);pending.append(index[cid])
        if len(parts)>1024 or sum(map(len,parts.values()))>8*1024**2: raise ValueError('DATAFRAME_BOUND')
    if sorted(parts)!=list(range(len(parts))) or (total is not None and total!=len(parts)): raise ValueError('DATAFRAME_GAP')
    raw=b''.join(parts[i] for i in sorted(parts))
    if expected is not None and checksum(raw)!=expected:
        fnv=14695981039346656037
        for b in raw: fnv=((fnv^b)*1099511628211)&0xffffffffffffffff
        if fnv!=expected: raise ValueError('DATAFRAME_CHECKSUM')
    return raw

def decompress(raw):
    if not raw.startswith(b'\x28\xb5\x2f\xfd'): raise ValueError('METADATA_COMPRESSION')
    lib=ctypes.CDLL('libzstd.so.1')
    lib.ZSTD_decompress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
    lib.ZSTD_decompress.restype=ctypes.c_size_t
    out=ctypes.create_string_buffer(8*1024**2)
    n=lib.ZSTD_decompress(out,len(out),raw,len(raw))
    if not 0<n<=len(out): raise ValueError('METADATA_DECOMPRESSION_BOUND')
    return out.raw[:n]

def parse_block(raw, slot, meta_type, signatures=None):
    index=nodes(raw);blocks=[n for n in index.values() if n[0]==2]
    if len(blocks)!=1 or blocks[0][1]!=slot: raise ValueError('BLOCK_SLOT_MISMATCH')
    block=blocks[0];txs=[];errors=[];seen=set()
    for entry_link in block[3]:
        entry=index[link(entry_link)]
        if entry[0]!=1: raise ValueError('ENTRY_LINK_TYPE')
        for tx_link in entry[3]:
            cid=link(tx_link);n=index[cid]
            if cid in seen or n[0]!=0 or n[3]!=slot: raise ValueError('TRANSACTION_BLOCK_MEMBERSHIP')
            seen.add(cid)
            try:
                wire=dataframe(n[1],index)
                if signatures is not None:
                    count,offset=varint(wire)
                    if not 1<=count<=64 or offset+64*count>=len(wire):raise ValueError('SIGNATURE_COUNT')
                    if encode(wire[offset:offset+64]) not in signatures:continue
                signature,keys,ix=raw_transaction(wire)
                meta_raw=dataframe(n[2],index)
                if not meta_raw: raise ValueError('MISSING_ARCHIVE_METADATA')
                meta=meta_type.FromString(decompress(meta_raw))
                txs.append(extract(signature,keys,ix,meta,slot,n[4] if len(n)>4 and n[4] is not None else len(seen)-1))
            except (ValueError,IndexError,KeyError) as e: errors.append(str(e)[:100])
    return dict(slot=slot,time=block[4][1],transactions=txs,decode_errors=errors,node_count=len(index),transaction_nodes=len(seen))

class Archive:
    def __init__(self, store, meta_type, max_bytes=1024**3):
        self.s=store;self.meta_type=meta_type;self.max_bytes=max_bytes;self.bytes=0

    def request(self, url, start, end):
        length=end-start+1
        if not 0<length<=32*1024**2 or self.bytes+length>self.max_bytes: raise ValueError('ARCHIVE_RUN_BYTE_LIMIT')
        # Reserve before dispatch so failed/unknown fetches consume the budget.
        self.bytes+=length
        from .io_budget import reserve
        total=reserve(self.s,length,'old_faithful_range')
        with self.s.db:self.s.set_state('archive_network_reserved',total)
        raw=get(url,length,(start,end));time.sleep(.03)
        return raw

    def read_slot(self, slot):
        if type(slot) is not int or not 240000000<=slot<=1000000000: raise ValueError('SLOT_BOUND')
        epoch=slot//432000;offset=(slot%432000)*12
        ix=self.request(f'{BASE}/{epoch}/epoch-{epoch}-slot-ranges.raw',offset,offset+11)
        pos,size=struct.unpack('<QI',ix)
        if size==0: return None,None
        raw=self.request(f'{BASE}/{epoch}/epoch-{epoch}.car',pos,pos+size-1)
        parsed=parse_block(raw,slot,self.meta_type)
        return parsed,dict(index=ix.hex(),car_offset=pos,car_size=size,car_sha256=hashlib.sha256(raw).hexdigest(),raw=raw)

    def locate_time(self, timestamp, low=240000000, high=456000000):
        # Timestamp-guided search narrows the range, but every requested slot
        # must independently match its CAR block. This is not an RPC timestamp.
        if not 1705000000<=timestamp<=time.time(): raise ValueError('LAUNCH_TIME_BOUND')
        samples=[]
        while high-low>4:
            mid=(low+high)//2;block=None
            for step in range(4):
                block,_=self.read_slot(mid+step)
                if block: break
            if not block: raise ValueError('TIME_SEARCH_MISSING_RANGE')
            samples.append([block['slot'],block['time']])
            if block['time']<timestamp: low=block['slot']+1
            else: high=mid
        return low,samples

    def next_indexed_slot(self, start, end):
        """Bounded sparse-index search; zero entries are provider gaps, not finality."""
        if (type(start) is not int or type(end) is not int or
            not 240000000<=start<=end<=1000000000 or end-start>=4096):
            raise ValueError('INDEX_SEARCH_BOUND')
        slot=start
        while slot<=end:
            epoch=slot//432000
            last=min(end,(epoch+1)*432000-1,slot+255)
            offset=(slot%432000)*12
            raw=self.request(f'{BASE}/{epoch}/epoch-{epoch}-slot-ranges.raw',offset,offset+(last-slot+1)*12-1)
            if len(raw)!=(last-slot+1)*12:raise ValueError('INDEX_SEARCH_TRUNCATED')
            for i in range(last-slot+1):
                _,size=struct.unpack_from('<QI',raw,i*12)
                if size:return slot+i
            slot=last+1
        return None

    def ingest_slot(self, slot):
        block,receipt=self.read_slot(slot)
        with self.s.db:
            if block is None:
                self.s.db.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)',('old-faithful',slot,slot,'provider_index_absent',canonical({'not_proven_skipped':True}),time.time()))
                return dict(slot=slot,absent=True)
            eid=self.s.evidence('old-faithful',f'slot:{slot}',receipt.pop('raw'))
            watches=self.s.watch_roots()
            for tx in block['transactions']:
                self.s.ingest(tx,eid,'old-faithful','archive_content_verified')
                self.s.expand_watch(tx,eid,watches)
            details=dict(receipt,transactions=len(block['transactions']),decode_errors=block['decode_errors'],content_hashes_verified=True,independent_consensus_verified=False)
            self.s.db.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)',('old-faithful',slot,slot,'decoded' if not block['decode_errors'] else 'partial_decode',canonical(details),time.time()))
        return dict(slot=slot,transactions=len(block['transactions']),errors=len(block['decode_errors']),time=block['time'])
