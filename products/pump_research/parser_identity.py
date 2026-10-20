"""Recover signatures from a bounded prefix of already verified COPY bytes.

One v1 and one v2 row with identical signature, account identities, execution
coordinates and exact amounts form one source-event group. No other pair is
merged. This verifies source duplication, not canonical on-chain execution.
"""
import argparse
from collections import defaultdict,Counter
import hashlib
import json
from pathlib import Path
import time
from economic_machine.pump_research.history_core import Scope,Chunks,Stream,stream_rows,project,sha
from economic_machine.pump_research.history_dump import columns
from economic_machine.pump_research.store import atomic,encode,canonical


class CachedOnly(Chunks):
    def part(self,part,start,size):
        ident=hashlib.sha256(f'{part["download_url"]}:{start}:{size}'.encode()).hexdigest()
        if not (self.folder/(ident+'.range')).is_file() or not (self.folder/(ident+'.json')).is_file():raise ValueError('CACHE_ONLY_NO_NETWORK')
        return super().part(part,start,size)


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);scope=Scope(base,base/'evidence/core-trace-v1/grant.json');out=base/'evidence/wallet-map-v1'
    try:
        source=base/'evidence/history-alpha-v1'
        for name,key in [('core-read-plan.json','plan_sha256'),('factories-toc.json','toc_sha256'),('figshare.json','parts_sha256')]:
            if sha(source/name)!=scope.grant[key]:raise ValueError('SOURCE_IDENTITY')
        toc=json.loads((source/'factories-toc.json').read_bytes());plan=json.loads((source/'core-read-plan.json').read_bytes())
        parts=sorted((r for r in json.loads((source/'figshare.json').read_bytes())['files'] if r['name'].startswith('dump.pgcustom.part-')),key=lambda r:r['name'])
        entry=next(e for e in toc['entries'] if e['tag']=='pump_event_default' and e['description']=='TABLE DATA')
        row=next(r for r in plan['core_tables'] if r['table']=='pump_event_default')
        positions={v:i for i,v in enumerate(columns(entry))};stream=Stream(CachedOnly(scope,parts).range(entry['offset'],row['compressed_bytes']))
        groups=defaultdict(list);versions=Counter();selected=[]
        for n,cells in enumerate(stream_rows(stream,entry),1):
            scope.guard();v=project('pump_event_default',cells,positions)
            mint,trader,signature,stamp,slot,outer,inner,ordinal,side,sol,tokens,decimals,version=v
            ident=hashlib.sha256(canonical([encode(signature),encode(mint),encode(trader),slot,outer,ordinal,side,sol,tokens,decimals]).encode()).hexdigest()
            record=dict(rowid=n,parse_version=version,original_signature=encode(signature),event_identity=ident,inner_ix=inner)
            groups[ident].append(record);versions[version]+=1
            if n in (844,845,5228,5229):selected.append(dict(record,mint=encode(mint),trader=encode(trader),slot=slot,sol_raw=sol,token_raw=tokens))
            if n>=6000:break
        pairs=[v for v in groups.values() if len(v)==2 and {r['parse_version'] for r in v}=={1,2}]
        aliases={str(r['rowid']):r for pair in pairs for r in pair}
        report=dict(schema='economic-machine.source-event-identity.v1',at_ms=time.time_ns()//1000000,rows_examined=n,
            source_compressed_sha256=json.loads((scope.out/'pump_event_default-extraction.json').read_bytes())['compressed_sha256'],
            matched_parser_pairs=len(pairs),parse_versions=dict(versions),identities=aliases,selected_original_records=selected,
            new_network_bytes=0,full_population_rate_not_estimated=True,independent_chain_verification=False)
        version=out/f'source-identities-{report["at_ms"]}.json';atomic(version,report);atomic(out/'source-identities.json',report)
        print(json.dumps(dict(path=str(version),rows=n,matched_parser_pairs=len(pairs),new_network_bytes=0)))
    finally:scope.close()

if __name__=='__main__':main()
