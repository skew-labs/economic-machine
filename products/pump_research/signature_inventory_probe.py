"""Bounded two-epoch structural count probe using retained GSFA root CIDs."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import time
import urllib.request

from economic_machine.pump_research.archive import NoRedirect
from economic_machine.pump_research.archive_gate import ArchiveGate,ArchiveDeferred
from economic_machine.pump_research.gsfa_index import _meta
from economic_machine.pump_research.research_expansion import ExpansionScope
from economic_machine.pump_research.research_spend import Spend
from economic_machine.pump_research.signature_inventory import header,count_entries,MAX_TABLE_BYTES
from economic_machine.pump_research.store import atomic

DEADLINE=1792351694298


def run(base,epochs):
    base=Path(base);out=base/'evidence/solana-workload-v1/signature-counts';out.mkdir(exist_ok=True)
    if not 1<=len(epochs)<=2 or len(set(epochs))!=len(epochs):raise ValueError('SIGCOUNT_EPOCH_BOUND')
    scope=ExpansionScope(base);spend=Spend(base);gate=base/'evidence/wallet-map-v1/archive-ranges'
    results=[]
    try:
        for epoch in epochs:
            if type(epoch) is not int:raise ValueError('SIGCOUNT_EPOCH')
            scope.continuation.assert_epoch(epoch)
            folder=base/'evidence/address-epochs-v2/epochs'/str(epoch)
            receipt=json.loads((folder/'import-receipt.json').read_bytes())
            path=folder/receipt['directory']/'manifest';manifest=path.read_bytes()
            expected=next(r for r in receipt['extraction']['files'] if r['name']=='manifest')
            if hashlib.sha256(manifest).hexdigest()!=expected['sha256']:raise ValueError('SIGCOUNT_RETAINED_MANIFEST_HASH')
            if manifest[:16]!=b'gsfamnfs'+(5).to_bytes(8,'little'):raise ValueError('SIGCOUNT_RETAINED_MANIFEST_MAGIC')
            meta,end=_meta(manifest,16);root=meta[b'rootCid']
            if end!=len(manifest) or len(root)!=36 or not root.startswith(b'\x01\x71\x12\x20') or meta[b'epoch']!=epoch.to_bytes(8,'little'):
                raise ValueError('SIGCOUNT_RETAINED_MANIFEST_IDENTITY')
            cid='b'+base64.b32encode(root).decode().lower().rstrip('=')
            url=f'https://files.old-faithful.net/{epoch}/epoch-{epoch}-{cid}-mainnet-sig-to-cid.index'
            def read(start,size,pin=None):
                if not 0<size<=MAX_TABLE_BYTES:raise ValueError('SIGCOUNT_RANGE_BOUND')
                path=out/f'{epoch}-{start}-{size}.bin';receipt_path=path.with_suffix('.json')
                if path.exists() and receipt_path.exists():
                    saved=json.loads(receipt_path.read_bytes());raw=path.read_bytes()
                    if saved['url']!=url or saved['start']!=start or saved['bytes']!=size or len(raw)!=size or hashlib.sha256(raw).hexdigest()!=saved['sha256']:
                        raise ValueError('SIGCOUNT_CACHE_IDENTITY')
                    identity=saved['source_identity']
                    if pin is not None and pin!=identity:raise ValueError('SIGCOUNT_SOURCE_CHANGED')
                    return raw,identity
                started=time.monotonic();stamp=time.time_ns()//1000000
                def guard():
                    if time.time_ns()//1000000>=DEADLINE:raise ValueError('SIGCOUNT_DEADLINE')
                    if time.monotonic()-started>15:raise ArchiveDeferred(time.time_ns()//1000000+1000,'bounded_metadata_wait')
                    scope.guard()
                with ArchiveGate(gate,guard,wait=True):
                    reservation=scope.reserve(size+1,f'solana-signature-count:{epoch}:{start}:{stamp}')
                    spend.reserve('read',f'solana-signature-count:{epoch}:{start}:{stamp}',bytes_=size+1)
                    attempt=out/f'attempt-{epoch}-{stamp}.json'
                    atomic(attempt,dict(epoch=epoch,start=start,bytes=size,reservation=reservation,state='reserved',at_ms=stamp))
                    req=urllib.request.Request(url,headers={'Range':f'bytes={start}-{start+size-1}',
                        'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'})
                    with urllib.request.build_opener(NoRedirect).open(req,timeout=20) as r:
                        match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.headers.get('Content-Range',''))
                        if r.status!=206 or match is None or tuple(map(int,match.groups()[:2]))!=(start,start+size-1):raise ValueError('SIGCOUNT_RANGE_RESPONSE')
                        identity=dict(total_bytes=int(match[3]),etag=r.headers.get('ETag'))
                        if pin is not None and pin!=identity:raise ValueError('SIGCOUNT_SOURCE_CHANGED')
                        raw=r.read(size+1)
                        if len(raw)!=size:raise ValueError('SIGCOUNT_RESPONSE_SIZE')
                path.write_bytes(raw)
                saved=dict(url=url,start=start,bytes=size,sha256=hashlib.sha256(raw).hexdigest(),source_identity=identity,at_ms=stamp)
                atomic(receipt_path,saved);atomic(attempt,dict(epoch=epoch,start=start,bytes=size,reservation=reservation,state='retained',at_ms=stamp))
                return raw,identity
            try:
                raw,identity=read(0,512);h=header(raw,epoch=epoch,root_cid=root)
                table,identity=read(h['header_bytes'],h['bucket_table_bytes'],identity)
                result=count_entries(h,table,identity['total_bytes'])
                result.update(index_url=url,index_identity=identity,at_ms=time.time_ns()//1000000,
                    source_manifest_sha256=hashlib.sha256(manifest).hexdigest(),metadata_bytes_read=512+len(table))
                atomic(out/f'epoch-{epoch}.json',result);results.append(result)
            except ArchiveDeferred as error:
                atomic(out/f'deferred-{epoch}-{time.time_ns()//1000000}.json',dict(epoch=epoch,retry_at_ms=error.retry_at_ms,state='SOURCE_DEFERRED'))
                break
            except Exception as error:
                atomic(out/f'error-{epoch}-{time.time_ns()//1000000}.json',dict(epoch=epoch,state='FAILED_RETAINED',
                    error_type=type(error).__name__,error=str(error)[:160],http_status=getattr(error,'code',None)))
                raise
        atomic(out/'summary.json',dict(at_ms=time.time_ns()//1000000,sampled_epochs=results,
            provider_signature_entries=sum(r['provider_signature_entries'] for r in results),
            all_epoch_count_known=False,canonical_transaction_denominator_verified=False))
        return results
    finally:scope.close();spend.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--epochs',default='1009,1010');a=p.parse_args()
    print(json.dumps(run(a.base,[int(v) for v in a.epochs.split(',')])))
