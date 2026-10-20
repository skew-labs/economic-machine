"""Owner-requested offline CAR export canary and read-only private query CLI."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from economic_machine.pump_research.cli import proto
from economic_machine.pump_research.solana_export import Facts, file_hash, search, archive_pack
from economic_machine.pump_research.store import atomic, canonical

DEADLINE = 1792351694298


def prepare(base):
    out = base/'evidence/solana-export-route-v1'; out.mkdir(parents=True, exist_ok=True)
    policy = dict(schema='economic-machine.solana-export-policy.v1',
        authorization='owner_full_solana_transfer_route_20261016',deadline_ms=DEADLINE,
        first_slot=436099800,last_slot=436099803,maximum_output_bytes=512*1024**2,
        host_free_reserve_bytes=128*1024**3,cloud_limit_bytes=4*1024**4,
        monthly_limit_krw=1000000,source='bulk-car-continuation-v1',
        provider_requests=0,model_calls=0,services_restarted=False,
        parent_pins={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in (base/'evidence/solana-continuation-v1/grant.json',base/'evidence/research-budget-v1/grant.json')})
    p=out/'policy.json'
    if p.exists():
        if json.loads(p.read_bytes()) != policy: raise ValueError('EXPORT_POLICY_CHANGED')
    else:
        atomic(p,policy);(out/'policy.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'\n')
    return out,policy


def run(base):
    out,policy=prepare(base);policy_hash=hashlib.sha256((out/'policy.json').read_bytes()).hexdigest()
    def guard():
        if time.time_ns()//1000000 >= DEADLINE: raise ValueError('EXPORT_DEADLINE')
        if hashlib.sha256((out/'policy.json').read_bytes()).hexdigest()!=policy_hash or (out/'policy.sha256').read_text().strip()!=policy_hash:
            raise ValueError('EXPORT_POLICY_CHANGED')
        if shutil.disk_usage(out).free < policy['host_free_reserve_bytes']: raise ValueError('EXPORT_HOST_FREE_RESERVE')
        used=sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
        if used>policy['maximum_output_bytes']:raise ValueError('EXPORT_OUTPUT_LIMIT')
    guard()
    for rel,digest in policy['parent_pins'].items():
        if hashlib.sha256((base/rel).read_bytes()).hexdigest()!=digest:raise ValueError('EXPORT_PARENT_CHANGED')
    parent=base/'evidence'/policy['source'];db=sqlite3.connect((parent/'runtime.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    pages=[json.loads(r[0]) for r in db.execute("SELECT body FROM pages WHERE state='indexed' ORDER BY n")];db.close()
    _,meta=proto('/srv/economic-machine/collector');reward=importlib.import_module(meta.__module__).Rewards
    facts=Facts(out,policy,guard);results=[];source_checks=[];started=time.monotonic()
    try:
        for page in pages:
            blocks=[r for r in page['blocks'] if policy['first_slot']<=r['slot']<=policy['last_slot']]
            if not blocks:continue
            guard();r=page['raw'];ident=hashlib.sha256(f"{r['source_url']}:{r['offset']}:{r['bytes']}".encode()).hexdigest()
            path=parent/'ranges'/(ident+'.raw')
            if (path.stat().st_size!=r['bytes'] or file_hash(path,32*1024**2)!=r['sha256'] or
                r['blob']['sha256']!=r['sha256'] or r['blob']['bytes']!=r['bytes']):raise ValueError('EXPORT_RETAINED_SOURCE_HASH')
            source_checks.append(dict(source_page_sha256=r['sha256'],page_receipt_sha256=hashlib.sha256(canonical(page).encode()).hexdigest(),bytes=r['bytes'],retained_blob_receipt=True,fresh_blob_readback=False))
            with path.open('rb') as stream:
                for block in blocks:
                    guard();stream.seek(block['offset']-r['offset']);raw=stream.read(block['bytes'])
                    if hashlib.sha256(raw).hexdigest()!=block['car_sha256']:raise ValueError('EXPORT_SOURCE_SLICE_HASH')
                    got=facts.export(raw,block['slot'],meta,reward,
                        archive=lambda path,digest:archive_pack(base,path,digest,guard))
                    if got['transactions']!=block['transactions']:raise ValueError('EXPORT_EXISTING_TRANSACTION_COUNT')
                    results.append(got)
        if {r['slot'] for r in results}!=set(range(policy['first_slot'],policy['last_slot']+1)):
            raise ValueError('EXPORT_RETAINED_RANGE_MISSING')
        report=dict(at_ms=time.time_ns()//1000000,policy_sha256=policy_hash,
            state='APPROVED_RETAINED_RANGE_SEARCHABLE',blocks=results,source_checks=source_checks,
            elapsed_seconds=time.monotonic()-started,provider_requests=0,model_calls=0,
            transactions=facts.db.execute('SELECT count(*) FROM transactions').fetchone()[0],
            address_appearances=facts.db.execute('SELECT count(*) FROM appearances').fetchone()[0],
            full_history_complete=False,source_side_transfer_acceleration_proven=False)
        atomic(out/f'run-{report["at_ms"]}.json',report);atomic(out/'status.json',report);return report
    finally:facts.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--base');p.add_argument('--run',action='store_true')
    p.add_argument('--search');p.add_argument('--database');p.add_argument('--limit',type=int,default=20);a=p.parse_args()
    if a.search:
        if not a.database or Path(a.database).name!='facts.sqlite3':p.error('--database must identify facts.sqlite3')
        result=search(Path(a.database).parent,a.search,a.limit)
        print(json.dumps(dict(events=result['hits'],coverage=dict(indexed_blocks=result['indexed_blocks'],full_wallet_history=False,independent_consensus_verified=False),
            gaps=['Only the explicitly exported retained blocks are indexed.','Internal instruction commitment may remain unresolved.','Token-owner boundary observations do not establish common ownership.'],
            truncated=result['truncated'],current_balance_verified=False)))
    elif a.run and a.base:print(json.dumps(run(Path(a.base))))
    else:p.error('choose --run --base or --search --database')


if __name__=='__main__':main()
