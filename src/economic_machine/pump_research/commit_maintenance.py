"""Small offline maintenance pass under the original research scope.

No provider, model, network reservation, transfer, or old frontier operation is
called. Cached CAR policy review precedes graph assessment. Reports and the
separate audit cursor are append-only, including across process restarts.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from .address_index import AddressIndex,RAW_COMMIT_BLOCK_SQL
from .commit_audit import audit_cached_commits
from .commit_graph import assess_edges,assess_new_edges
from .store import canonical


PROTO_ROOT='/srv/economic-machine/collector'


def _cache_revision(folder,index,object_roots):
    stat=folder.stat() if folder.exists() else None
    inputs=dict(directory_mtime_ns=stat.st_mtime_ns if stat else None,
        block_highwater=index.db.execute('SELECT coalesce(max(id),0) FROM blocks WHERE '+RAW_COMMIT_BLOCK_SQL).fetchone()[0],parser_policy=1,
        object_directories=[(root/'objects').stat().st_mtime_ns if (root/'objects').exists() else None for root in object_roots])
    return hashlib.sha256(canonical(inputs).encode()).hexdigest()


def review_retained(g,scope,max_blocks=2,max_edges=300):
    if (type(max_blocks) is not int or not 1<=max_blocks<=8 or
        type(max_edges) is not int or not 1<=max_edges<=1000):raise ValueError('COMMIT_REVIEW_BOUND')
    if g.db.in_transaction:raise ValueError('COMMIT_REVIEW_OPEN_TRANSACTION')
    scope.guard(max_blocks*32*1024**2+1024**2)
    path=Path(g.out)/'address-index.sqlite3'
    if not path.exists():return dict(state='WAITING_FOR_RETAINED_INDEX',network_requests=0,model_calls=0)
    lock=(Path(g.out)/'commit-review.lock').open('a')
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='COMMIT_REVIEW_BUSY',network_requests=0,model_calls=0)
        with g.db:g.db.execute('''CREATE TABLE IF NOT EXISTS commit_review_runs(
            id INTEGER PRIMARY KEY,report_path TEXT,report_sha256 TEXT,body TEXT)''')
        row=g.db.execute('SELECT body FROM commit_review_runs ORDER BY id DESC LIMIT 1').fetchone()
        previous=json.loads(row[0]) if row else {}
        index=AddressIndex(path)
        try:
            folder=Path(g.out)/'archive-ranges';object_roots=(Path(g.base)/'data',Path(g.out)/'recovered')
            revision=_cache_revision(folder,index,object_roots)
            cursor=previous.get('cache_cursor',0);number=previous.get('cache_pass',1)
            pass_revision=previous.get('cache_revision',revision)
            if previous.get('cache_pass_complete') and previous.get('cache_revision')!=revision:
                cursor=0;number+=1;pass_revision=revision
            run_ns=time.time_ns();report=dict(schema='economic-machine.commit-review.v1',started_ns=run_ns,
                grant_sha256=scope.hash,deadline_ms=scope.grant['deadline_ms'],cache_revision=pass_revision,cache_observed_revision=revision,
                cache_pass=number,cache_cursor_before=cursor,cache_cursor=cursor,cache_pass_complete=False,
                maximum_blocks=max_blocks,maximum_edges=max_edges,network_requests=0,model_calls=0,
                orders_submitted=0,original_frontiers_unchanged=True,full_wallet_history=False,
                independent_consensus_verified=False)
            error=None
            try:
                scope.guard()
                if previous.get('cache_pass_complete') and previous.get('cache_revision')==revision:
                    cached=dict(scanned_blocks=0,next_after_id=cursor,more_blocks=False,policies_appended=0,state='CACHE_PASS_CURRENT')
                else:
                    from .cli import proto
                    _,meta=proto(PROTO_ROOT)
                    cached=audit_cached_commits(index,folder,meta,scope.guard,after_id=cursor,limit=max_blocks,
                        max_bytes=max_blocks*32*1024**2,object_roots=object_roots)
                # Full per-block audit receipts are immutable in the index;
                # this operational report keeps a bounded diagnostic sample.
                for block in cached.get('blocks',[]):
                    errors=block.get('decode_errors',[])
                    block['decode_error_count']=len(errors);block['decode_errors']=errors[:20]
                report.update(cache=cached,cache_cursor=cached['next_after_id'],cache_pass_complete=not cached['more_blocks'])
                priority=assess_new_edges(g,index,scope,limit=min(64,max_edges))
                report['graph_priority']=priority
                remaining=max_edges-priority['assessed']
                report['graph']=(assess_edges(g,index,scope,limit=remaining) if remaining else
                    dict(assessed=0,status='PRIORITY_USED_REVIEW_BUDGET',historical_checkpoints_unchanged=True))
                report['state']='OFFLINE_REVIEW_COMPLETE'
            except BaseException as caught:
                error=caught;report.update(state='OFFLINE_REVIEW_FAILED',error_type=type(caught).__name__,error=str(caught)[:160])
            report['finished_ns']=time.time_ns()
            report_path=Path(g.out)/f'COMMIT-REVIEW-{run_ns}-{os.getpid()}.json'
            raw=(canonical(report)+'\n').encode()
            if len(raw)>1024**2:raise ValueError('COMMIT_REVIEW_REPORT_BOUND')
            with report_path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            with g.db:g.db.execute('INSERT INTO commit_review_runs(report_path,report_sha256,body) VALUES(?,?,?)',
                (report_path.name,hashlib.sha256(raw).hexdigest(),canonical(report)))
            if error is not None:raise error
            return dict(report,report_path=str(report_path))
        finally:index.close()
    finally:lock.close()
