"""Amsterdam-only 12 GiB extension. Full-table scan, bounded graph, honest gaps."""
import argparse
from collections import Counter, deque
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import time

from economic_machine.pump_research.history_core import Scope,Chunks,extract,TABLES,sha,GIB
from economic_machine.pump_research.store import atomic,decode,encode


def readonly(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-32768');db.execute('PRAGMA temp_store=FILE')
    return db


def second(stamp):
    if stamp is None:return None
    # Explicit source time normalization, never a guess about ordering in a slot.
    if 10**12<=stamp<10**13:return stamp//1000
    if 0<=stamp<10**10:return stamp
    raise ValueError('CORE_TIMESTAMP_UNIT_UNSUPPORTED')


def audit(base,out):
    with closing(readonly(base/'data/search.sqlite3')) as db:
        rows=db.execute('SELECT mint,creator,created_ms,provider_rank,ath_status,reported_ath FROM candidates ORDER BY provider_rank').fetchall()
        report=dict(at_ms=time.time_ns()//1000000,candidate_rows=len(rows),
            ath_status=dict(db.execute('SELECT ath_status,count(*) FROM candidates GROUP BY ath_status')),
            candidates_with_recovered_create=db.execute('SELECT count(DISTINCT c.mint) FROM candidates c JOIN launches l ON l.mint=c.mint').fetchone()[0],
            watchlist_by_reason=dict(db.execute('SELECT reason,count(*) FROM watchlist GROUP BY reason')),
            historical_jobs=[dict(kind=k,status=s,count=n) for k,s,n in db.execute('SELECT kind,status,count(*) FROM jobs GROUP BY kind,status')],
            verified_all_time_top1000=False,complete_wallet_history=False,verified_current_funded_wallets=None,
            verified_bundle_membership=None,automatic_purchases=0)
        candidates=[dict(mint=m,creator=c,created_ms=t,provider_rank=r,ath_status=s,reported_ath=a) for m,c,t,r,s,a in rows]
    for name in ('model-status.json','fast-status.json','live-status.json'):
        p=base/'data'/name
        if p.exists():
            d=json.loads(p.read_bytes())
            report[name] = {k:d.get(k) for k in ('state','updated_ms','at','counts','budget','policy_version','error','orders_submitted','verified_all_time_top1000') if k in d}
    atomic(out/'initial-audit.json',report);atomic(out/'candidate-snapshot.json',candidates)
    return report


def trace(db,seeds,before,protocol,scope):
    """Observed funding only. Global table coverage is not wallet completeness."""
    queue=deque((s,before,0) for s in seeds)
    visited=set();edges=[];counts=Counter();seen_edges=set()
    while queue:
        scope.guard()
        wallet,cutoff,depth=queue.popleft()
        if (wallet,cutoff) in visited:continue
        if len(visited)>=protocol['trace_node_limit_per_candidate']:
            counts['node_limit_pending']+=len(queue)+1;break
        visited.add((wallet,cutoff))
        if depth>=protocol['trace_depth']:counts['depth_limit']+=1;continue
        # Raw timestamps in this table may be seconds or milliseconds. Both
        # ranges are explicit and indexed; equality is excluded causally.
        rows=db.execute('''SELECT rowid,source,stamp,slot,symbol,amount,source_labeled,signature
            FROM projection WHERE destination=? AND
              ((stamp>=0 AND stamp<?) OR (stamp>=1000000000000 AND stamp<?))
            ORDER BY stamp DESC LIMIT ?''',(wallet,cutoff,cutoff*1000,protocol['trace_branch_limit']+1)).fetchall()
        if len(rows)>protocol['trace_branch_limit']:
            counts['high_fanout_boundary']+=1;continue
        if not rows:counts['no_prior_edge_observed']+=1
        for ident,source,stamp,slot,symbol,amount,labeled,signature in rows:
            at=second(stamp)
            if at>=cutoff:raise ValueError('CORE_TRACE_TIME_ORDER')
            if ident not in seen_edges:
                seen_edges.add(ident)
                edges.append(dict(rowid=ident,source=encode(source),destination=encode(wallet),timestamp=at,
                    slot=slot,symbol=symbol,amount=amount,depth=depth+1,signature=signature,
                    source_labeled_by_third_party=bool(labeled),ownership_proven=False))
            if labeled:counts['labeled_source_boundary']+=1
            elif source!=wallet:queue.append((source,at,depth+1))
    return dict(edges=edges,visited_wallet_time_pairs=len(visited),boundaries=dict(counts),
                complete_history=False,current_funds_proven=False,bundle_membership_proven=False)


def analyse(scope,protocol,notify):
    out=scope.out;folder=out/'dossiers';folder.mkdir(exist_ok=True)
    candidates=json.loads((out/'candidate-snapshot.json').read_bytes())
    counters=Counter();reports=[]
    with closing(readonly(out/'token_metadata.sqlite3')) as meta, closing(readonly(out/'arkm_funding.sqlite3')) as fund, closing(readonly(out/'pump_event_default.sqlite3')) as trades:
        for db in (meta,fund,trades):db.set_progress_handler(lambda:(scope.guard() or 0),100000)
        population={}
        for name,db,timecol in [('token_metadata',meta,'first_seen'),('arkm_funding',fund,'stamp'),('pump_event_default',trades,'stamp')]:
            notify(phase='POPULATION_ANALYSIS',table=name)
            rows= db.execute('SELECT count(*),min('+timecol+'),max('+timecol+') FROM projection').fetchone()
            population[name]=dict(rows=rows[0],minimum_timestamp=rows[1],maximum_timestamp=rows[2])
        population['funding_symbols']=[dict(symbol=s,rows=n) for s,n in fund.execute('SELECT symbol,count(*) FROM projection GROUP BY symbol')]
        population['event_sides']=[dict(side=s,rows=n) for s,n in trades.execute('SELECT side,count(*) FROM projection GROUP BY side')]
        population['metadata_creator_frequency_histogram']=[dict(launches_per_creator=k,creators=n) for k,n in meta.execute('SELECT launches,count(*) FROM (SELECT creator,count(*) launches FROM projection WHERE creator IS NOT NULL GROUP BY creator) GROUP BY launches')]
        atomic(out/'population.json',population)
        for c in candidates:
            scope.guard(32*1024**2,force=True)
            mint=decode(c['mint']);metadata=meta.execute('SELECT creator,first_seen,created_signature,last_updated FROM projection WHERE mint=?',(mint,)).fetchall()
            first_trade=trades.execute('SELECT min(stamp),max(stamp),count(*) FROM projection WHERE mint=?',(mint,)).fetchone()
            creators={r[0] for r in metadata if r[0] is not None}
            origin=min((second(r[1]) for r in metadata if r[1] is not None),default=None)
            provider_time=c['created_ms']//1000 if c['created_ms'] else None
            # First-seen metadata is not automatically the true launch time.
            bound=origin if origin is not None else provider_time
            creator=next(iter(creators)) if len(creators)==1 else None
            if creator is not None:counters['metadata_creator_observed']+=1
            if not metadata:counters['metadata_absent']+=1
            if len(creators)>1:counters['metadata_creator_conflict']+=1
            if creator and creator!=decode(c['creator']):counters['provider_creator_mismatch']+=1
            early=[];early_count=0
            if bound is not None:
                early_count=trades.execute('SELECT count(DISTINCT trader) FROM projection WHERE mint=? AND stamp>=? AND stamp<?',(mint,bound,bound+protocol['early_window_seconds'])).fetchone()[0]
                early=trades.execute('''SELECT trader,min(stamp),count(*),sum(CASE WHEN side='buy' THEN sol ELSE 0 END),sum(CASE WHEN side='sell' THEN sol ELSE 0 END)
                    FROM projection WHERE mint=? AND stamp>=? AND stamp<? AND trader IS NOT NULL
                    GROUP BY trader ORDER BY min(stamp),trader LIMIT ?''',(mint,bound,bound+protocol['early_window_seconds'],protocol['early_wallet_seed_limit_per_candidate'])).fetchall()
            seeds=[creator] if creator else [decode(c['creator'])]
            seeds+= [r[0] for r in early if r[0] not in seeds]
            graph=trace(fund,seeds,bound,protocol,scope) if bound is not None else dict(edges=[],complete_history=False)
            if graph['edges']:counters['candidates_with_prior_associations']+=1
            if first_trade[2]:counters['candidates_with_trade_observations']+=1
            repeat=meta.execute('SELECT count(*) FROM projection WHERE creator=? AND first_seen<?',(creator,origin)).fetchone()[0] if creator and origin is not None else None
            report=dict(schema='economic-machine.pump-candidate-dossier.v1',candidate=c,
                all_time_rank_verified=False,metadata_records=len(metadata),metadata_creators=[encode(v) for v in sorted(creators)],
                first_seen_seconds=origin,provider_launch_seconds=provider_time,chain_create_independently_verified=False,
                earlier_observed_launches_same_creator=repeat,
                observed_trade_records=first_trade[2],trade_first_seen=first_trade[0],trade_last_seen=first_trade[1],
                early_window_seconds=protocol['early_window_seconds'],early_distinct_traders=early_count,
                early_seed_truncated=early_count>len(early),
                early_wallets=[dict(wallet=encode(w),first_seen=t,events=n,buy_lamports=b,sell_lamports=s,bundle_membership_proven=False) for w,t,n,b,s in early],
                funding=graph,current_balance_verified=False,latest_funded_wallets=None,
                exit_policy=protocol['exit_policy'],profitability_proven=False,trading_enabled=False)
            atomic(folder/(str(c['provider_rank']).zfill(4)+'.json'),report)
            counters['processed_candidates']+=1
            reports.append(dict(rank=c['provider_rank'],mint=c['mint'],metadata_rows=len(metadata),trade_rows=first_trade[2],
                early_traders=early_count,early_seed_truncated=report['early_seed_truncated'],funding_edges=len(graph['edges']),
                funding_boundaries=graph.get('boundaries',{})))
            if len(reports)%10==0:notify(phase='CANDIDATE_ANALYSIS',processed_candidates=len(reports),counters=dict(counters))
    report=dict(schema='economic-machine.pump-core-analysis.v1',state='COMPLETE_BOUNDED_CORE_ANALYSIS',
        at_ms=time.time_ns()//1000000,grant_sha256=scope.hash,counters=dict(counters),population=population,
        candidates=reports,verified_all_time_top1000=False,complete_wallet_history=False,latest_funded_wallets_verified=False,
        live_tracker_running=False,bundle_membership_verified=False,exit_policy_established=False,profitability_proven=False,
        new_model_calls=0,orders_submitted=0,
        limitations=['The provider ATH snapshot is not an independently verified top-1000 universe.',
          'All rows of three approved tables were scanned; event data are sampled and funding data are incomplete.',
          'No transaction index within a slot, reserve replay, complete transfer histories, or current finalized balances are supplied by these tables.',
          'Historical funding associations and labels do not prove common ownership or bundle membership.',
          'No winning entry/exit policy is established by this descriptive study.'])
    atomic(out/'REPORT.json',report)
    md='# Pump creator and funding investigation\n\n'
    md+='Status: bounded core-table analysis complete; the requested full top-1000-to-current-wallet investigation remains incomplete.\n\n'
    md+='All source rows of the three approved tables were processed. The original provider snapshot is preserved.\n\n'
    for k,v in counters.items():md+=f'- {k}: {v}\n'
    md+='\nEach candidate has a dossier under `dossiers/`, including missing evidence and traversal boundaries. No bundle, current-balance, trading, or profitability claim is made.\n'
    md+='\nSource: https://doi.org/10.1184/R1/33420628 ; methods: https://arxiv.org/html/2609.10246v1\n'
    (out/'REPORT.md').write_text(md)
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--authorize',action='store_true');a=p.parse_args()
    base=Path(a.base);out=base/'evidence/core-trace-v1';out.mkdir(exist_ok=True)
    plan_path=base/'evidence/history-alpha-v1/core-read-plan.json'
    toc_path=base/'evidence/history-alpha-v1/factories-toc.json'
    parts_path=base/'evidence/history-alpha-v1/figshare.json'
    protocol_path=Path(__file__).with_name('CORE-TRACE-PROTOCOL-V1.json')
    grant_path=out/'grant.json'
    if a.authorize:
        if grant_path.exists():raise ValueError('CORE_PRESERVE_GRANT')
        now=time.time_ns()//1000000
        old=json.loads((base/'data/research-model-scope.json').read_bytes())
        grant=dict(schema='economic-machine.pump-core-grant.v1',approved_at_ms=now,deadline_ms=min(now+43200000,old['deadline_ms']),
            authorization='Owner explicitly approved expansion to all core raw records; 12 GiB additional IO, 32 GiB total research storage, one core for 12 hours.',
            additional_io_bytes=12*GIB,total_storage_bytes=32*GIB,maximum_seconds=43200,maximum_cpu_percent=100,
            trading=False,paid_api=False,new_model_calls=0,new_server=False,
            plan_sha256=sha(plan_path),toc_sha256=sha(toc_path),parts_sha256=sha(parts_path),protocol_sha256=sha(protocol_path),
            old_io_ledger_preserved=True)
        atomic(grant_path,grant);atomic(out/'protocol.json',json.loads(protocol_path.read_bytes()));audit(base,out)
        print(json.dumps(dict(state='AUTHORIZED',deadline_ms=grant['deadline_ms'],grant_sha256=sha(grant_path))));return
    lock=(out/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scope=Scope(base,grant_path);g=scope.grant
    for path,k in [(plan_path,'plan_sha256'),(toc_path,'toc_sha256'),(parts_path,'parts_sha256'),(protocol_path,'protocol_sha256')]:
        if sha(path)!=g[k]:raise ValueError('CORE_INPUT_IDENTITY_CHANGED')
    protocol=json.loads(protocol_path.read_bytes())
    status=dict(schema='economic-machine.pump-core-status.v1',started_ms=time.time_ns()//1000000,grant_sha256=scope.hash,
        deadline_ms=g['deadline_ms'],core_analysis_complete=False,full_user_investigation_complete=False,orders_submitted=0,new_model_calls=0,tables={})
    def notify(**values):
        status.update(values,updated_ms=time.time_ns()//1000000,reserved_additional_bytes=scope.reserved())
        atomic(out/'status.json',status)
    def stop(*_):raise RuntimeError('CORE_STOP_SIGNAL_PRESERVE_CHECKPOINT')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    try:
        notify(state='RUNNING',phase='ADMISSION')
        plan=json.loads(plan_path.read_bytes());toc=json.loads(toc_path.read_bytes())
        parts=sorted((r for r in json.loads(parts_path.read_bytes())['files'] if r['name'].startswith('dump.pgcustom.part-')),key=lambda r:r['name'])
        chunks=Chunks(scope,parts)
        for name in TABLES:
            entry=next(e for e in toc['entries'] if e['tag']==name and e['description']=='TABLE DATA')
            row=next(r for r in plan['core_tables'] if r['table']==name)
            if entry['offset']!=row['start'] or row['end_exclusive']-row['start']!=row['compressed_bytes']:raise ValueError('CORE_PLAN_OFFSETS')
            notify(table=name,phase='EXTRACTING')
            status['tables'][name]=extract(scope,chunks,entry,row['compressed_bytes'],notify)
            notify(phase='TABLE_COMPLETE')
        analyse(scope,protocol,notify)
        scope.guard(force=True)
        notify(state='COMPLETE_BOUNDED_CORE_ANALYSIS',phase='DONE',core_analysis_complete=True,research_storage_bytes=scope.used)
    except BaseException as e:
        notify(state='STOPPED_ERROR',error_type=type(e).__name__,error=str(e)[:160]);raise
    finally:scope.close();lock.close()


if __name__=='__main__':main()
