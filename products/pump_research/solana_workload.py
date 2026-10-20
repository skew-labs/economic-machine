"""Bounded metadata inventory; no CAR bodies, collector mutation, or new epochs."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

from economic_machine.pump_research.archive import NoRedirect
from economic_machine.pump_research.archive_gate import ArchiveGate, ArchiveDeferred
from economic_machine.pump_research.research_expansion import ExpansionScope
from economic_machine.pump_research.research_spend import Spend
from economic_machine.pump_research.store import atomic
from economic_machine.pump_research.workload_inventory import (
    parse_report, estimate, interval_rate, GENERATOR_SHA256, GENERATOR_COMMIT)

DEADLINE=1792351694298


def document(path, limit=1024**2):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('WORKLOAD_DOCUMENT_BOUND')
    return json.loads(path.read_bytes())


def readonly(path):
    return sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=3)


def acquisition(base):
    status=document(base/'evidence/address-epochs-v2/status.json')
    return dict(at_ms=status['at_ms'],active=status['active_epochs'],
        imported_epochs=status['imported_epochs'],legacy_imports_reused=status['legacy_imports_reused'])


def collect_heads(base,out,rows,epochs):
    by_epoch={r['epoch']:r for r in rows}
    if len(epochs)>16 or len(epochs)!=len(set(epochs)):raise ValueError('WORKLOAD_HEAD_BOUND')
    scope=ExpansionScope(base);spend=Spend(base)
    def guard():
        if time.time_ns()//1000000>=DEADLINE:raise ValueError('WORKLOAD_DEADLINE')
        scope.guard()
    results=[]
    try:
        for epoch in epochs:
            if epoch not in by_epoch or not by_epoch[epoch]['car_url']:raise ValueError('WORKLOAD_HEAD_SCOPE')
            prior=out/f'head-{epoch}.json'
            if prior.exists():results.append(document(prior));continue
            stamp=time.time_ns()//1000000;name=f'head-{epoch}-{stamp}'
            url=by_epoch[epoch]['car_url']
            attempt=dict(epoch=epoch,url=url,at_ms=stamp,state='before_admission')
            try:
                with ArchiveGate(base/'evidence/wallet-map-v1/archive-ranges',guard,wait=False,success_status=200):
                    attempt['reservation']=scope.reserve(4097,'solana-workload:'+name)
                    spend.reserve('read','solana-workload:'+name,bytes_=4097)
                    attempt['state']='reserved';atomic(out/(name+'.attempt.json'),attempt)
                    req=urllib.request.Request(url,method='HEAD',headers={'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'})
                    with urllib.request.build_opener(NoRedirect).open(req,timeout=30) as response:
                        body=response.read(4097)
                        size=response.headers.get('Content-Length','')
                        if response.status!=200 or body or not size.isdigit() or int(size)<=0:raise ValueError('WORKLOAD_HEAD_RESPONSE')
                        value=dict(epoch=epoch,url=url,bytes=int(size),at_ms=stamp,
                            etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'),
                            provider_identity_only=True,source_body_verified=False)
                atomic(prior,value);attempt['state']='complete';results.append(value)
            except ArchiveDeferred as error:
                attempt.update(state='deferred',retry_at_ms=error.retry_at_ms)
                atomic(out/(name+'.attempt.json'),attempt);break
            except Exception as error:
                attempt.update(state='failed_retained',error_type=type(error).__name__,http_status=getattr(error,'code',None))
                atomic(out/(name+'.attempt.json'),attempt);break
            atomic(out/(name+'.attempt.json'),attempt)
    finally:scope.close();spend.close()
    return results


def run(base,heads=()):
    base=Path(base);out=base/'evidence/solana-workload-v1';out.mkdir(exist_ok=True)
    if time.time_ns()//1000000>=DEADLINE:raise ValueError('WORKLOAD_DEADLINE')
    receipt=document(base/'evidence/address-epochs-v2/catalogue-receipt.json')
    raw_path=base/'evidence/address-epochs-v2'/receipt['path'];raw=raw_path.read_bytes()
    if len(raw)>1024**2 or hashlib.sha256(raw).hexdigest()!=receipt['sha256']:raise ValueError('WORKLOAD_CATALOGUE_HASH')
    rows=parse_report(raw)
    generator=base/'evidence/workload-inventory-v1'/f'{GENERATOR_SHA256}.raw'
    if hashlib.sha256(generator.read_bytes()).hexdigest()!=GENERATOR_SHA256:raise ValueError('WORKLOAD_GENERATOR_HASH')
    baseline=out/'acquisition-before.json'
    if not baseline.exists():atomic(baseline,acquisition(base))
    if heads:collect_heads(base,out,rows,list(heads))
    exact=[document(p) for p in sorted(out.glob('head-*.json')) if not p.name.endswith('.attempt.json')]
    budget=document(base/'evidence/research-budget-v1/grant.json')
    now=time.time_ns()//1000000
    status=acquisition(base);old=document(baseline);rate=None
    if old['active'] and status['active']:
        a=dict(old['active'][0],at_ms=old['at_ms']);b=dict(status['active'][0],at_ms=status['at_ms'])
        if a['epoch']==b['epoch'] and b['at_ms']>a['at_ms']:rate=interval_rate(a,b)
    planner=readonly(base/'evidence/address-epochs-v2/planner.sqlite3')
    try:states=dict(planner.execute('SELECT state,count(*) FROM epochs GROUP BY state'))
    finally:planner.close()
    existing={};measurements=[];provider_sizes={r['epoch']:r for r in exact}
    for name in ['bulk-car-v1','bulk-car-fast-v1','bulk-car-continuation-v1']:
        folder=base/'evidence'/name
        if not (folder/'runtime.sqlite3').exists():continue
        d=readonly(folder/'runtime.sqlite3')
        try:
            for url,body in d.execute('SELECT url,body FROM sources'):
                if not url.endswith('.car'):continue
                epoch=int(url.split('/')[-2]);size=json.loads(body)['total_bytes']
                record=dict(epoch=epoch,url=url,bytes=size,source_evidence=str(folder.relative_to(base))+'/runtime.sqlite3',
                            source_kind='retained_validated_http_range_content_length',entire_car_body_verified=False)
                if epoch in provider_sizes and provider_sizes[epoch]['bytes']!=size:raise ValueError('WORKLOAD_PROVIDER_SIZE_CONFLICT')
                provider_sizes.setdefault(epoch,record)
            for slot,digest,txs in d.execute('SELECT slot,hash,transactions FROM indexed_blocks'):
                if slot in existing and existing[slot]!=(digest,txs):raise ValueError('WORKLOAD_INDEX_CONFLICT')
                existing[slot]=(digest,txs)
            first=d.execute('SELECT min(at_ms) FROM attempts').fetchone()[0]
        finally:d.close()
        s=document(folder/'status.json');elapsed=s.get('elapsed_seconds')
        if elapsed is None and first is not None:elapsed=(now-first)/1000
        measurements.append(dict(path=str(folder.relative_to(base)),state=s['state'],indexed_blocks=s['indexed_blocks'],
            decoded_transactions=s['decoded_transactions'],elapsed_seconds=elapsed,
            blocks_per_second=s['indexed_blocks']/elapsed if elapsed else None,
            elapsed_includes_waits_and_current_stalls='elapsed_seconds' not in s,
            full_search_and_blob_path=True,not_global_or_representative_rate=True))
    model=estimate(rows,known_exact=list(provider_sizes.values()),available_seconds=(DEADLINE-now)/1000,
        cloud_cap_bytes=budget['maximum_cloud_bytes'],storage_krw_per_gib=budget['storage_krw_per_gib_month'],
        read_krw_per_gib=budget['internet_read_krw_per_gib'])
    observed=None;live_receipt_path=base/'evidence/workload-inventory-v1/live-report-receipt.json'
    if live_receipt_path.exists():
        live_receipt=document(live_receipt_path);live_raw=(live_receipt_path.parent/live_receipt['path']).read_bytes()
        if hashlib.sha256(live_raw).hexdigest()!=live_receipt['sha256']:raise ValueError('WORKLOAD_LIVE_CATALOGUE_HASH')
        live_rows=parse_report(live_raw);old_rows={r['epoch']:r for r in rows}
        observed=dict(receipt=live_receipt,inventory_mutated=False,
            availability_changes=[dict(epoch=r['epoch'],old=old_rows[r['epoch']]['provider_state'],new=r['provider_state'])
                for r in live_rows if r['epoch'] in old_rows and r['provider_state']!=old_rows[r['epoch']]['provider_state']])
    result=dict(schema='economic-machine.solana-workload.v1',at_ms=now,deadline_ms=DEADLINE,
        catalogue_receipt=receipt,generator_commit=GENERATOR_COMMIT,generator_sha256=GENERATOR_SHA256,
        provider_size_evidence=list(provider_sizes.values()),current_provider_catalogue=observed,
        whole_inventory=model,acquisition=status,acquisition_observation=rate,epoch_states=states,
        direct_bulk=dict(nonoverlapping_blocks=len(existing),transactions=sum(v[1] for v in existing.values()),
                         observations=measurements),
        shared_budget=document(base/'evidence/research-budget-v1/status.json'),
        mapping_projection=dict(implemented_full_history=False,source_bytes_saved_proven=False,
            derived_size=None,required_fetch_bytes=None,
            reason='Filtering after decode may reduce retained projection, not bytes needed to inspect every transaction. Pure vote records still carry fees and account changes.'),
        original_grants_and_ledgers_preserved=True,production_changed=False,
        completion_possible_under_current_evidence=False)
    signature_summary=out/'signature-counts/summary.json'
    if signature_summary.exists():result['signature_index_inventory']=document(signature_summary)
    census_summary=out/'signature-census/summary.json'
    if census_summary.exists():result['signature_index_census']=document(census_summary)
    atomic(out/f'snapshot-{now}.json',result);atomic(out/'summary.json',result)
    atomic(out/'epochs.json',dict(catalogue_sha256=receipt['sha256'],rows=rows))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True)
    parser.add_argument('--head-epochs',default='');args=parser.parse_args()
    heads=[int(v) for v in args.head_epochs.split(',') if v]
    result=run(args.base,heads)
    print(json.dumps(dict(at_ms=result['at_ms'],whole_inventory=result['whole_inventory'],
                         acquisition_observation=result['acquisition_observation'],direct_bulk=result['direct_bulk'])))
