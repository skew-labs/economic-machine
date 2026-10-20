"""Apply the owner's additive private research scope; never starts services."""
import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from economic_machine.pump_research.history_core import Scope,used_bytes
from economic_machine.pump_research.research_expansion import GIB,FOLDER
from economic_machine.pump_research.store import atomic,canonical


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pinned_new(path,value):
    if path.exists() or path.with_suffix('.sha256').exists():
        raise ValueError('EXPANSION_EXISTING_AUTHORITY_PRESERVED')
    atomic(path,value)
    path.with_suffix('.sha256').write_text(sha(path)+'\n')


def authorize(base):
    out=base/'evidence/wallet-map-v1';folder=base/FOLDER
    core=base/'evidence/core-trace-v1/grant.json'
    parent=json.loads(core.read_bytes());now=time.time_ns()//1000000
    if now>=parent['deadline_ms']:raise ValueError('CORE_DEADLINE')
    legacy=base/'evidence/address-epoch-v1/grant.json'
    internal=out/'internal-grant.json';model=out/'model-scope.json'
    common=dict(parent_grant_sha256=sha(core),deadline_ms=parent['deadline_ms'],
        approved_ms=now,maximum_aggregate_cpu_percent=100,total_storage_bytes=224*GIB,
        trading=False,paid_data_api=False,new_servers=False,public_deployment=False,
        production_collector_changes=False)
    folder.mkdir(parents=True,exist_ok=True)
    pinned_new(folder/'grant.json',dict(common,schema='economic-machine.solana-history-expansion.v2',
        authorization='owner_all_public_solana_history_hundred_muse_workers',
        additional_io_bytes=128*GIB,minimum_host_free_bytes=128*GIB,
        epoch_scope='all_public_solana_history',priority_epochs=[1009,1008],
        original_ledgers_preserved=True,previous_epoch_grant_sha256=sha(legacy),
        previous_internal_grant_sha256=sha(internal),
        instruction='Expand public historical address indexes and missing raw data with one shared source budget; preserve original deadline, private scope and source journals.'))
    pinned_new(out/'parallel-grant.json',dict(common,schema='economic-machine.parallel-research-scope.v1',
        authorization='owner_private_solana_research_hundred_muse_workers',parallel_models=100,
        model='meta/muse-spark-1.3-contributor',model_call_limit=None,model_total_cost_limit=None,
        supersedes_internal_grant_sha256=sha(internal),original_model_scope_sha256=sha(model),
        source_fetch_parallelism=1,evidence_writer_parallelism=1))
    scope=Scope(base,core)
    try:
        return dict(history_grant_sha256=scope.expansion.hash,
            parallel_grant_sha256=sha(out/'parallel-grant.json'),deadline_ms=scope.grant['deadline_ms'],
            storage_bytes=scope.used,storage_limit=scope.storage_limit,
            new_io_limit=scope.expansion.grant['additional_io_bytes'])
    finally:scope.close()


def read_db(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
    return db


def prefix(db,table,maximum=None):
    maximum=maximum if maximum is not None else db.execute('SELECT coalesce(max(rowid),0) FROM '+table).fetchone()[0]
    h=hashlib.sha256();count=0
    for r in db.execute('SELECT rowid,* FROM '+table+' WHERE rowid<=? ORDER BY rowid',(maximum,)):
        h.update((canonical(list(r))+'\n').encode());count+=1
    return dict(max_rowid=maximum,count=count,sha256=h.hexdigest())


def snapshot(base,baseline=None):
    out=base/'evidence/wallet-map-v1';now=time.time_ns()//1000000
    before=json.loads(Path(baseline).read_bytes()) if baseline else None
    result=dict(schema='economic-machine.history-expansion-review.v2',at_ms=now)
    oldpaths=('core-trace-v1/grant.json','address-epoch-v1/grant.json','wallet-map-v1/internal-grant.json','wallet-map-v1/model-scope.json')
    result['original_grants']={p:sha(base/'evidence'/p) for p in oldpaths}
    with closing(read_db(out/'graph.sqlite3')) as db:
        result['graph_prefixes']={t:prefix(db,t,before['graph_prefixes'][t]['max_rowid'] if before else None)
            for t in ('edges','frontier_anchors')}
        result['graph_counts']={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0]
            for t in ('roots','nodes','edges','frontier','investigations')}
        row=db.execute("SELECT body FROM state WHERE key='parallel_last_batch'").fetchone()
        if row:
            batch=json.loads(row[0]);result['last_batch']={k:batch.get(k) for k in (
                'batch_ms','configured','reservations','dispatched','provider_calls_completed','selected_jobs',
                'observed_peak_concurrent','errors','interrupted_lanes_preserved')}
    with closing(read_db(out/'model.sqlite3')) as db:
        result['model_prefix']=prefix(db,'calls',before['model_prefix']['max_rowid'] if before else None)
        rows=db.execute('SELECT id,actual,reserved,status,response_hash FROM calls').fetchall()
        result['model']=dict(calls_reserved=len(rows),responses_received=sum(r['response_hash'] is not None for r in rows),
            valid_responses=sum(r['status']=='valid' for r in rows),status_counts=dict(db.execute('SELECT status,count(*) FROM calls GROUP BY status')),
            recorded_actual_usd=str(sum((Decimal(r['actual']) for r in rows if r['actual'] is not None),Decimal(0))),
            unconfirmed_reservations=[dict(call_id=r['id'],reserved_usd=r['reserved'],status=r['status']) for r in rows if r['actual'] is None])
    result['io_ledgers']={}
    for name,relative,table in (
        ('original_core','evidence/core-trace-v1/io-extension.sqlite3','reservations'),
        ('legacy_epoch685','evidence/address-epoch-v1/io.sqlite3','requests'),
        ('shared_history_expansion',FOLDER+'/io.sqlite3','reservations')):
        p=base/relative
        if p.exists():
            with closing(read_db(p)) as db:result['io_ledgers'][name]=dict(
                path=relative,reserved_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM '+table).fetchone()[0])
    for name,relative in (
        ('workers','wallet-map-v1/workers.json'),('acquisition','address-epochs-v2/status.json'),
        ('index_registry','address-epochs-v2/registry.json')):
        p=base/'evidence'/relative
        if p.exists():
            v=json.loads(p.read_bytes())
            if name=='workers':v={k:v.get(k) for k in ('configured','active','observed_peak_concurrent','provider_calls_started','provider_calls_completed','updated_ms','batch_ms')}
            result[name]=v
    result['research_storage_bytes']=used_bytes(base)
    result.update(complete_solana_history=False,cex_internal_ledgers_recovered=False,
        independent_consensus_verified=False,orders_submitted=0,public_deployment=False)
    if before:
        result['original_grants_unchanged']=result['original_grants']==before['original_grants']
        result['graph_prefixes_unchanged']=result['graph_prefixes']==before['graph_prefixes']
        result['model_prefix_unchanged']=result['model_prefix']==before['model_prefix']
    folder=base/FOLDER;folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'review-{now}.json';atomic(path,result)
    return dict(path=str(path),**{k:result[k] for k in ('at_ms','graph_counts','model','workers') if k in result},
        **{k:result[k] for k in ('original_grants_unchanged','graph_prefixes_unchanged','model_prefix_unchanged') if k in result})


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    p.add_argument('--authorize',action='store_true');p.add_argument('--baseline');a=p.parse_args()
    result=authorize(Path(a.base)) if a.authorize else snapshot(Path(a.base),a.baseline)
    print(json.dumps(result))


if __name__=='__main__':main()
