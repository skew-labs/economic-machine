"""Fresh Ticking -> checked event adapter -> native signal. No order transport.

The archival research cursor is never touched. A cold start explicitly attaches
at the current durable head and can only admit launches observed subsequently.
Processed observations are provisional. Reorgs/late evidence invalidate output.
"""
import argparse
from collections import Counter, deque
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import sqlite3
import stat
import threading
import time
from .cli import proto
from .originals import Originals
from .store import PUMP, decode, atomic, canonical
from .wire import from_geyser
from .fast_policy import load
from .fast_native import Library, Engine
from .fast_bus import Bus

MAX_ACTIVE=128
QUOTA=5*1024**3

def marker(path,producer_uid=None):
    if producer_uid is None:producer_uid=os.geteuid()
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=producer_uid or s.st_mode&0o077 or s.st_size>1024:raise ValueError('FAST_MARKER_FILE')
        v=json.loads(f.read(1025))
    if (type(v) is not dict or not re.fullmatch('[0-9]{8}',v.get('segment',''))
        or type(v.get('offset')) is not int or not 0<=v['offset']<=96*1024**2
        or type(v.get('records')) is not int or v['records']<0):raise ValueError('FAST_MARKER_VALUE')
    return v

class Wake:
    def __init__(self,path):
        lib=ctypes.CDLL(None,use_errno=True)
        self.fd=lib.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
        if self.fd<0:raise OSError('FAST_INOTIFY')
        lib.inotify_add_watch.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32]
        if lib.inotify_add_watch(self.fd,os.fsencode(path.parent),0x8|0x80)<0:
            os.close(self.fd);raise OSError('FAST_INOTIFY_WATCH')
    def wait(self):
        if select.select([self.fd],[],[],.01)[0]:os.read(self.fd,65536)
    def close(self):os.close(self.fd)

class Funding:
    """Indexed, bounded slow lane. No SQLite call on the event/decision lane."""
    def __init__(self,root):
        self.requests=queue.Queue(512);self.results=queue.Queue(512);self.stop=threading.Event()
        self.stats=Counter();self.root=root
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def request(self,mint,wallet,launch_slot,known_at):
        try:self.requests.put_nowait((mint,wallet,launch_slot,known_at))
        except queue.Full:self.stats['request_capacity']+=1
    def run(self):
        db=sqlite3.connect(f'file:{self.root/"search.sqlite3"}?mode=ro',uri=True,timeout=.1)
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-2048')
        while not self.stop.is_set():
            try:mint,wallet,slot,at=self.requests.get(timeout=.2)
            except queue.Empty:continue
            # Each query has a VM instruction budget; missing history stays unknown.
            ticks=0
            def limit():
                nonlocal ticks
                ticks+=1;return ticks>100
            db.set_progress_handler(limit,1000)
            try:
                rows=db.execute('''SELECT t.source,t.slot FROM transfers t
                    JOIN observations o ON o.id=t.observation JOIN evidence e ON e.id=o.evidence
                    WHERE t.destination=? AND t.mint='SOL' AND t.kind='system_transfer'
                    AND t.slot<? AND e.observed<=? AND o.finality!='dead_branch'
                    ORDER BY t.slot DESC LIMIT 4''',(wallet,slot,at)).fetchall()
                self.stats['lookups']+=1
                # This is only the latest OBSERVED inbound association. Multiple
                # sampled sources remain ambiguous; a CEX is never an owner label.
                if rows and len({r[0] for r in rows})==1:
                    try:self.results.put_nowait((mint,wallet,rows[0][0],rows[0][1]));self.stats['associated']+=1
                    except queue.Full:self.stats['result_capacity']+=1
                else:self.stats['unknown_or_ambiguous']+=1
            except sqlite3.Error:self.stats['query_unavailable']+=1
        db.close()
    def close(self):self.stop.set();self.thread.join(timeout=2)

def percentiles(values):
    v=sorted(values)
    if not v:return dict(n=0)
    return dict(n=len(v),p50=v[max(0,math.ceil(len(v)*.5)-1)],p95=v[max(0,math.ceil(len(v)*.95)-1)],p99=v[max(0,math.ceil(len(v)*.99)-1)],maximum=v[-1])

def admission(root):
    # Same existing research quota, including WAL and crash leftovers; no purge.
    used=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    fs=os.statvfs(root)
    if used+2*1024**2>QUOTA or fs.f_bavail*fs.f_frsize<48*1024**3:raise ValueError('RESEARCH_STORAGE_LIMIT')
    return used

def run(root,ticking,library,seconds=0,bus_path='/run/economic-machine-pump-fast/signals.sock'):
    root=Path(root);ticking=Path(ticking)
    scope=json.loads((root/'research-model-scope.json').read_bytes())
    deadline=scope['deadline_ms'];now=lambda:time.time_ns()//1000000
    if now()>=deadline:raise ValueError('FAST_SCOPE_ENDED')
    started=time.monotonic();stop_at=min(deadline/1000,time.time()+seconds) if seconds else deadline/1000
    lock=(root/'fast.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    used=admission(root)
    p=load(root,now(),deadline);native=Library(library);pb,_=proto(ticking)
    folder=ticking/'data/result-archive'
    producer_uid=folder.stat().st_uid
    memory=Path('/dev/shm')/('tick-tail-'+str(producer_uid)+'-'+hashlib.sha256(str(folder.resolve()).encode()).hexdigest()+'.json')
    path=memory if memory.exists() else folder/'durable-tail.json'
    origin=marker(path,producer_uid);head=origin;segment=head['segment'];offset=head['offset'];seq=None;highslot=0
    wake=Wake(path);reader=Originals(ticking);funding=Funding(root)
    bus=Bus(bus_path)
    counts=Counter();reasons=Counter();active={};recent=deque(maxlen=128)
    timings={k:deque(maxlen=8192) for k in ('frame_read_decode_and_ingest_us','event_adapter_and_native_ingest_us','native_decide_ffi_us','frame_read_to_result_us','archive_ingress_to_result_ms')}
    stopping=False;last_status=last_policy=last_health=0;policy_error=None;state='RUNNING_RESEARCH_ONLY'
    gaps={};last_stamp=0
    def stop(*_):
        nonlocal stopping
        stopping=True
    def revoke(result):
        if result and not result['invalidated_after_publication']:
            result['invalidated_after_publication']=True;counts['signal_invalidations']+=1
            ms=now();bus.publish(result,2,ms,ms+1000)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    def publish():
        return dict(schema='economic-machine.pump-fast-status.v1',state=state,updated_ms=now(),started_at_ms=int((time.time()-(time.monotonic()-started))*1000),
                    start_head=origin,cursor=dict(segment=segment,offset=offset),head=head,policy_version=p['version'],profile=p['profile'],
                    policy_error=policy_error,policy_expires_ms=p['expires_ms'],scope_deadline_ms=deadline,
                    processed_slot=highslot,active_cohorts=len(active),native_bytes_per_cohort=native.lib.machine_pump_bytes(),
                    maximum_cohorts=MAX_ACTIVE,counts=dict(counts),reasons=dict(reasons),funding=dict(funding.stats),
                    signal_bus=dict(bus.stats),signal_bus_packet_bytes=232,
                    timings={k:percentiles(v) for k,v in timings.items()},timing_sample='last_8192_observations_per_stage',
                    last_archive_ingress_age_ms=(time.time_ns()-last_stamp)/1e6 if last_stamp else None,
                    upstream=gaps,research_storage_bytes=used,cold_or_blob_reads=0,model_calls_in_event_loop=0,
                    historical_cursor_modified=False,history_complete=False,profitability_proven=False,
                    commitment='processed_feed_unconfirmed',execution_authority=False,orders_submitted=0)
    try:
        while not stopping and time.time()<stop_at:
            bus.accept()
            clock=time.monotonic()
            if clock-last_policy>=5:
                try:
                    candidate=load(root,now(),deadline)
                    if candidate['version']<p['version']:raise ValueError('FAST_POLICY_ROLLBACK')
                    p=candidate;policy_error=None
                except (ValueError,OSError) as ex:policy_error=str(ex)[:100]
                last_policy=clock
            if clock-last_health>=30:
                used=admission(root)
                upstream=json.loads((folder/'status.json').read_bytes())
                gaps={k:upstream.get(k) for k in ('lastSlot','unrepairedGapWitnesses','completeGapRecoveryVerified','connected','clockSynchronizationVerified')}
                last_health=clock
            while True:
                try:mint,wallet,funder,edge_slot=funding.results.get_nowait()
                except queue.Empty:break
                cohort=active.get(mint)
                if cohort and not cohort['done']:cohort['engine'].funding(wallet,funder,edge_slot)
            if (segment==head['segment'] and offset==head['offset']) or counts['frames']%128==0:
                head=marker(path,producer_uid)
            if (head['segment'],head['offset'])<(segment,offset):raise ValueError('FAST_HEAD_REGRESSION')
            if segment==head['segment'] and offset==head['offset']:
                if clock-last_status>=5:atomic(root/'fast-status.json',publish());atomic(root/'fast-recent.json',list(recent));last_status=clock
                wake.wait();continue
            # No network/cold catch-up. Falling behind retained hot originals is
            # an explicit stop, never a silent jump to another head.
            hot=reader._hot(segment)
            if hot is None:raise ValueError('FAST_LANE_FELL_BEHIND_HOT_ORIGINALS')
            limit=head['offset'] if segment==head['segment'] else os.fstat(hot.fileno()).st_size
            if offset==limit:
                segment=f'{int(segment)+1:08}';offset=0;seq=0;continue
            t0=time.perf_counter_ns();raw,n,stamp,end=reader.frame(segment,offset,limit)
            if seq is not None and n!=seq+1:raise ValueError('FAST_FRAME_SEQUENCE')
            seq=n;start_offset=offset;offset=end;counts['frames']+=1;last_stamp=stamp
            msg=pb.SubscribeUpdate();msg.ParseFromString(raw);kind=msg.WhichOneof('update_oneof')
            transactions=[];slot=0;dead=False
            if kind=='transaction':slot=msg.transaction.slot;transactions=[msg.transaction.transaction]
            elif kind=='block':slot=msg.block.slot;transactions=msg.block.transactions
            elif kind=='slot':slot=msg.slot.slot;dead=msg.slot.status==6
            highslot=max(highslot,slot)
            age=(time.time_ns()-stamp)/1e6
            bad_source=age>2000 or age < -1000
            if dead or bad_source:
                counts['dead_slot' if dead else 'stale_source_frame']+=1
                for cohort in active.values():
                    if bad_source or cohort['launch']['slot']<=slot<=cohort['cutoff']:
                        cohort['engine'].invalidate()
                        revoke(cohort.get('result'))
                if dead:
                    for result in recent:
                        if result['launch_slot']<=slot<=result['cutoff_slot'] and not result['invalidated_after_publication']:
                            revoke(result)
            if decode(PUMP) in raw:
                for info in transactions:
                    if info.is_vote or info.meta.HasField('err'):continue
                    if decode(PUMP) not in [bytes(k) for k in (*info.transaction.message.account_keys,*info.meta.loaded_writable_addresses,*info.meta.loaded_readonly_addresses)]:continue
                    try:tx=from_geyser(info,slot)
                    except (ValueError,IndexError,KeyError):
                        counts['decode_error']+=1
                        for cohort in active.values():cohort['engine'].invalidate();revoke(cohort.get('result'))
                        continue
                    counts['pump_transactions']+=1
                    if tx['event_errors']:counts['event_evidence_errors']+=len(tx['event_errors'])
                    for launch in tx['launches']:
                        mint=launch['mint']
                        if mint in active:continue
                        if bad_source or policy_error or now()>=p['expires_ms']:counts['launch_policy_or_source_rejected']+=1;continue
                        if len(active)>=MAX_ACTIVE:counts['launch_capacity_rejected']+=1;continue
                        launch=dict(launch,slot=slot,tx_order=tx['index'],known_at=time.time())
                        active[mint]=dict(launch=launch,engine=Engine(native,p,launch),done=False,cutoff=slot+p['parameters']['window_slots'])
                        counts['launches_admitted']+=1
                    affected={(a['mint'],a['ix']) for a in tx['activity']}
                    matched={(e['mint'],e['ix']) for e in tx['events']}
                    for mint,_ in affected-matched:
                        if mint in active:
                            active[mint]['engine'].invalidate();revoke(active[mint].get('result'));counts['missing_trade_event']+=1
                    for e in tx['events']:
                        cohort=active.get(e['mint'])
                        if not cohort:continue
                        e=dict(e,slot=slot,tx_order=tx['index'],signature=tx['signature'])
                        if (slot,tx['index'],e['ix'])<(cohort['launch']['slot'],cohort['launch']['tx_order'],cohort['launch']['ix']):continue
                        if slot>=cohort['cutoff']:continue
                        engine=cohort['engine'];key=(e['signature'],e['ix'])
                        if cohort['done'] and key not in engine.events:
                            engine.invalidate();counts['late_after_decision']+=1
                            revoke(cohort['result'])
                            continue
                        unknown=e['user'] not in engine.ids
                        t=time.perf_counter_ns()
                        try:reason=engine.ingest(e)
                        except ValueError:engine.invalidate();reason='bad_input'
                        timings['event_adapter_and_native_ingest_us'].append((time.perf_counter_ns()-t)/1000)
                        counts['native_events']+=1
                        if reason not in ('candidate','observing'):
                            counts['native_rejected_events']+=1
                            revoke(cohort.get('result'))
                        if unknown:funding.request(e['mint'],e['user'],cohort['launch']['slot'],cohort['launch']['known_at'])
            timings['frame_read_decode_and_ingest_us'].append((time.perf_counter_ns()-t0)/1000)
            for mint,cohort in list(active.items()):
                if not cohort['done'] and highslot>=cohort['cutoff']:
                    t=time.perf_counter_ns();result=cohort['engine'].decide(highslot,now())
                    timings['native_decide_ffi_us'].append((time.perf_counter_ns()-t)/1000)
                    result.update(mint=mint,launch_slot=cohort['launch']['slot'],decided_at_ms=now(),
                        source_reference=dict(segment=segment,start=start_offset,end=end,sequence=n,raw_sha256=hashlib.sha256(raw).hexdigest()),
                        processed_unconfirmed=True,history_complete=False,ownership_inferred=False,
                        funding_semantics='latest_observed_inbound_association_not_verified_owner',
                        upstream_gap_count=gaps.get('unrepairedGapWitnesses'),invalidated_after_publication=False,
                        execution_authority=False,profitability_proven=False,quote_method='post_event_reserve_sensitivity_not_fill')
                    cohort['done']=True;cohort['result']=result;recent.append(result);reasons[result['reason']]+=1;counts['decisions']+=1
                    ms=now();result['signal_valid_until_ms']=min(ms+1000,cohort['engine'].policy['expires_ms'])
                    if result['signal_valid_until_ms']>ms:bus.publish(result,1,ms,result['signal_valid_until_ms'])
                    timings['frame_read_to_result_us'].append((time.perf_counter_ns()-t0)/1000)
                    timings['archive_ingress_to_result_ms'].append((time.time_ns()-stamp)/1e6)
                if highslot>cohort['cutoff']+32:cohort['engine'].close();del active[mint]
            if clock-last_status>=5:
                atomic(root/'fast-status.json',publish());atomic(root/'fast-recent.json',list(recent));last_status=clock
        state='COMPLETE_SCOPE' if time.time()>=deadline/1000 else 'COMPLETE_BOUNDED_RUN' if seconds else 'STOPPED'
    except Exception as ex:
        state='STOPPED_ERROR';counts['fatal_errors']+=1;policy_error=type(ex).__name__+':'+str(ex)[:120]
        raise
    finally:
        funding.close();wake.close();reader.close();bus.close()
        for cohort in active.values():cohort['engine'].close()
        atomic(root/'fast-status.json',publish());atomic(root/'fast-recent.json',list(recent))
    return publish()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--ticking-root',required=True)
    p.add_argument('--library',required=True);p.add_argument('--seconds',type=int,default=0);a=p.parse_args()
    print(canonical(run(a.root,a.ticking_root,a.library,a.seconds)))
