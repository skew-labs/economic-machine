import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from .store import Store, atomic, canonical, address
from .archive import Archive, get
from .reporting import summary

def proto(root):
    sys.path.insert(0,str(Path(root)/'vendor/yellowstone'))
    import geyser_pb2, solana_storage_pb2
    return geyser_pb2,solana_storage_pb2.TransactionStatusMeta

def seed(s, maximum=1000):
    offset=s.state('seed_offset',0)
    while offset<maximum:
        limit=min(50,maximum-offset)
        url=f'https://frontend-api-v3.pump.fun/coins?offset={offset}&limit={limit}&sort=ath_market_cap&order=DESC&includeNsfw=true'
        raw=get(url,2*1024**2);rows=json.loads(raw)
        if not isinstance(rows,list) or not rows or len(rows)>limit: raise ValueError('PUMP_PAGE_SHAPE')
        with s.db:
            ev=s.evidence('pump_metadata',f'ath_snapshot:offset={offset}',raw)
            s.candidates(rows,ev,offset);offset+=len(rows);s.set_state('seed_offset',offset)
        time.sleep(.15)
    with s.db:
        s.set_state('seed',dict(at=time.time(),rows_requested=offset,universe_complete=False,ranking_verified=False,nsfw_included=True,sort='provider_reported_ath_market_cap',pagination_snapshot_isolation=False))
    return summary(s)

def enqueue_funding_followups(s):
    """Continue a Muse-selected investigation from recovered launch evidence.

    One preceding 128-slot window per selected mint in this initial scope.
    No matched edge means 'not found in scope', never 'unfunded/clean'.
    """
    added=[]
    with s.db:
        for job in s.db.execute("SELECT * FROM jobs WHERE kind='launch_window' AND status='complete'").fetchall():
            args=json.loads(job['args']);mint=args['mint']
            launch=s.db.execute('SELECT min(slot) FROM launches WHERE mint=?',(mint,)).fetchone()[0]
            if launch is None: continue
            ident='funding:'+mint+':'+str(launch)
            body=dict(start=max(240000000,launch-128),end=launch-1,mint=mint,parent_job=job['id'],purpose='prelaunch_funding_search')
            if s.db.execute('INSERT OR IGNORE INTO jobs(id,kind,args,status) VALUES(?,?,?,?)',(ident,'slot_window',canonical(body),'pending')).rowcount:added.append(ident)
    return added

def run_archive_job(s, meta, budget):
    lock=(s.root/'archive.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scope=s.root/'research-model-scope.json'
    if scope.exists() and time.time()*1000>=json.loads(scope.read_bytes())['deadline_ms']:
        return dict(state='RESEARCH_SCOPE_ENDED')
    enqueue_funding_followups(s)
    jobs=s.db.execute("SELECT * FROM jobs WHERE status IN ('running','pending','failed') ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,rowid").fetchall()
    job=None
    for candidate in jobs:
        transient=any((candidate['error'] or '').startswith('HTTPError:HTTP Error '+str(code)) for code in (429,500,502,503,504))
        storage_recovered=(candidate['error'] or '')=='ValueError:RESEARCH_STORAGE_LIMIT'
        if candidate['status']=='failed' and (not (transient or storage_recovered) or candidate['attempts']>=3):continue
        if time.time()<s.state('archive_retry_at:'+candidate['id'],0):continue
        job=candidate;break
    if not job: return None
    args=json.loads(job['args']);reader=Archive(s,meta,max_bytes=budget)
    with s.db: s.db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=?",(job['id'],))
    try:
        if 'resolved_start' in args:
            start=args['resolved_start'];end=args['resolved_end'];samples=args.get('time_search_samples',[])
        elif job['kind']=='launch_window':
            slot,samples=reader.locate_time(args['created_ms']//1000)
            # Metadata creation time is only an anchor; absence here is not
            # evidence that the launch never happened.
            start=max(240000000,slot-8);end=slot+8
        elif job['kind']=='slot_window':
            start=args['start'];end=args['end'];samples=[]
        else: raise ValueError('JOB_KIND')
        if not start<=end<start+128: raise ValueError('JOB_SLOT_BOUND')
        args.update(resolved_start=start,resolved_end=end,time_search_samples=samples)
        with s.db:s.db.execute('UPDATE jobs SET args=? WHERE id=?',(canonical(args),job['id']))
        rows=[]
        for n in range(start,end+1):
            old=s.db.execute("SELECT status FROM coverage WHERE source='old-faithful' AND start=? AND end=?",(n,n)).fetchone()
            rows.append(dict(slot=n,reused_committed=True) if old and old['status']=='decoded' else reader.ingest_slot(n))
        result=dict(slots=rows,time_search_samples=samples,requested_bytes=reader.bytes,complete_wallet_history=False)
        with s.db: s.db.execute("UPDATE jobs SET status='complete',result=? WHERE id=?",(canonical(result),job['id']))
        enqueue_funding_followups(s)
    except Exception as e:
        with s.db:
            s.db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?",(type(e).__name__+':'+str(e)[:160],job['id']))
            s.set_state('archive_retry_at:'+job['id'],time.time()+60*2**min(job['attempts'],4))
        raise
    finally:
        with s.db:
            total=s.state('archive',dict(requested_bytes=0,jobs_attempted=0))
            total['requested_bytes']+=reader.bytes;total['jobs_attempted']+=1;total['updated_at']=time.time();s.set_state('archive',total)
    return result

def main():
    p=argparse.ArgumentParser(description='Economic Machine evidence search (read-only chain research)')
    p.add_argument('--root',required=True);p.add_argument('--ticking-root',default='/srv/economic-machine/collector')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('seed').add_argument('--count',type=int,default=1000)
    sub.add_parser('status')
    q=sub.add_parser('search');q.add_argument('query')
    q=sub.add_parser('token');q.add_argument('mint')
    q=sub.add_parser('trace');q.add_argument('wallet');q.add_argument('--depth',type=int,default=3);q.add_argument('--before',type=int,default=2**62)
    q=sub.add_parser('queue-slot');q.add_argument('start',type=int);q.add_argument('end',type=int)
    q=sub.add_parser('queue-launch');q.add_argument('mint')
    q=sub.add_parser('backfill');q.add_argument('--max-mib',type=int,default=256)
    q=sub.add_parser('tail');q.add_argument('--once',action='store_true')
    q=sub.add_parser('serve');q.add_argument('--port',type=int,default=19436)
    a=p.parse_args();s=Store(a.root)
    if a.command=='seed':
        if not 1<=a.count<=10000: raise ValueError('SEED_COUNT_BOUND')
        result=seed(s,a.count)
    elif a.command=='search':result=s.search(a.query)
    elif a.command=='token':result=s.token(a.mint)
    elif a.command=='trace':result=s.trace(a.wallet,a.depth,before=a.before)
    elif a.command=='status':result=summary(s)
    elif a.command in ('queue-slot','queue-launch'):
        if a.command=='queue-slot':
            if not 240000000<=a.start<=a.end<min(a.start+128,1000000001): raise ValueError('SLOT_QUEUE_BOUND')
            args=dict(start=a.start,end=a.end);kind='slot_window';ident=f'slots:{a.start}:{a.end}'
        else:
            if not address(a.mint): raise ValueError('MINT')
            row=s.db.execute('SELECT * FROM candidates WHERE mint=?',(a.mint,)).fetchone()
            if not row or not row['created_ms']: raise ValueError('NO_CREATION_ANCHOR')
            args=dict(mint=a.mint,created_ms=row['created_ms']);kind='launch_window';ident='launch:'+a.mint
        with s.db: s.db.execute('INSERT OR IGNORE INTO jobs(id,kind,args,status) VALUES(?,?,?,?)',(ident,kind,canonical(args),'pending'))
        result=dict(queued=ident)
    elif a.command=='backfill':
        if not 1<=a.max_mib<=1024: raise ValueError('BACKFILL_BYTE_BOUND')
        _,meta=proto(a.ticking_root);result=run_archive_job(s,meta,a.max_mib*1024**2)
    elif a.command=='tail':
        lock=(s.root/'tail.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pb,_=proto(a.ticking_root)
        from .ticking import consume
        stopping=False;last_report=0
        def stop(*_):
            nonlocal stopping;stopping=True
        signal.signal(signal.SIGTERM,stop)
        while not stopping:
            try:
                scope=s.root/'research-model-scope.json'
                if scope.exists() and time.time()*1000>=json.loads(scope.read_bytes())['deadline_ms']:
                    atomic(s.root/'live-status.json',dict(state='RESEARCH_SCOPE_ENDED',**summary(s)));break
                # Bound write-lock residency so the independent evaluator and
                # archive worker can commit without starving behind a replay.
                state=consume(s,a.ticking_root,pb,max_frames=256)
                # Counting millions of derived rows is reporting work, not a
                # prerequisite for every 256-frame durable cursor commit.
                if a.once or time.monotonic()-last_report>=5:
                    atomic(s.root/'live-status.json',dict(state='READ_ONLY_RESEARCH',**summary(s)))
                    last_report=time.monotonic()
            except Exception as e:
                atomic(s.root/'live-status.json',dict(state='PAUSED_ERROR',error=type(e).__name__+':'+str(e)[:160],**summary(s)))
                raise
            if a.once:break
            time.sleep(.2 if state['caught_up_to_sampled_watermark'] else .01)
        result=summary(s)
    else:
        from .http import serve
        serve(s,a.port);return
    print(canonical(result))

if __name__=='__main__': main()
