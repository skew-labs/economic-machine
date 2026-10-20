"""Count provider signature-index entries from bounded structural metadata.

Format pinned to rpcpool/yellowstone-faithful 9ca9ec15983c61efb83b1a839d2fe2384875e408.
Index cardinality is not independent proof that every canonical transaction is
present. It supplies a candidate workload denominator, including vote entries.
"""
import struct
from .gsfa_index import _meta

MAX_TABLE_BYTES=4*1024**2


def header(raw, *, epoch, root_cid):
    if len(raw)<25 or raw[:8]!=b'compiszd':raise ValueError('SIGCOUNT_MAGIC')
    rest,=struct.unpack_from('<I',raw,8);size=12+rest
    if not 13<=rest<=65524 or size>len(raw):raise ValueError('SIGCOUNT_HEADER_BOUND')
    value,buckets=struct.unpack_from('<QI',raw,12)
    if value!=36 or raw[24]!=1 or not 0<buckets<=MAX_TABLE_BYTES//16:raise ValueError('SIGCOUNT_LAYOUT')
    meta,end=_meta(raw[:size],25)
    if end!=size or set(meta)!={b'epoch',b'rootCid',b'network',b'kind'}:raise ValueError('SIGCOUNT_METADATA_KEYS')
    if (meta[b'epoch']!=epoch.to_bytes(8,'little') or meta[b'rootCid']!=root_cid
        or meta[b'network']!=b'mainnet' or meta[b'kind']!=b'sig-to-cid'):
        raise ValueError('SIGCOUNT_METADATA_IDENTITY')
    return dict(header_bytes=size,value_bytes=value,buckets=buckets,bucket_table_bytes=buckets*16,
                record_bytes=39,epoch=epoch,root_cid_hex=root_cid.hex())


def count_entries(h,table,source_bytes):
    if len(table)!=h['bucket_table_bytes'] or len(table)>MAX_TABLE_BYTES:raise ValueError('SIGCOUNT_TABLE_BOUND')
    expected=h['header_bytes']+len(table);count=0
    for pos in range(0,len(table),16):
        entries,=struct.unpack_from('<I',table,pos+4)
        hashlen,reserved=table[pos+8:pos+10]
        offset=int.from_bytes(table[pos+10:pos+16],'little')
        if entries>1<<24 or hashlen!=3 or reserved!=0 or offset!=expected:
            raise ValueError('SIGCOUNT_BUCKET_LAYOUT')
        count+=entries;expected+=entries*h['record_bytes']
    if expected!=source_bytes:raise ValueError('SIGCOUNT_FINAL_SIZE')
    return dict(**h,provider_signature_entries=count,source_index_bytes=source_bytes,
        full_table_offsets_and_size_consistent=True,whole_index_hash_verified=False,
        transaction_records_replayed=False,complete_canonical_history_proven=False)
