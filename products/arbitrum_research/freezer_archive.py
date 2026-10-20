"""Bounded index/derivative archivists, never source-ledger writers."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import json
from pathlib import Path
import signal
import sqlite3
import time

from economic_machine.arbitrum_research.freezer_cloud_index import prepare,retire,retire_derived
from economic_machine.arbitrum_research.freezer_source import DEADLINE
from economic_machine.arbitrum_research.freezer_archive_guard import inventory_metrics
from economic_machine.pump_research.store import atomic

STOP=False


def reconcile_finished(db, running, *, drain=False):
    """Journal all completed workers, including siblings of a failed worker."""
    failures=[]
    for folder,future in list(running.items()):
        if not drain and not future.done():continue
        if future.cancelled():
            del running[folder]
            continue
        try:
            value=future.result()
            with db:db.execute('insert or replace into pages values(?,?)',(str(folder),json.dumps(value)))
        except Exception as error:
            detail=dict(at_ms=time.time_ns()//1000000,folder=str(folder),
                        error_type=type(error).__name__,details=getattr(error,'safe_details',None),
                        original_preserved=True,automatic_retry=False)
            with db:db.execute('insert into failures(folder,at_ms,body) values(?,?,?)',
                               (str(folder),detail['at_ms'],json.dumps(detail)))
            failures.append(detail)
        finally:del running[folder]
    return failures


def archive(base,folder):
    began=time.monotonic();prepare(base,folder);prepared=time.monotonic();index=retire(base,folder);retired=time.monotonic();derived=retire_derived(base,folder)
    return dict(folder=str(folder),seconds=time.monotonic()-began,index_bytes=index['source_identity']['bytes'],
                derived_bytes=sum(f['identity']['bytes'] for f in derived['files']),full_index_readback=True,
                prepare_seconds=prepared-began,retire_seconds=retired-prepared,derived_seconds=time.monotonic()-retired,
                query_parity=True,native_root_replay=derived['native_root_replay'],retired_at_ms=time.time_ns()//1000000)


def refresh_progress(db, out, state, running):
    values=[json.loads(r[0]) for r in db.execute('select body from pages')]
    parent=sqlite3.connect('file:'+str(out/'runtime.sqlite3')+'?mode=ro',uri=True)
    try:
        registered=parent.execute('select first,last,blocks,body from pages order by first').fetchall()
        started=json.loads(parent.execute('select body from identity').fetchone()[0])['run_started_at_ms']
    finally:parent.close()
    retired={v['folder'] for v in values};prefix=0;backlog_bytes=0
    for first,last,blocks,body in registered:
        w=json.loads(body);folder=Path(w['batch'])/f'window-{first}-{last}'
        if str(folder) not in retired:break
        prefix+=blocks
    for first,last,blocks,body in registered:
        w=json.loads(body);folder=Path(w['batch'])/f'window-{first}-{last}'
        if str(folder) not in retired:
            for name in ('index.sqlite3','candidate-records.json','candidate-decoded.json'):
                path=folder/name
                try:backlog_bytes+=path.stat().st_size
                except FileNotFoundError:pass
    wall=max(.001,(time.time_ns()//1000000-started)/1000)
    state.update(at_ms=time.time_ns()//1000000,archive_inventory=inventory_metrics(),retired_pages=len(values),active_pages=[str(p) for p in running],
                 retired_index_bytes=sum(r['index_bytes'] for r in values),retired_derived_bytes=sum(r['derived_bytes'] for r in values),
                 registered_pages=len(registered),archive_backlog_pages=len(registered)-len(values),
                 archive_backlog_local_bytes=backlog_bytes,portable_search_contiguous_blocks=prefix,
                 portable_search_blocks_per_second=prefix/wall,run_started_at_ms=started,wall_seconds=wall)


def wait_for_workers(running):
    """Refill promptly on completion without adding workers or requests."""
    if running:
        wait(tuple(running.values()), timeout=3, return_when=FIRST_COMPLETED)
    else:
        time.sleep(3)


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--workers',type=int,choices=(1,2,3),default=2)
    p.add_argument('--max-pages',type=int);a=p.parse_args();base=Path(a.base).resolve()
    if a.max_pages is not None and not 1<=a.max_pages<=64:raise ValueError('ARCHIVE_RUN_BOUND')
    out=base/'evidence/freezer-continuous-v1';lock=(out/'archiver.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def stop(signum,frame):
        global STOP
        STOP=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    db=sqlite3.connect(out/'archive.sqlite3');db.execute('create table if not exists pages(folder text primary key,body text)')
    db.execute('create table if not exists failures(id integer primary key,folder text,at_ms integer,body text)');db.commit()
    launched=0;failures=[]
    running={};state=dict(state='RUNNING',deadline_ms=DEADLINE,full_mapping_complete=False,workers=a.workers)
    pool=ThreadPoolExecutor(max_workers=a.workers)
    try:
        while not STOP and time.time_ns()//1000000<DEADLINE:
            failures.extend(reconcile_finished(db,running))
            if failures:raise RuntimeError('ARCHIVE_WORKER_FAILED_RETAINED')
            if a.max_pages is not None and launched>=a.max_pages and not running:
                state['state']='COMPLETE_BOUNDED_ARCHIVE';break
            if len(running)<a.workers and (a.max_pages is None or launched<a.max_pages):
                known={r[0] for r in db.execute('select folder from pages')}
                for batch in sorted(out.glob('batch-*'),key=lambda x:int(x.name.split('-')[1])):
                    if not (batch/'status.json').exists() or json.loads((batch/'status.json').read_bytes())['state']!='COMPLETE_BOUNDED_WINDOW':continue
                    for folder in sorted(batch.glob('window-*'),key=lambda x:int(x.name.split('-')[1])):
                        if str(folder) in known or folder in running:continue
                        running[folder]=pool.submit(archive,base,folder);launched+=1
                        if len(running)==a.workers or a.max_pages is not None and launched>=a.max_pages:break
                    if len(running)==a.workers or a.max_pages is not None and launched>=a.max_pages:break
                if a.max_pages is not None and not running:
                    state['state']='NO_PENDING_ARCHIVE';break
            refresh_progress(db,out,state,running)
            atomic(out/'archive-status.json',state);wait_for_workers(running)
        if state['state']=='RUNNING':state['state']='STOPPED_PRESERVED' if STOP else 'DEADLINE_COMPLETE_PARTIAL'
    except Exception as e:
        state.update(state='STOPPED_REVIEW_REQUIRED',error_type=type(e).__name__);raise
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        failures.extend(reconcile_finished(db,running,drain=True))
        if failures:state['state']='STOPPED_REVIEW_REQUIRED'
        refresh_progress(db,out,state,running)
        state.update(at_ms=time.time_ns()//1000000,active_pages=[],launched_this_run=launched,
                     max_pages_this_run=a.max_pages,worker_failures=failures,
                     retired_pages=db.execute('select count(*) from pages').fetchone()[0])
        atomic(out/'archive-status.json',state);db.close();lock.close()


if __name__=='__main__':main()
