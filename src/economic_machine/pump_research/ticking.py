"""Independent, durable consumer of Ticking's retained originals (read only)."""
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib
from .store import PUMP, decode, canonical
from .wire import from_geyser

from .originals import HEADER, payload

def consume(store, root, pb, max_frames=4000):
    root=Path(root);folder=root/'data/result-archive';head=json.loads((folder/'durable-tail.json').read_bytes())
    if not (isinstance(head.get('segment'),str) and len(head['segment'])==8 and head['segment'].isdigit() and type(head.get('offset')) is int and head['offset']>=0): raise ValueError('TICKING_WATERMARK')
    state=store.state('ticking')
    if state is None:
        state=dict(segment=head['segment'],offset=0,started_at=time.time(),frames=0,indexed=0,errors=0,first_slot=None,last_slot=None,scope='from selected retained segment; not whole-chain coverage')
    with store.db:watches=store.watch_roots()
    watched={decode(a) for a in watches}
    watched.add(decode(PUMP));count=0;reader=store.originals(root)
    with store.db:
        while count<max_frames:
            if state['segment']>head['segment']: raise ValueError('TICKING_CURSOR_AHEAD')
            limit=head['offset'] if state['segment']==head['segment'] else reader.size(state['segment'])
            if state['offset']>limit:raise ValueError('TICKING_CURSOR_BEYOND_SEGMENT')
            if state['offset']>=limit:
                if state['segment']==head['segment']: break
                state['segment']=f"{int(state['segment'])+1:08d}";state['offset']=0;continue
            while state['offset']<limit and count<max_frames:
                start=state['offset'];raw,seq,stamp,end=reader.frame(state['segment'],start,limit)
                update=pb.SubscribeUpdate.FromString(raw)
                kind=update.WhichOneof('update_oneof');infos=[]
                if kind=='transaction': infos=[(update.transaction.transaction,update.transaction.slot)]
                elif kind=='block': infos=[(i,update.block.slot) for i in update.block.transactions]
                elif kind=='slot' and update.slot.status==pb.SLOT_DEAD:
                    store.db.execute("UPDATE observations SET finality='dead_branch' WHERE source='ticking' AND slot=?",(update.slot.slot,))
                eid=None
                for info,slot in infos:
                    state['first_slot']=slot if state['first_slot'] is None else min(state['first_slot'],slot)
                    state['last_slot']=max(state['last_slot'] or 0,slot)
                    if info.is_vote or info.meta.HasField('err'): continue
                    keys=[*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses]
                    if not any(bytes(k) in watched for k in keys): continue
                    try: tx=from_geyser(info,slot)
                    except (ValueError,IndexError) as e:
                        state['errors']+=1
                        store.db.execute('INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)',('ticking_decode',slot,slot,'decode_error',canonical({'error':str(e),'segment':state['segment'],'offset':start}),time.time()))
                        continue
                    if eid is None: eid=store.evidence('ticking',f"{state['segment']}:{start}:{seq}",raw,
                        original=dict(root=str(root.resolve()),segment=state['segment'],start=start,end=end,sequence=seq))
                    state['indexed']+=store.ingest(tx,eid,'ticking','processed_unconfirmed')
                    for new in store.expand_watch(tx,eid,watches):watched.add(decode(new))
                state['offset']=end;state['frames']+=1;count+=1
        upstream=json.loads((folder/'status.json').read_bytes())
        state['upstream']=dict(last_slot=upstream.get('lastSlot'),unrepaired_gaps=upstream.get('unrepairedGapWitnesses'),complete_gap_recovery=upstream.get('completeGapRecoveryVerified'))
        state['caught_up_to_sampled_watermark']=(state['segment'],state['offset'])==(head['segment'],head['offset'])
        state['storage_path']='shared_verified_ticking_originals';state['cold_loads_process']=reader.cold_loads
        state['updated_at']=time.time();store.set_state('ticking',state)
        if state['first_slot'] is not None:
            store.db.execute('DELETE FROM coverage WHERE source=?',('ticking_observed_window',))
            store.db.execute('INSERT INTO coverage VALUES(?,?,?,?,?,?)',('ticking_observed_window',state['first_slot'],state['last_slot'],'filtered_processed_not_complete',canonical(state['upstream']),time.time()))
    return state
