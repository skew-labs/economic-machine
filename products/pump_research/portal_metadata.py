"""Metered public provider metadata for research; no canonicality claims."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.portal_history import portal_network,RETRY_STATUSES
from economic_machine.pump_research.store import atomic

HOST='https://portal.sqd.dev/datasets/solana-mainnet'

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);a=p.parse_args()
    base=Path(a.base);out=base/'evidence/recent50-v1'
    scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    path=out/'portal-metadata-ledger.json'
    ledger=json.loads(path.read_bytes()) if path.exists() else dict(schema='economic-machine.portal-metadata-ledger.v1',requests=[])
    def fetch(endpoint,body=None):
        for r in ledger['requests']:
            if r['endpoint']==endpoint and r.get('request_body')==body and r.get('status')==200:
                raw=(out/r['raw_file']).read_bytes()
                if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('PORTAL_METADATA_HASH')
                return json.loads(raw)
        cap=256*1024 if body else 65536
        if sum(r['reserved_bytes'] for r in ledger['requests'])+cap+1>4*1024**2:raise ValueError('PORTAL_METADATA_CAP')
        scope.reserve(cap+1,'portal_metadata:'+endpoint)
        row=dict(id=len(ledger['requests'])+1,endpoint=endpoint,request_body=body,reserved_bytes=cap+1,at_ms=time.time_ns()//1000000,status='reserved')
        ledger['requests'].append(row);atomic(path,ledger)
        request=urllib.request.Request(HOST+endpoint,data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json','User-Agent':'EconomicMachine-Research/1.0'})
        try:
            with portal_network(base,scope):
                with urllib.request.urlopen(request,timeout=30) as r:raw=r.read(cap+1);row['status']=r.status
            row.update(response_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),raw_file=f'portal-meta-{row["id"]:04}.json')
            (out/row['raw_file']).write_bytes(raw);atomic(path,ledger)
            if len(raw)>cap:raise ValueError('PORTAL_METADATA_RESPONSE_BOUND')
            return json.loads(raw)
        except Exception as e:
            row['error']=type(e).__name__
            if isinstance(e,urllib.error.HTTPError):row.update(status=e.code,retry_after=e.headers.get('Retry-After'))
            atomic(path,ledger);raise
    def retry(endpoint,body=None):
        while True:
            try:return fetch(endpoint,body)
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_STATUSES:raise
                # Shared admission waits before the next network dispatch.
                scope.guard()
    try:
        head=retry('/finalized-head');atomic(out/'portal-head.json',dict(head,at_ms=time.time_ns()//1000000,evidence_tier='provider_claim'))
        probe=retry('/finalized-stream',dict(type='solana',fromBlock=296264063,toBlock=296264063,fields=dict(block=dict(number=True,hash=True,timestamp=True),transaction=dict(transactionIndex=True,signatures=True,accountKeys=True,loadedAddresses=True,err=True)),transactions=[dict(mentionsAccount=['HyYNVYmnFmi87NsQqWzLJhUTPBKQUfgfhdbBa554nMFF'])]))
        print(json.dumps(dict(probe_header=probe.get('header'),transaction=probe.get('transactions',[])[:1])),flush=True)
        snap=json.loads((out/'snapshot.json').read_bytes());anchors={}
        for item in snap['selected']:
            answer=retry('/timestamps/'+str(item['created_ms']//1000)+'/block')
            slot=answer['block_number']
            if type(slot) is not int or not 0<=slot<=head['number']:raise ValueError('PORTAL_METADATA_SLOT')
            anchors[item['mint']]=dict(slot=slot,created_ms=item['created_ms'],source='portal_timestamp_provider_claim',start=max(0,slot-8),end=slot+16,canonical_verified=False)
            atomic(out/'launch-anchors.json',anchors)
            time.sleep(.5)
        print(json.dumps(dict(state='ANCHORS_READY',head=head['number'],anchors=len(anchors))),flush=True)
    finally:scope.close()

if __name__=='__main__':main()
