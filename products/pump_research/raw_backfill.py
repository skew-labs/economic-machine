"""One bounded acquisition pass using the existing grant; no model or paid API."""
import argparse
import fcntl
import hashlib
import json
import time
import urllib.error
from pathlib import Path
from economic_machine.pump_research.wallet_map import Graph
from economic_machine.pump_research.history_core import Scope,used_bytes
from economic_machine.pump_research.research_quality import audit
from economic_machine.pump_research.store import atomic

def retry_source(operation,scope,report):
    for attempt in range(4):
        try:return operation()
        except urllib.error.HTTPError as e:
            if e.code not in (429,502,503,504) or attempt==3:raise
            report.setdefault('source_retries',[]).append(dict(status=e.code,attempt=attempt+1,at_ms=time.time_ns()//1000000))
            # ScopedArchive also enforces the provider Retry-After timestamp.
            for _ in range(60*(attempt+1)):
                scope.guard();time.sleep(1)

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);g=Graph(base);scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    lock=(g.out/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    started=time.time_ns()//1000000
    report=dict(at_ms=started,paid_api=False,model_calls=0,orders_submitted=0,
        grant_sha256=scope.hash,io_reserved_before=scope.reserved(),storage_before=used_bytes(base),
        launches=[],raw_transfers=[],full_wallet_history=False,independent_consensus_verified=False)
    try:
        scope.guard(64*1024**2,force=True)
        blocked=g.db.execute("SELECT * FROM frontier WHERE source='recover_launch' AND state IN ('source_blocked','pending')").fetchall()
        for row in blocked:
            job=dict(row);old=g.state('source_error:'+job['id'],{})
            if old.get('error')!='MAP_TIME_SEARCH_MISSING_RANGE':continue
            record=dict(job_id=job['id'],before=job,original_error=old,outcomes=[])
            report['launches'].append(record)
            # Reuse exact job/cursor; never reset prior binary-search samples.
            with g.db:g.db.execute("UPDATE frontier SET state='pending' WHERE id=?",(job['id'],))
            for _ in range(8):
                job=dict(g.db.execute('SELECT * FROM frontier WHERE id=?',(job['id'],)).fetchone())
                if job['state']!='pending':break
                record['outcomes'].append(retry_source(lambda:g.recover_launch(dict(g.db.execute('SELECT * FROM frontier WHERE id=?',(job['id'],)).fetchone()),scope),scope,report))
            record['after']=dict(g.db.execute('SELECT * FROM frontier WHERE id=?',(job['id'],)).fetchone())
        for _ in range(12):
            g.raw_history.seed(scope.guard)
            row=g.db.execute("SELECT * FROM frontier WHERE source='archive_transfer' AND state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:break
            report['raw_transfers'].append(retry_source(lambda:g.raw_history.run(dict(row),scope),scope,report))
        quality=audit(base,scope.guard);atomic(g.out/'quality.json',quality)
        report['quality_totals']=quality['totals'];report['root_coverage']=quality['roots']
        # A previous unknown model outcome must not collide with its durable
        # request identity on resume. Preserve all reservations and responses.
        with g.db:g.set('model_retry_generation',g.state('model_retry_generation',0)+1)
        report['state']='ACQUISITION_PASS_COMPLETE'
    except BaseException as e:
        report.update(state='ACQUISITION_PASS_FAILED',error_type=type(e).__name__,error=str(e)[:160])
        raise
    finally:
        report.update(io_reserved_after=scope.reserved(),storage_after=used_bytes(base),finished_ms=time.time_ns()//1000000)
        atomic(g.out/'raw-history.json',g.raw_history.snapshot());atomic(g.out/'map.json',g.view())
        path=g.out/f'RAW-HISTORY-REPORT-{started}.json';atomic(path,report)
        print(str(path),flush=True)
        g.close();scope.close();lock.close()

if __name__=='__main__':main()
