"""Independent Ticking address index consumer, hot/capsule/Blob originals.

Starts at a declared retained segment, not at genesis. Each durable cursor is
separate from prior research. Slot-dead messages revoke matching stream rows.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import time
from .address_index import AddressIndex
from .cli import proto
from .history_core import Scope
from .originals import Originals
from .store import atomic,encode
from .wire import from_geyser
from .precision import enrich_transaction


def consume(index,reader,pb,root,cursor,scope,frames=400):
    head=json.loads((root/'data/result-archive/durable-tail.json').read_bytes());n=0
    if cursor is None:
        cursor=dict(segment=head['segment'],offset=0,started_ms=time.time_ns()//1000000,frames=0,transactions=0,
            origin=dict(segment=head['segment'],offset=0),history_before_origin_indexed=False,errors=0,last_slot=None)
    while n<frames:
        scope.guard(4*1024**2)
        if cursor['segment']>head['segment']:raise ValueError('INDEX_CURSOR_AHEAD')
        limit=head['offset'] if cursor['segment']==head['segment'] else reader.size(cursor['segment'])
        if cursor['offset']>limit:raise ValueError('INDEX_CURSOR_BEYOND_END')
        if cursor['offset']==limit:
            if cursor['segment']==head['segment']:break
            cursor['segment']=f"{int(cursor['segment'])+1:08d}";cursor['offset']=0;continue
        start=cursor['offset'];raw,seq,observed_ns,end=reader.frame(cursor['segment'],start,limit)
        update=pb.SubscribeUpdate.FromString(raw);kind=update.WhichOneof('update_oneof');infos=[];stamp=None
        if kind=='transaction':infos=[(update.transaction.transaction,update.transaction.slot)]
        elif kind=='block':
            infos=[(t,update.block.slot) for t in update.block.transactions]
            if update.block.HasField('block_time'):stamp=update.block.block_time.timestamp
        elif kind=='slot' and update.slot.status==pb.SLOT_DEAD:
            index.dead_slot(update.slot.slot)
        by_slot={}
        for info,slot in infos:
            if info.is_vote:continue
            try:
                tx=from_geyser(info,slot)
                tx['account_keys']=[encode(bytes(k)) for k in [*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses]]
                enrich_transaction(tx,info.meta)
                by_slot.setdefault(slot,[]).append(tx)
            except (ValueError,IndexError):cursor['errors']+=1
        for slot,txs in by_slot.items():
            block=dict(slot=slot,time=stamp,transactions=txs,transaction_nodes=len(txs),decode_errors=[])
            receipt=dict(car_sha256=hashlib.sha256(raw).hexdigest(),source='ticking_retained',
                locator=dict(segment=cursor['segment'],start=start,end=end,sequence=seq),
                coverage_state='filtered_processed_stream',whole_block=False,transaction_signatures_verified=False,
                independent_consensus_verified=False,receive_timestamp_is_not_block_time=True)
            if index.ingest(block,receipt):cursor['transactions']+=len(txs)
            cursor['last_slot']=slot
        cursor['offset']=end;cursor['frames']+=1;n+=1
    cursor['caught_up_to_sampled_head']=(cursor['segment'],cursor['offset'])==(head['segment'],head['offset'])
    cursor['updated_ms']=time.time_ns()//1000000;return cursor


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--ticking',required=True);p.add_argument('--once',action='store_true');a=p.parse_args()
    base=Path(a.base);out=base/'evidence/wallet-map-v1';root=Path(a.ticking)
    lock=(out/'index-tail.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scope=Scope(base,base/'evidence/core-trace-v1/grant.json');index=AddressIndex(out/'address-index.sqlite3')
    def reserve(amount,purpose):
        # A cold capsule may exceed a single HTTP range. Reserve its full size
        # in chunks BEFORE Originals obtains a broker file descriptor.
        cached=sum(p.stat().st_size for p in (scope.out/'ranges').glob('*.range'))
        remaining=scope.remaining_io()
        if remaining is not None and amount+max(0,9139504504-cached)+64*1024**2>remaining:raise ValueError('INDEX_IO_RESERVED_FOR_CORE')
        while amount:
            n=min(amount,16*1024**2);scope.reserve(n,purpose);amount-=n
    reader=Originals(root,reserve);pb,_=proto(root);path=out/'index-tail-cursor.json'
    cursor=json.loads(path.read_bytes()) if path.exists() else None;stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    try:
        while not stopping:
            scope.guard(force=True);cursor=consume(index,reader,pb,root,cursor,scope)
            atomic(path,cursor);atomic(out/'index-tail-status.json',dict(state='RUNNING',cursor=cursor,complete_history=False,orders_submitted=0))
            if a.once:break
            for _ in range(2):
                if stopping:break
                time.sleep(1)
        if stopping:atomic(out/'index-tail-status.json',dict(state='STOPPED',cursor=cursor))
    except BaseException as e:
        atomic(out/'index-tail-status.json',dict(state='STOPPED_ERROR',error_type=type(e).__name__,error=str(e)[:120],cursor=cursor));raise
    finally:reader.close();index.close();scope.close();lock.close()

if __name__=='__main__':main()
