"""Lossless, bounded CAS compression. Identity is always SHA256 of raw bytes.

Legacy files remain readable. An interrupted migration leaves either the raw
file, or an independently verified compressed replacement (sometimes both).
"""
import hashlib
import os
from pathlib import Path
import re
import zlib

MAX_RAW = 32 * 1024**2

def paths(root, digest):
    if not re.fullmatch('[0-9a-f]{64}', digest): raise ValueError('OBJECT_DIGEST')
    p=Path(root)/'objects'/digest
    return p,p.with_suffix('.z')

def unpack(data):
    d=zlib.decompressobj()
    try: raw=d.decompress(data,MAX_RAW+1)
    except zlib.error: raise ValueError('OBJECT_COMPRESSION') from None
    if len(raw)>MAX_RAW or not d.eof or d.unused_data or d.unconsumed_tail:
        raise ValueError('OBJECT_COMPRESSION_BOUND')
    return raw

def read(root,digest):
    plain,packed=paths(root,digest);p=packed if packed.exists() else plain
    if p.stat().st_size>MAX_RAW+65536:raise ValueError('OBJECT_SIZE_BOUND')
    raw=unpack(p.read_bytes()) if p==packed else p.read_bytes()
    if len(raw)>MAX_RAW or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('OBJECT_HASH_CONFLICT')
    return raw

def encode(raw):
    if len(raw)>MAX_RAW:raise ValueError('OBJECT_SIZE_BOUND')
    packed=zlib.compress(raw,1)
    return (packed,'deflate1') if len(packed)+32<len(raw) else (raw,'raw')

def sync_dir(root):
    fd=os.open(Path(root)/'objects',os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def write(root,digest,data,codec):
    plain,packed=paths(root,digest);target=packed if codec=='deflate1' else plain
    tmp=target.with_name(target.name+'.next')
    with tmp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(tmp,target);sync_dir(root)
    return target
