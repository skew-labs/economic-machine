"""Resumable private snapshot header catalogue, no unpacking or execution."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import subprocess
import sys
import uuid

from economic_machine.arbitrum_research.snapshot_ranges import parts,split_range,tar_header,HTTP
from economic_machine.pump_research.archive_gate import ArchiveGate,ArchiveDeferred
from three_chain_status import DEADLINE_MS
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.store import atomic,canonical


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--out',required=True)
    p.add_argument('--metadata',required=True);p.add_argument('--source-base',required=True);p.add_argument('--headers',type=int,default=64)
    p.add_argument('--loop',action='store_true')
    a=p.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    if a.loop:
        # A bounded batch releases its DB/HTTP handles. The same persisted
        # source gate, cursor and attempt count remain authoritative on resume.
        while time.time_ns()//1000000<DEADLINE_MS:
            run=subprocess.run([sys.executable,__file__,*[v for v in sys.argv[1:] if v!='--loop']],capture_output=True)
            state=json.loads((out/'status.json').read_bytes()) if (out/'status.json').exists() else {}
            if state.get('cursor',{}).get('finished'):return
            if run.returncode:
                error=json.loads((out/'error.json').read_bytes()) if (out/'error.json').exists() else {}
                code=error.get('http_status')
                transient=error.get('error_type')=='URLError' or type(code) is int and (code==429 or 500<=code<=599)
                if not transient:raise ValueError('CATALOGUE_CHILD_STOPPED')
            gate=out/'source-gate/network-state.json'
            cooldown=json.loads(gate.read_bytes()).get('not_before',0) if gate.exists() else 0
            while time.time()<cooldown:
                if time.time_ns()//1000000>=DEADLINE_MS:return
                time.sleep(min(30,max(0,cooldown-time.time())))
        return
    if not 1<=a.headers<=256:raise ValueError('CATALOGUE_RUN_BOUND')
    metadata=Path(a.metadata).read_bytes()
    if len(metadata)>65536:raise ValueError('CATALOGUE_METADATA_BOUND')
    rows=parts(json.loads(metadata),a.source_base);total=sum(r['bytes'] for r in rows)
    identity=canonical(dict(metadata_sha256=hashlib.sha256(metadata).hexdigest(),parts=rows,deadline_ms=DEADLINE_MS))
    lock=(out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=sqlite3.connect(out/'runtime.sqlite3',timeout=30);db.row_factory=sqlite3.Row
    db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1),offset INTEGER,finished INTEGER);
      CREATE TABLE IF NOT EXISTS sources(url TEXT PRIMARY KEY,body TEXT);
      CREATE TABLE IF NOT EXISTS headers(offset INTEGER PRIMARY KEY,raw BLOB,body TEXT);
      CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,offset INTEGER,bytes INTEGER,state TEXT);
    ''')
    with db:
        old=db.execute('select body from identity').fetchall()
        if old and [r[0] for r in old]!=[identity]:raise ValueError('CATALOGUE_IDENTITY_CHANGED')
        db.execute('insert or ignore into identity values(?)',(identity,));db.execute('insert or ignore into cursor values(1,0,0)')
    cloud=Cloud(a.base);http=HTTP();began=time.monotonic();calls=0
    def guard():
        if time.time_ns()//1000000>=DEADLINE_MS:raise ValueError('CATALOGUE_DEADLINE')
        cloud.guard_local(1024**2)
        if db.execute('select count(*) from attempts').fetchone()[0]>=20000:raise ValueError('CATALOGUE_REQUEST_BOUND')
    def read(start,size):
        nonlocal calls
        data=[]
        for part,relative,n in split_range(rows,start,size):
            with ArchiveGate(out/'source-gate',guard,wait=False):
                attempts=[]
                def reserve():
                    guard();attempt=uuid.uuid4().hex;attempts.append(attempt)
                    with db:db.execute('insert into attempts values(?,?,?,?)',(attempt,start,n,'reserved'))
                    cloud.spend.reserve('read','snapshot-'+attempt,bytes_=n)
                reserve()
                r=db.execute('select body from sources where url=?',(part['url'],)).fetchone();pin=json.loads(r[0]) if r else None
                raw,source=http.read(part,relative,n,pin,before_redirect=reserve);calls=http.requests;data.append(raw)
                with db:
                    db.execute('insert or ignore into sources values(?,?)',(part['url'],canonical(source)))
                    db.executemany("update attempts set state='received' where id=?",[(attempt,) for attempt in attempts])
        return b''.join(data)
    state='CATALOGUING'
    try:
        for _ in range(a.headers):
            guard();cursor=db.execute('select offset,finished from cursor').fetchone()
            if cursor['finished']:break
            offset=cursor['offset'];raw=read(offset,512);entry=tar_header(raw,offset,total)
            if entry['end_marker']:
                if read(offset+512,512)!=bytes(512):raise ValueError('CATALOGUE_END_MARKER')
            with db:
                db.execute('insert into headers values(?,?,?)',(offset,raw,canonical(entry)))
                db.execute('update cursor set offset=?,finished=? where id=1',(entry['next_offset'],int(entry['end_marker'])))
        state='CATALOGUING'
    except ArchiveDeferred:state='SOURCE_BACKOFF'
    except Exception as e:
        state='STOPPED'
        error=dict(error_type=type(e).__name__,error=str(e)[:200],http_status=getattr(e,'code',None),at_ms=time.time_ns()//1000000)
        atomic(out/('error-'+str(error['at_ms'])+'.json'),error);atomic(out/'error.json',error)
        raise
    finally:
        entries=[json.loads(r[0]) for r in db.execute('select body from headers order by offset')]
        cursor=dict(db.execute('select offset,finished from cursor').fetchone())
        if cursor['finished']:state='CATALOGUE_COMPLETE'
        report=dict(at_ms=time.time_ns()//1000000,state=state,source_bytes=total,header_count=len(entries),cursor=cursor,
            history_candidates=[r for r in entries if r.get('history_candidate')],requests_this_run=calls,
            elapsed_seconds=time.monotonic()-began,header_payload_bytes_retained=512*len(entries),
            cursor_is_archive_position_not_downloaded_bytes=True,
            whole_snapshot_sha256_verified=False,chain_records_indexed=0)
        raw_headers=[dict(offset=r[0],raw_hex=bytes(r[1]).hex()) for r in db.execute('select offset,raw from headers order by offset')]
        atomic(out/'catalogue.json',dict(source=json.loads(identity),entries=entries,raw_headers=raw_headers))
        proof=cloud.upload(out/'catalogue.json');report['catalogue_blob']=proof
        atomic(out/'status.json',report);print(json.dumps(report))
        http.close();cloud.close();db.close();lock.close()


if __name__=='__main__':main()
