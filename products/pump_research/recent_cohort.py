"""Freeze a public, incomplete current-cap sample; never submit transactions."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request

from economic_machine.pump_research.cohort import build_snapshot, snapshot_sha256, AUTHORIZATION
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.store import atomic


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);out=base/'evidence/recent50-v1';out.mkdir(exist_ok=True)
    scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    frozen=out/'snapshot.json'
    if frozen.exists():
        print(json.dumps({'state':'SNAPSHOT_PRESERVED','sha256':snapshot_sha256(json.loads(frozen.read_bytes()))}));return
    cutoff=out/'cutoff.json'
    if not cutoff.exists():atomic(cutoff,{'as_of_ms':time.time_ns()//1000000})
    asof=json.loads(cutoff.read_bytes())['as_of_ms'];pages=[]
    try:
        for offset in range(0,500,50):
            scope.guard()
            path=out/f'cap-page-{offset:04}.json';receipt=path.with_suffix('.receipt.json')
            url=f'https://frontend-api-v3.pump.fun/coins?offset={offset}&limit=50&sort=market_cap&order=DESC&includeNsfw=true'
            if not receipt.exists():
                scope.reserve(2*1024**2+1,'recent50:current_market_cap_page:'+str(offset))
                at=time.time_ns()//1000000
                with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'EconomicMachine-Research/1.0'}),timeout=25) as r:
                    raw=r.read(2*1024**2+1);status=r.status
                path.write_bytes(raw)
                atomic(receipt,dict(url=url,at_ms=at,status=status,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),reserved_bytes=2*1024**2+1))
                if len(raw)>2*1024**2:raise ValueError('COHORT_RESPONSE_BOUND')
                time.sleep(1)
            info=json.loads(receipt.read_bytes());raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=info['sha256']:raise ValueError('COHORT_RESPONSE_HASH')
            rows=json.loads(raw,parse_float=str)
            if not isinstance(rows,list):raise ValueError('COHORT_RESPONSE_ROWS')
            for row in rows:
                row['timestamp_unit']='ms'
                if row.get('chain_id')!='solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp':raise ValueError('COHORT_CHAIN')
            pages.append(dict(provider='pump.fun_public_frontend',fetched_ms=info['at_ms'],sha256=info['sha256'],rows=rows,url=url,raw_path=path.name,provider_sort='market_cap_quote_units'))
            if len(rows)<50:break
        snapshot=build_snapshot(pages,asof)
        atomic(frozen,snapshot)
        grant=dict(authorization=AUTHORIZATION,cutoff_ms=asof,window_start_ms=snapshot['window_start_ms'],snapshot_sha256=snapshot_sha256(snapshot),deadline_ms=scope.grant['deadline_ms'],trading=False,paid_api=False,parent_grant_sha256=scope.hash,total_storage_bytes=scope.storage_limit,maximum_cpu_percent=100,model='meta/muse-spark-1.3-contributor',total_model_call_limit=None,total_model_cost_limit=None,authority='Owner requested original10 plus recent30day current-marketcap50; public research only; original resource and time limits preserved.')
        atomic(out/'grant.json',grant)
        (out/'grant.sha256').write_text(hashlib.sha256((out/'grant.json').read_bytes()).hexdigest()+'\n')
        print(json.dumps(dict(state='SNAPSHOT_READY',selected=len(snapshot['selected']),rows=snapshot['rows_observed'],eligible=snapshot['eligible_distinct_mints'],ranking_verified=False,sha256=snapshot_sha256(snapshot))))
    finally:scope.close()


if __name__=='__main__':main()
