"""One bounded cold capsule cache; original logical offsets survive compression.

Uses Ticking's existing authenticated read-only FD broker. No cloud credentials,
uploads, deletion, restored plaintext files, or fall-through on corruption.
"""
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct
import zlib

HEADER=struct.Struct('<4sIQQI')
CLIENT_SHA256='5e16b91c64d68b792d0bdad444adcfe65e3350fe5921135659cb3c439a8ce758'
MAX_RAW=8*1024**2
MAX_CANONICAL=256*1024**2

def payload(magic,stored,crc):
    if magic==b'SLI1':raw=stored
    elif magic==b'SLZ1':
        d=zlib.decompressobj();raw=d.decompress(stored,MAX_RAW+1)
        if not d.eof or d.unused_data or d.unconsumed_tail:raise ValueError('TICKING_COMPRESSION_BOUND')
    else:raise ValueError('TICKING_MAGIC')
    if not 0<len(raw)<=MAX_RAW or zlib.crc32(raw)!=crc:raise ValueError('TICKING_CRC')
    return raw

class Originals:
    def __init__(self,root,reserve=None):
        self.root=Path(root).resolve();self.reserve=reserve
        self.segment=None;self.raw=None;self.frames={};self.cold_size=0
        self.hot_path=None;self.hot=None;self.client=None
        self.cold_loads=0;self.remote_requests=0

    def close(self):
        if self.hot is not None:self.hot.close()
        self.hot=None;self.hot_path=None;self.raw=None;self.frames={};self.segment=None

    def folder(self,segment):
        if not isinstance(segment,str) or not re.fullmatch('[0-9]{8}',segment):raise ValueError('TICKING_SEGMENT')
        p=self.root/'data/result-archive/originals'/segment
        if p.is_symlink():raise ValueError('TICKING_SEGMENT_SYMLINK')
        return p

    def _hot(self,segment):
        p=self.folder(segment)/'inputs.sli'
        if self.hot_path==p and self.hot is not None:return self.hot
        if self.hot is not None:self.hot.close()
        self.hot=None;self.hot_path=None
        try:fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        except FileNotFoundError:return None
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size>96*1024**2:
            os.close(fd);raise ValueError('TICKING_HOT_BOUND')
        self.hot=os.fdopen(fd,'rb');self.hot_path=p
        return self.hot

    def _client(self):
        if self.client is None:
            p=self.root/'blob_capsule.py'
            if hashlib.sha256(p.read_bytes()).hexdigest()!=CLIENT_SHA256:raise ValueError('TICKING_BLOB_CLIENT_CHANGED')
            spec=importlib.util.spec_from_file_location('_pump_verified_blob_client',p)
            mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);self.client=mod
        return self.client

    def _cold(self,segment):
        if self.segment==segment:return
        # Drop previous 256 MiB buffer before allocating the next one.
        self.raw=None;self.frames={};self.segment=None
        folder=self.folder(segment);meta=folder/'capsule.json'
        if not meta.is_file():raise ValueError('COLD_REPLAY_REQUIRED_PRESERVE_CURSOR')
        st=meta.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>16384:raise ValueError('TICKING_CAPSULE_MANIFEST_BOUND')
        env=json.loads(meta.read_bytes());v=env['value']
        canonical=json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        if (hashlib.sha256(canonical).hexdigest()!=env['sha256'] or v['schema']!=1 or
            v['codec']!='zstd-canonical-frames-v1' or v['segment']!=segment or
            not 0<v['canonicalBytes']<=MAX_CANONICAL or not 0<v['sourceBytes']<=96*1024**2 or
            not 0<v['frames']<=262144):raise ValueError('TICKING_CAPSULE_MANIFEST')
        client=self._client()
        if (folder/'inputs.slc').exists():
            st=(folder/'inputs.slc').lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or not 0<st.st_size<=64*1024**2:
                raise ValueError('TICKING_COMPRESSED_SIZE_BOUND')
        else:
            ref=client.reference(folder,v)
            if self.reserve is None:raise ValueError('COLD_NETWORK_RESERVATION_REQUIRED')
            # Charge the whole compressed object even on broker cache hit or
            # failed request. Never silently bypass the cumulative network cap.
            self.reserve(ref['bytes'],'ticking_blob_read');self.remote_requests+=1
        compressed=client.read_compressed(folder,v)
        if len(compressed)>64*1024**2 or hashlib.sha256(compressed).hexdigest()!=v['capsuleSHA256']:
            raise ValueError('TICKING_CAPSULE_HASH')
        lib=ctypes.CDLL('libzstd.so.1')
        lib.ZSTD_decompress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
        lib.ZSTD_decompress.restype=ctypes.c_size_t
        lib.ZSTD_findFrameCompressedSize.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
        lib.ZSTD_findFrameCompressedSize.restype=ctypes.c_size_t
        if lib.ZSTD_findFrameCompressedSize(compressed,len(compressed))!=len(compressed):raise ValueError('TICKING_CAPSULE_FRAME_BOUND')
        output=ctypes.create_string_buffer(v['canonicalBytes'])
        if lib.ZSTD_decompress(output,len(output),compressed,len(compressed))!=len(output):raise ValueError('TICKING_CAPSULE_DECOMPRESSION')
        raw=memoryview(output).cast('B')
        if hashlib.sha256(raw).hexdigest()!=v['canonicalSHA256']:raise ValueError('TICKING_CANONICAL_HASH')
        pos=logical=sequence=0;frames={}
        while pos<len(raw):
            if pos+32>len(raw):raise ValueError('TICKING_CAPSULE_TORN')
            magic,n,seq,stamp,crc=HEADER.unpack_from(raw,pos);length=struct.unpack_from('<I',raw,pos+28)[0]
            if (magic not in (b'SLI1',b'SLZ1') or not 0<n<=MAX_RAW or not 0<length<=MAX_RAW or
                pos+32+length>len(raw) or seq!=sequence+1):raise ValueError('TICKING_CAPSULE_ACCOUNTING')
            end=logical+HEADER.size+n
            if zlib.crc32(raw[pos+32:pos+32+length])!=crc:raise ValueError('TICKING_CRC')
            frames[logical]=(pos+32,length,end,seq,stamp)
            logical=end;pos+=32+length;sequence=seq
        if logical!=v['sourceBytes'] or sequence!=v['frames']:raise ValueError('TICKING_CAPSULE_FINAL_ACCOUNTING')
        self.raw=raw;self.frames=frames;self.cold_size=logical;self.segment=segment;self.cold_loads+=1

    def size(self,segment):
        f=self._hot(segment)
        if f is not None:return os.fstat(f.fileno()).st_size
        self._cold(segment);return self.cold_size

    def frame(self,segment,offset,limit):
        if type(offset) is not int or type(limit) is not int or not 0<=offset<limit<=96*1024**2:
            raise ValueError('TICKING_CURSOR_BOUND')
        f=self._hot(segment)
        if f is not None:
            f.seek(offset);head=f.read(HEADER.size)
            if len(head)!=HEADER.size:raise ValueError('TICKING_TORN_HEADER')
            magic,n,seq,stamp,crc=HEADER.unpack(head);end=offset+HEADER.size+n
            if not 0<n<=MAX_RAW or end>limit:raise ValueError('TICKING_FRAME_BOUNDARY')
            stored=f.read(n)
            if len(stored)!=n:raise ValueError('TICKING_TORN_BODY')
            return payload(magic,stored,crc),seq,stamp,end
        self._cold(segment)
        if limit>self.cold_size or offset not in self.frames:raise ValueError('TICKING_COLD_CURSOR')
        pos,length,end,seq,stamp=self.frames[offset]
        if end>limit:raise ValueError('TICKING_FRAME_BOUNDARY')
        return bytes(self.raw[pos:pos+length]),seq,stamp,end

    def read_reference(self,ref,digest,length):
        raw,seq,_,end=self.frame(ref['segment'],ref['start'],ref['end'])
        if seq!=ref['sequence'] or end!=ref['end'] or len(raw)!=length or hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('TICKING_REFERENCE_HASH_OR_POSITION')
        return raw
