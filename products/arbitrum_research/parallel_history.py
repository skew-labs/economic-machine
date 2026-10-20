"""Amsterdam owns leases, budget, Blob receipts and cursors; Tokyo computes."""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextlib import closing
from decimal import Decimal
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import zlib
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from economic_machine.arbitrum_research.parallel import load_plan,SourceGate
from economic_machine.arbitrum_research.source import parse_page,SourceBackoff
from economic_machine.arbitrum_research.storage import Storage
from economic_machine.pump_research.store import atomic,canonical
from economic_machine.pump_research.research_spend import ceiling

BASE=Path('/srv/economic-machine/arbitrum')
SHARED=Path('/srv/economic-machine/solana')
MIB=1024**2
STOP=threading.Event()


def unpack(packed,part,maximum):
    if (type(part.get('raw_bytes')) is not int or not 0<part['raw_bytes']<=maximum
        or len(packed)!=part['bytes'] or hashlib.sha256(packed).hexdigest()!=part['sha256']):
        raise ValueError('TOKYO_COMPRESSED_IDENTITY')
    decoder=zlib.decompressobj(31)
    raw=decoder.decompress(packed,part['raw_bytes']+1)
    if (not decoder.eof or decoder.unused_data or len(raw)!=part['raw_bytes']
        or hashlib.sha256(raw).hexdigest()!=part['raw_sha256']):
        raise ValueError('TOKYO_RAW_IDENTITY')
    return raw


def read_exact(stream,size):
    chunks=[];left=size
    while left:
        chunk=stream.read(min(left,MIB))
        if not chunk:raise ValueError('TOKYO_TRUNCATED_TRANSPORT')
        chunks.append(chunk);left-=len(chunk)
    return b''.join(chunks)


def read_worker_header(stream,job,on_source):
    """A source-stage notice releases only the fetch lease, never the cursor."""
    def one():
        line=stream.readline(65537)
        if len(line)>65536 or not line.endswith(b'\n'):raise ValueError('TOKYO_RESPONSE_HEADER')
        value=json.loads(line)
        if not isinstance(value,dict):raise ValueError('TOKYO_RESPONSE_SHAPE')
        return value,len(line)
    header,size=one();notice=None
    if header.get('event')=='SOURCE_RETAINED':
        notice=header
        if (set(notice)!={'event','job_id','source_sha256','first_block','last_block'}
            or notice['job_id']!=job['id'] or notice['first_block']!=job['start']
            or type(notice['first_block']) is not int or type(notice['last_block']) is not int
            or not job['start']<=notice['last_block']<=job['end']
            or not isinstance(notice['source_sha256'],str)
            or not re.fullmatch('[0-9a-f]{64}',notice['source_sha256'])):
            raise ValueError('TOKYO_SOURCE_NOTICE')
        on_source();header,more=one();size+=more
        if 'error' not in header and any(header.get(k)!=notice[k] for k in ('source_sha256','first_block','last_block')):
            raise ValueError('TOKYO_SOURCE_NOTICE_MISMATCH')
    return header,size,notice


def settle_transport(spend,key,received):
    # Preserve the reservation body; a separate receipt records received bytes.
    # 1 MiB of allowance covers framing and transport overhead conservatively.
    if type(received) is not int or not 0<=received<=63*MIB:raise ValueError('TOKYO_TRANSFER_BOUND')
    with spend.db:
        spend.db.execute('BEGIN IMMEDIATE')
        row=spend.db.execute("SELECT body FROM operations WHERE kind='read' AND key=?",(key,)).fetchone()
        if not row or json.loads(row[0])!={'bytes':64*MIB,'digest':None,'usd':None}:
            raise ValueError('TOKYO_TRANSFER_RESERVATION')
        cost=ceiling(Decimal(received+MIB)*spend.grant['internet_read_krw_per_gib']/1024**3)+spend.grant['operation_krw']
        spend.db.execute("UPDATE operations SET krw=? WHERE kind='read' AND key=?",(cost,key))


def validate_package(header,job,raw,index_path,previous_tip):
    if header.get('schema')!='economic-machine.tokyo-public-shard.v1' or header.get('job')!=job:
        raise ValueError('TOKYO_JOB_IDENTITY')
    page=parse_page(raw,job['start'],job['end'])
    if (page['sha256']!=header['source_sha256'] or page['last_block']!=header['last_block']
        or page['first_block']!=header['first_block']):raise ValueError('TOKYO_SOURCE_IDENTITY')
    first_parent=page['blocks'][0]['header']['parentHash']
    if previous_tip and first_parent!=previous_tip:raise ValueError('TOKYO_CROSS_PAGE_PARENT')
    with closing(sqlite3.connect('file:'+str(index_path)+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('TOKYO_INDEX_INTEGRITY')
        identity=json.loads(db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0])
        if identity!={'chain_id':42161,'initial_start':job['start'],'previous_block_hash':first_parent,'schema':1}:
            raise ValueError('TOKYO_INDEX_IDENTITY')
        rows=db.execute('SELECT number,hash,parent_hash FROM blocks ORDER BY number').fetchall()
        expected=[(b['header']['number'],b['header']['hash'],b['header']['parentHash']) for b in page['blocks']]
        if rows!=expected:raise ValueError('TOKYO_INDEX_HEADERS')
        if db.execute('SELECT source_sha256,start,end FROM pages').fetchall()!=[(page['sha256'],page['first_block'],page['last_block'])]:
            raise ValueError('TOKYO_INDEX_SOURCE')
        if db.execute('SELECT hash,block,tx_index,sender,recipient,value,status FROM transactions ORDER BY block,tx_index').fetchall()!=[
            (tx['hash'].lower(),b['header']['number'],tx['transactionIndex'],tx['from'].lower(),
             tx['to'].lower() if tx.get('to') else None,
             str(int(tx['value'],16) if isinstance(tx['value'],str) and tx['value'].startswith('0x') else int(tx['value'])),
             tx.get('status')) for b in page['blocks'] for tx in b['transactions']]:
            raise ValueError('TOKYO_INDEX_TRANSACTIONS')
    return page


def execute(job,lane,previous_tip):
    started=time.monotonic()
    lane_base=BASE/'parallel-tokyo'/str(lane)
    (lane_base/'evidence').mkdir(parents=True,exist_ok=True)
    cached=lane_base/'evidence'/('job-'+job['id']+'.json')
    if cached.exists():
        result=json.loads(cached.read_text())
        if result['job']!=job:raise ValueError('TOKYO_CACHED_JOB')
        return result
    storage=Storage(lane_base,SHARED);gate=SourceGate(BASE/'evidence/parallel-source.sqlite3')
    token=None;process=None
    try:
        if storage.cloud.spend.totals()['remaining_krw']<1000:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
        token=gate.acquire(owner='remote')
        attempt='tokyo:'+uuid.uuid4().hex
        storage.cloud.spend.reserve('read',attempt,bytes_=64*MIB)
        command=['ssh','-T','-i',str(BASE/'private/tokyo-transport'),'-o','BatchMode=yes',
            '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(BASE/'private/tokyo-known-hosts'),
            '-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            'em-history@192.0.2.10']
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        process.stdin.write((canonical(job)+'\n').encode());process.stdin.flush()
        source_released=None
        def release_source():
            nonlocal token,source_released
            gate.release(token);token=None;source_released=time.monotonic()
        header,framing,notice=read_worker_header(process.stdout,job,release_source)
        if header.get('error')=='WORKER_FAILED':
            raise ValueError('TOKYO_WORKER_FAILED:'+str(header.get('reason'))[:180])
        if header.get('error')=='SOURCE_BACKOFF':
            delay=header.get('retry_after_seconds')
            if not isinstance(delay,(int,float)) or not 1<=delay<=86400:raise ValueError('TOKYO_BACKOFF_BOUND')
            if token:gate.release(token,delay);token=None
            settle_transport(storage.cloud.spend,attempt,framing)
            raise SourceBackoff(delay,header.get('status'))
        parts=header.get('parts')
        if (not isinstance(parts,list) or len(parts)!=2
            or [p.get('name') for p in parts]!=['source.ndjson.gz','index.sqlite3.gz']
            or any(type(p.get('bytes')) is not int or not 0<p['bytes']<=63*MIB for p in parts)
            or sum(p['bytes'] for p in parts)+framing>63*MIB):raise ValueError('TOKYO_PARTS_BOUND')
        raw=unpack(read_exact(process.stdout,parts[0]['bytes']),parts[0],16*MIB)
        index=unpack(read_exact(process.stdout,parts[1]['bytes']),parts[1],256*MIB)
        settle_transport(storage.cloud.spend,attempt,sum(p['bytes'] for p in parts)+framing)
        if token:release_source()
        received=time.monotonic()
        path=lane_base/'staging'/(job['id']+'.sqlite3')
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=parts[1]['raw_sha256']:raise ValueError('TOKYO_STAGING_CHANGED')
        else:
            with path.open('xb') as out:out.write(index);out.flush();os.fsync(out.fileno())
        del index
        page=validate_package(header,job,raw,path,previous_tip)
        validated=time.monotonic()
        source=storage.put(raw,'tokyo_source_page',dict(job_id=job['id'],lane=lane,
            first_block=page['first_block'],last_block=page['last_block'],provider='SQD'))
        shard=storage.archive_index(path,header['snapshot'])
        result=dict(job=job,lane=lane,start=page['first_block'],end=page['last_block'],
            first_parent=page['blocks'][0]['header']['parentHash'],tip_hash=page['blocks'][-1]['header']['hash'],
            source=source,shard=shard,at_ms=time.time_ns()//1000000,
            source_sha256=page['sha256'],snapshot=header['snapshot'],
            graph_semantics='native_index_built_on_pinned_tokyo_worker',
            central_headers_and_transactions_rechecked=True,independent_consensus_verified=False,
            model_calls=0,transport_reserved_bytes=64*MIB,transport_received_bytes=sum(p['bytes'] for p in parts)+framing,
            source_lease_released_before_index=notice is not None,worker_timings=header.get('timings'),
            source_transport=header.get('source_transport'),timings=dict(source_slot_seconds=source_released-started,
                remote_and_transfer_seconds=received-started,central_validation_seconds=validated-received,
                central_archive_seconds=time.monotonic()-validated))
        proof=storage.put((canonical(result)+'\n').encode(),'tokyo_completed_shard')
        result['completion_proof']=proof
        atomic(cached,result)
        process.stdin.write((canonical(dict(id=job['id'],blob_verified=True,source_sha256=page['sha256']))+'\n').encode())
        process.stdin.flush();process.stdin.close()
        if process.wait(timeout=30)!=0:result['remote_cleanup_ack']='UNCONFIRMED_RETAINED'
        else:result['remote_cleanup_ack']='CONFIRMED'
        atomic(cached,result)
        return result
    finally:
        if process and process.poll() is None:process.kill();process.wait()
        if token:gate.release(token)
        gate.close();storage.close()


def run():
    import fcntl
    plan=load_plan(BASE)
    if plan is None:raise ValueError('PARALLEL_PLAN_REQUIRED')
    out=BASE/'evidence/parallel-tokyo';out.mkdir(exist_ok=True)
    lock=(out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=sqlite3.connect(out/'runtime.sqlite3');db.row_factory=sqlite3.Row
    db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS lanes(id INTEGER PRIMARY KEY,start INTEGER,end INTEGER,next INTEGER,tip TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,lane INTEGER,start INTEGER,end INTEGER,state TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS faults(at_ms INTEGER,job TEXT,error TEXT);''')
    digest=hashlib.sha256((BASE/'evidence/parallel-plan.json').read_bytes()).hexdigest()
    with db:
        rows=db.execute('SELECT hash FROM identity').fetchall()
        if rows and [r[0] for r in rows]!=[digest]:raise ValueError('TOKYO_PLAN_CHANGED')
        db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(digest,))
        for r in plan['ranges'][1:]:db.execute('INSERT OR IGNORE INTO lanes VALUES(?,?,?,?,NULL)',(r['lane'],r['start'],r['end'],r['start']))
    def publish(state,**extra):
        primary=json.loads((BASE/'evidence/live-status.json').read_text())
        primary_blocks=min(primary['next_block'],plan['ranges'][0]['end']+1)
        remote_blocks=sum(r['next']-r['start'] for r in db.execute('SELECT * FROM lanes'))
        atomic(out/'status.json',dict(state=state,at_ms=time.time_ns()//1000000,
            lanes=[dict(r) for r in db.execute('SELECT * FROM lanes ORDER BY id')],
            archived_shards=db.execute("SELECT count(*) FROM jobs WHERE state='ARCHIVED'").fetchone()[0],
            mapped_blocks=remote_blocks,combined_mapped_blocks=primary_blocks+remote_blocks,
            frozen_snapshot_block_percentage=100*(primary_blocks+remote_blocks)/(plan['frozen_provider_head']+1),
            frozen_provider_head=plan['frozen_provider_head'],whole_history_complete=False,
            independent_consensus_verified=False,**extra))
    running={};cooldowns={}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            while not STOP.is_set():
                active={v['lane'] for v in running.values()}
                for row in db.execute('SELECT * FROM lanes ORDER BY id').fetchall():
                    lane=row['id']
                    if lane in active or cooldowns.get(lane,0)>time.time() or row['next']>row['end']:continue
                    old=db.execute("SELECT * FROM jobs WHERE lane=? AND state='PENDING' ORDER BY start LIMIT 1",(lane,)).fetchone()
                    job=({'id':old['id'],'start':old['start'],'end':old['end']} if old else
                        dict(id=uuid.uuid4().hex,start=row['next'],end=min(row['end'],row['next']+8191)))
                    with db:db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)',
                        (job['id'],lane,job['start'],job['end'],'PENDING','{}'))
                    running[pool.submit(execute,job,lane,row['tip'])]=dict(lane=lane,job=job)
                publish('RUNNING',inflight=len(running))
                if not running:
                    if all(r['next']>r['end'] for r in db.execute('SELECT * FROM lanes')):publish('FROZEN_RANGES_COMPLETE');break
                    STOP.wait(2);continue
                done,_=wait(running,timeout=2,return_when=FIRST_COMPLETED)
                for future in done:
                    value=running.pop(future);job=value['job'];lane=value['lane']
                    try:result=future.result()
                    except SourceBackoff as e:
                        cooldowns[lane]=time.time()+e.retry_after_seconds
                        publish('SOURCE_BACKOFF',retry_seconds=e.retry_after_seconds);continue
                    except Exception as e:
                        with db:db.execute('INSERT INTO faults VALUES(?,?,?)',(time.time_ns()//1000000,job['id'],type(e).__name__+':'+str(e)[:160]))
                        publish('STOPPED',error=type(e).__name__+':'+str(e)[:160]);STOP.set();raise
                    with db:
                        db.execute("UPDATE jobs SET state='ARCHIVED',body=? WHERE id=?",(canonical(result),job['id']))
                        db.execute('UPDATE lanes SET next=?,tip=? WHERE id=?',(result['end']+1,result['tip_hash'],lane))
    finally:db.close();lock.close()


if __name__=='__main__':
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:STOP.set())
    run()
