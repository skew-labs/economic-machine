"""Reuse a bounded Zstandard buffer and copy only the decoded output bytes.

The reference decoder allocates AND copies 8 MiB for every transaction. This
path retains its output ceiling without those per-record copies. CID/signature
and instruction-commit checks still use the original address-index parser.
"""
import ctypes
import threading

MAX_METADATA=8*1024**2
_thread=threading.local()


def decompress(raw):
    if not raw.startswith(b'\x28\xb5\x2f\xfd') or len(raw)>MAX_METADATA:
        raise ValueError('METADATA_COMPRESSION')
    if not hasattr(_thread,'lib'):
        lib=ctypes.CDLL('libzstd.so.1')
        lib.ZSTD_decompress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
        lib.ZSTD_decompress.restype=ctypes.c_size_t
        lib.ZSTD_getFrameContentSize.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
        lib.ZSTD_getFrameContentSize.restype=ctypes.c_ulonglong
        _thread.lib=lib;_thread.buffer=ctypes.create_string_buffer(MAX_METADATA)
    lib=_thread.lib;out=_thread.buffer
    declared=lib.ZSTD_getFrameContentSize(raw,len(raw))
    if declared!=2**64-1 and not 0<declared<=MAX_METADATA:
        raise ValueError('METADATA_DECOMPRESSION_BOUND')
    n=lib.ZSTD_decompress(out,MAX_METADATA,raw,len(raw))
    if not 0<n<=MAX_METADATA or declared!=2**64-1 and n!=declared:
        raise ValueError('METADATA_DECOMPRESSION_BOUND')
    return ctypes.string_at(out,n)
