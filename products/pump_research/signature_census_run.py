"""Single low-priority metadata worker over the already authorized inventory.

Each admitted response is reserved in the original continuation and shared
spending ledgers. Index bodies and CAR transaction bodies are never requested.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid

from economic_machine.pump_research.archive import NoRedirect
from economic_machine.pump_research.archive_gate import ArchiveGate,ArchiveDeferred
from economic_machine.pump_research.gsfa_index import _meta
from economic_machine.pump_research.research_expansion import ExpansionScope
from economic_machine.pump_research.research_spend import Spend
from economic_machine.pump_research.signature_census import Census
from economic_machine.pump_research.signature_inventory import header,count_entries,MAX_TABLE_BYTES
from economic_machine.pump_research.store import atomic,canonical

DEADLINE=1792351694298
FOLDER='evidence/solana-workload-v1/signature-census'
MAX_CID_BYTES=4096


def document(path,limit=65536):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('SIGCENSUS_DOCUMENT_BOUND')
    return json.loads(path.read_bytes())


def cid_text(root):
    if len(root)!=36 or root[:4]!=b'\x01\x71\x12\x20':raise ValueError('SIGCENSUS_ROOT_CID_LAYOUT')
    return 'b'+base64.b32encode(root).decode().lower().rstrip('=')


def decode_cid(raw):
    text=raw.decode('ascii').strip()
    if len(raw)>MAX_CID_BYTES or re.fullmatch('b[a-z2-7]{58}',text) is None:
        raise ValueError('SIGCENSUS_ROOT_CID_TEXT')
    encoded=text[1:].upper();root=base64.b32decode(encoded+'='*((-len(encoded))%8))
    if cid_text(root)!=text:raise ValueError('SIGCENSUS_ROOT_CID_CANONICAL')
    return root


def durable_bytes(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.pending')
    with temp.open('wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def policy_for(scope,spend):
    if scope.continuation is None:raise ValueError('SIGCENSUS_CONTINUATION_REQUIRED')
    return dict(schema='economic-machine.signature-census-policy.v1',
        authorization='owner_parallel_solana_full_workload_inventory_20261016',
        deadline_ms=DEADLINE,continuation_sha256=scope.continuation.hash,
        budget_sha256=spend.hash,catalogue_sha256=scope.continuation.grant['inventory_catalogue_sha256'],
        epochs=sorted(scope.continuation.inventory_epochs),
        max_cid_bytes=MAX_CID_BYTES,max_header_bytes=512,max_bucket_table_bytes=MAX_TABLE_BYTES,
        source_parallelism=1,provider_cooldown_preserved=True,metadata_only=True,
        transaction_bodies=False,car_bodies=False,new_servers=False,paid_api=False,
        existing_io_and_spending_ledgers=True,deadline_extension=False)


class Worker:
    def __init__(self,base,initialize=False):
        self.base=Path(base);self.out=self.base/FOLDER;self.out.mkdir(parents=True,exist_ok=True)
        self.scope=ExpansionScope(self.base);self.spend=Spend(self.base)
        expected=policy_for(self.scope,self.spend);path=self.out/'policy.json'
        if not path.exists():
            if not initialize:raise ValueError('SIGCENSUS_POLICY_MISSING')
            if time.time_ns()//1000000>=DEADLINE:raise ValueError('SIGCENSUS_DEADLINE')
            atomic(path,expected)
            durable_bytes(path.with_suffix('.sha256'),(hashlib.sha256(path.read_bytes()).hexdigest()+'\n').encode())
        raw=path.read_bytes()
        if document(path)!=expected or path.with_suffix('.sha256').read_text().strip()!=hashlib.sha256(raw).hexdigest():
            raise ValueError('SIGCENSUS_AUTHORITY_CHANGED')
        self.ledger=Census(self.out,expected);self.gate=self.base/'evidence/wallet-map-v1/archive-ranges'
        self.opener=urllib.request.build_opener(NoRedirect)

    def close(self):self.ledger.close();self.scope.close();self.spend.close()

    def guard(self):
        if time.time_ns()//1000000>=DEADLINE:raise ValueError('SIGCENSUS_DEADLINE')
        self.scope.guard()

    def request(self,epoch,kind,url,size,start=None,pin=None):
        self.guard();self.scope.continuation.assert_epoch(epoch)
        if kind not in ('root-cid','index-header','bucket-table'):raise ValueError('SIGCENSUS_REQUEST_KIND')
        if not 0<size<=MAX_TABLE_BYTES or kind=='root-cid' and size!=MAX_CID_BYTES:
            raise ValueError('SIGCENSUS_REQUEST_BOUND')
        folder=self.out/'metadata'/str(epoch);folder.mkdir(parents=True,exist_ok=True)
        path=folder/(kind+'.bin');receipt=path.with_suffix('.json')
        if path.exists() and receipt.exists():
            saved=document(receipt);raw=path.read_bytes()
            if (saved['url']!=url or saved['start']!=start or saved['requested_bytes']!=size
                or len(raw)!=saved['received_bytes'] or hashlib.sha256(raw).hexdigest()!=saved['sha256']):
                raise ValueError('SIGCENSUS_CACHE_IDENTITY')
            if pin is not None and pin!=saved['source_identity']:raise ValueError('SIGCENSUS_SOURCE_CHANGED')
            return raw,saved['source_identity'],saved
        attempt=uuid.uuid4().hex;stamp=time.time_ns()//1000000;actual=0;admitted=False
        try:
            with ArchiveGate(self.gate,self.guard,wait=False,success_status=200 if start is None else 206):
                key=f'signature-census:{epoch}:{kind}:{attempt}'
                reservation=self.scope.reserve(size+1,key)
                self.spend.reserve('read',key,bytes_=size+1)
                body=dict(url=url,start=start,requested_bytes=size,scope_reservation=reservation)
                with self.ledger.db:
                    self.ledger.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',
                        (attempt,epoch,kind,size+1,0,'RESERVED',stamp,canonical(body)))
                admitted=True
                headers={'Accept-Encoding':'identity','User-Agent':'EconomicMachineResearch/0.1'}
                if start is not None:headers['Range']=f'bytes={start}-{start+size-1}'
                with self.opener.open(urllib.request.Request(url,headers=headers),timeout=30) as response:
                    if response.headers.get('Content-Encoding','identity') not in ('','identity'):
                        raise ValueError('SIGCENSUS_UNEXPECTED_ENCODING')
                    if start is None:
                        if response.status!=200:raise ValueError('SIGCENSUS_ROOT_STATUS')
                        identity=dict(etag=response.headers.get('ETag'),last_modified=response.headers.get('Last-Modified'))
                    else:
                        match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                        if response.status!=206 or match is None or tuple(map(int,match.groups()[:2]))!=(start,start+size-1):
                            raise ValueError('SIGCENSUS_RANGE_RESPONSE')
                        identity=dict(total_bytes=int(match[3]),etag=response.headers.get('ETag'))
                        if pin is not None and pin!=identity:raise ValueError('SIGCENSUS_SOURCE_CHANGED')
                    raw=response.read(size+1);actual=len(raw)
                    if not 0<len(raw)<=size or start is not None and len(raw)!=size:
                        raise ValueError('SIGCENSUS_RESPONSE_SIZE')
            durable_bytes(path,raw)
            saved=dict(epoch=epoch,kind=kind,url=url,start=start,requested_bytes=size,received_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),source_identity=identity,at_ms=stamp,attempt_id=attempt)
            atomic(receipt,saved)
            with self.ledger.db:
                self.ledger.db.execute('UPDATE attempts SET received_bytes=?,state=?,body=? WHERE id=?',
                    (actual,'RETAINED',canonical(saved),attempt))
            return raw,identity,saved
        except BaseException as error:
            if admitted:
                failed=dict(error_type=type(error).__name__,http_status=getattr(error,'code',None),
                    failed_reservation_retained=True,partial_transport_bytes_unknown=True)
                with self.ledger.db:
                    self.ledger.db.execute('UPDATE attempts SET received_bytes=?,state=?,body=? WHERE id=?',
                        (actual,'FAILED_RETAINED',canonical(dict(body,**failed)),attempt))
            raise

    def root(self,epoch):
        folder=self.base/'evidence/address-epochs-v2/epochs'/str(epoch)
        path=folder/'import-receipt.json'
        if path.exists():
            receipt=document(path,1024**2);directory=receipt['directory']
            if not isinstance(directory,str) or Path(directory).name!=directory:
                raise ValueError('SIGCENSUS_MANIFEST_DIRECTORY')
            path=folder/directory/'manifest';raw=path.read_bytes()
            expected=next(r for r in receipt['extraction']['files'] if r['name']=='manifest')
            if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=expected['sha256']:
                raise ValueError('SIGCENSUS_MANIFEST_HASH')
            if raw[:16]!=b'gsfamnfs'+(5).to_bytes(8,'little'):raise ValueError('SIGCENSUS_MANIFEST_MAGIC')
            meta,end=_meta(raw,16);root=meta[b'rootCid'];cid_text(root)
            if end!=len(raw) or meta[b'epoch']!=epoch.to_bytes(8,'little') or meta.get(b'network')!=b'mainnet':
                raise ValueError('SIGCENSUS_MANIFEST_IDENTITY')
            return root,dict(source='retained_gsfa_manifest',sha256=expected['sha256'])
        url=f'https://files.old-faithful.net/{epoch}/epoch-{epoch}.cid'
        raw,identity,receipt=self.request(epoch,'root-cid',url,MAX_CID_BYTES)
        return decode_cid(raw),dict(source='provider_root_cid_text',receipt=receipt)

    def process(self,epoch):
        self.guard();self.ledger.begin(epoch)
        try:
            root,provenance=self.root(epoch);cid=cid_text(root)
            url=f'https://files.old-faithful.net/{epoch}/epoch-{epoch}-{cid}-mainnet-sig-to-cid.index'
            prefix,identity,first=self.request(epoch,'index-header',url,512,0)
            h=header(prefix,epoch=epoch,root_cid=root)
            table,identity,second=self.request(epoch,'bucket-table',url,h['bucket_table_bytes'],h['header_bytes'],identity)
            result=count_entries(h,table,identity['total_bytes'])
            result.update(root_provenance=provenance,index_url=url,index_identity=identity,
                header_sha256=first['sha256'],table_sha256=second['sha256'],at_ms=time.time_ns()//1000000,
                metadata_only=True,whole_index_content_not_downloaded=True)
            atomic(self.out/f'epoch-{epoch}.json',result);self.ledger.finish(epoch,result)
            self.ledger.snapshot(last_completed_epoch=epoch)
        except ArchiveDeferred as error:
            self.ledger.defer(epoch,'SOURCE_BACKOFF',error.retry_at_ms,dict(reason=error.reason))
            self.ledger.snapshot('SOURCE_BACKOFF',retry_at_ms=error.retry_at_ms)
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,EOFError) as error:
            code=getattr(error,'code',None);now=time.time_ns()//1000000
            state='PROVIDER_UNAVAILABLE' if code in (400,403,404) else 'SOURCE_BACKOFF'
            network=document(self.gate/'network-state.json') if (self.gate/'network-state.json').exists() else {}
            retry=max(now+(21600000 if state=='PROVIDER_UNAVAILABLE' else 10000),int(network.get('not_before',0)*1000))
            failure=dict(epoch=epoch,at_ms=now,error_type=type(error).__name__,http_status=code,
                reservation_retained=True,retry_at_ms=retry)
            atomic(self.out/f'error-{epoch}-{now}.json',failure)
            self.ledger.defer(epoch,state,retry,failure);self.ledger.snapshot(state,retry_at_ms=retry)
        except ValueError as error:
            # Resource and deadline failures stop the worker; a format/source
            # disagreement quarantines only the affected epoch without zeroing it.
            if str(error).startswith(('CORE_','SPEND_','RESEARCH_')) or str(error)=='SIGCENSUS_DEADLINE':raise
            failure=dict(epoch=epoch,at_ms=time.time_ns()//1000000,error_type=type(error).__name__,code=str(error)[:160])
            atomic(self.out/f'validation-{epoch}-{failure["at_ms"]}.json',failure)
            self.ledger.defer(epoch,'FAILED_VALIDATION',DEADLINE,failure);self.ledger.snapshot('VALIDATION_GAP')


def run(base,initialize=False,once=False):
    out=Path(base)/FOLDER;out.mkdir(parents=True,exist_ok=True)
    lock=os.open(out/'worker.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    worker=None
    try:
        worker=Worker(base,initialize);worker.ledger.snapshot('STARTING')
        if initialize:return worker.ledger.snapshot('INITIALIZED')
        while time.time_ns()//1000000<DEADLINE:
            worker.guard();epoch=worker.ledger.next_epoch()
            if epoch is None:
                summary=worker.ledger.snapshot('WAITING_FOR_ELIGIBLE_EPOCH')
                if summary['unknown_epochs']==0:return worker.ledger.snapshot('COMPLETE_PROVIDER_CENSUS')
            else:worker.process(epoch)
            if once:return document(out/'summary.json')
            # A single metadata lane yields to the existing CAR/GSFA collectors.
            retry=document(out/'summary.json').get('retry_at_ms',0)
            time.sleep(max(2,min(30,(retry-time.time_ns()//1000000)/1000)))
        return worker.ledger.snapshot('DEADLINE_STOPPED')
    except Exception as error:
        if worker is not None:worker.ledger.snapshot('STOPPED_RETAINED',error_type=type(error).__name__,code=str(error)[:160])
        raise
    finally:
        if worker is not None:worker.close()
        os.close(lock)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    p.add_argument('--initialize',action='store_true');p.add_argument('--once',action='store_true');a=p.parse_args()
    print(json.dumps(run(a.base,a.initialize,a.once)))
