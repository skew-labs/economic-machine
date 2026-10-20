"""Migrate verified research duplicates, then continue the existing epoch queue."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from economic_machine.pump_research.gsfa_index import Budget,GsfaIndex
from economic_machine.pump_research.research_cloud import Cloud,CloudFile,archive_file,retire_file,document
from economic_machine.pump_research.research_expansion import ExpansionScope
from economic_machine.pump_research.research_spend import FOLDER,authority,build_grant,Spend
from economic_machine.pump_research.store import atomic,canonical
from continue_history import exclusive

def activate(base):
    base=Path(base);folder=base/FOLDER;folder.mkdir(exist_ok=True)
    path=folder/'grant.json'
    if path.exists():return dict(existing=True,sha256=authority(base)[1])
    value=build_grant(base);raw=(canonical(value)+'\n').encode();digest=hashlib.sha256(raw).hexdigest()
    exclusive(path,raw);exclusive(path.with_suffix('.sha256'),(digest+'\n').encode())
    with_close=Spend(base)
    try:with_close.baseline_models();with_close.publish()
    finally:with_close.close()
    return dict(grant_sha256=digest,monthly_limit_krw=value['monthly_limit'],services_started=False)

def migrate_epoch(base,epoch,scope,cloud,report):
    folder=base/'evidence/address-epochs-v2/epochs'/str(epoch)
    receipt_path=folder/'import-receipt.json';receipt=document(receipt_path)
    if receipt.get('epoch')!=epoch or receipt.get('index_query_verified') is not True:raise ValueError('CLOUD_IMPORT_REQUIRED')
    members=receipt['extraction']['files'];directory=folder/receipt['directory']
    if (directory.name!=f'gsfa-index-{epoch}' or directory.is_symlink()
        or directory.resolve().parent!=folder.resolve()):raise ValueError('CLOUD_IMPORT_DIRECTORY')
    source=document(folder/'download-manifest.json')
    if source['sha256']!=receipt['source_sha256']:raise ValueError('CLOUD_IMPORT_SOURCE')
    portable_source=cloud.upload(folder/'download-manifest.json')
    portable_import=cloud.upload(receipt_path)
    from economic_machine.pump_research.source_checkpoints import migrate_sources
    retired=migrate_sources(folder,epoch,source,portable_source,portable_import,
        scope,cloud,report,checkpoint_chunks=int(os.environ.get('EM_CLOUD_SOURCE_CHECKPOINT_CHUNKS','1')))
    expected=next(r for r in members if r['name']=='linked-log')
    path=directory/'linked-log';reference=directory/'linked-log-cloud.json'
    if path.exists():
        archive_file(cloud,path,reference,expected,lambda done,total:report('ARCHIVING_SEARCH_INDEX',epoch=epoch,
            bytes_verified=done,member_bytes=total,duplicate_bytes_retired_this_run=retired))
        # Compare exact saved candidate pages, including cursors, through the
        # actual cloud read path before removing the local linked log.
        reader=CloudFile(base,reference,expected)
        with GsfaIndex(directory,epoch,Budget(scope.effective_deadline_ms,64*1024**2,scope.guard),linked_reader=reader) as index:
            if index.identity!=receipt['index_identity']:raise ValueError('CLOUD_INDEX_IDENTITY')
            for saved in receipt['queries']:
                if index.page(saved['wallet'],limit=8)!=saved:raise ValueError('CLOUD_INDEX_QUERY_PARITY')
        proof=dict(schema='economic-machine.cloud-gsfa-parity.v1',epoch=epoch,queries=len(receipt['queries']),
            import_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            cloud_reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
            index_identity=receipt['index_identity'],passed=True,at_ms=time.time_ns()//1000000)
        atomic(folder/'cloud-query-parity.json',proof)
        cloud.upload(folder/'cloud-query-parity.json')
        retired+=retire_file(cloud,path,reference,expected)
    else:
        parity=document(folder/'cloud-query-parity.json')
        if parity.get('passed') is not True or parity.get('cloud_reference_sha256')!=hashlib.sha256(reference.read_bytes()).hexdigest():
            raise ValueError('CLOUD_QUERY_PARITY_MISSING')
    # Import receipt, source manifests and small public-key index remain local;
    # also archive their exact bytes so the cloud objects are reconstructible.
    metadata=[]
    for p in (receipt_path,folder/'download-manifest.json',directory/'manifest',folder/'cloud-query-parity.json'):
        metadata.append(dict(path=str(p.relative_to(folder)),**cloud.upload(p)))
    references=[]
    for p in [*sorted((folder/'cloud-chunks').glob('*.json')),reference]:
        if p.name.endswith('.receipt.json'):continue
        references.append(dict(path=str(p.relative_to(folder)),**cloud.upload(p)))
    catalog=dict(schema='economic-machine.research-cloud-epoch.v1',epoch=epoch,metadata=metadata,references=references,
        source_sha256=receipt['source_sha256'],index_identity=receipt['index_identity'],original_import_preserved=True)
    atomic(folder/'cloud-catalog.json',catalog)
    atomic(folder/'cloud-catalog.receipt.json',cloud.upload(folder/'cloud-catalog.json'))
    report('EPOCH_CLOUD_SEARCH_READY',epoch=epoch,duplicate_bytes_retired_this_run=retired,
        whole_wallet_history=False)

def run(base,once=False):
    from epochs_download import EpochDownloader
    from epochs_import import inspect_and_import
    import urllib.error
    base=Path(base);out=base/FOLDER;scope=ExpansionScope(base);cloud=Cloud(base)
    worker=EpochDownloader(base,scope)
    lock=(worker.out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    def report(state,**value):
        atomic(out/'cloud-status.json',dict(state=state,at_ms=time.time_ns()//1000000,**value,
            budget=cloud.spend.totals(),production_changed=False,cloud_objects_deleted=False))
    try:
        # Only imported v2 epochs are eligible; legacy685 remains untouched.
        for row in worker.db.execute("SELECT epoch FROM epochs WHERE state='IMPORTED' ORDER BY epoch DESC").fetchall():
            migrate_epoch(base,row[0],scope,cloud,report)
        if once:return
        worker.demand();worker.catalogue()
        for epoch in worker.pending():
            report('CONTINUING_EXISTING_QUEUE',epoch=epoch)
            try:
                manifest=worker.download(epoch)
                receipt=inspect_and_import(base,epoch,scope,manifest,
                    notify=lambda state,**body:worker.state(epoch,state,**body))
                worker.state(epoch,'IMPORTED',receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest())
                migrate_epoch(base,epoch,scope,cloud,report)
            except urllib.error.HTTPError as error:
                if error.code==404:worker.state(epoch,'PROVIDER_UNAVAILABLE',http_status=404);continue
                raise
        report('CURRENT_INVENTORY_DRAINED',whole_wallet_history=False)
    except BaseException as error:
        report('STOPPED',reason=str(error)[:140],error_type=type(error).__name__)
        atomic(worker.out/'status.json',worker.snapshot(str(error)[:140]));raise
    finally:lock.close();worker.close();cloud.close();scope.close()

def canary(base):
    """One real retained chunk: upload, full independent read, then retire copy."""
    base=Path(base);cloud=Cloud(base);scope=ExpansionScope(base)
    folder=base/'evidence/address-epochs-v2/epochs/1009'
    manifest=document(folder/'download-manifest.json');row=manifest['chunks'][0]
    path=folder/'chunks'/row['path'];ref=folder/'cloud-chunks'/(row['path']+'.json')
    ref.parent.mkdir(exist_ok=True);start=time.monotonic()
    with (base/'evidence/address-epochs-v2/worker.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            value=archive_file(cloud,path,ref,row)
            reader=CloudFile(base,ref,row);h=hashlib.sha256();offset=0
            try:
                while offset<row['bytes']:
                    raw=reader.read(offset,min(8*1024**2,row['bytes']-offset));h.update(raw);offset+=len(raw)
            finally:reader.close()
            if h.hexdigest()!=row['sha256']:raise ValueError('CLOUD_CANARY_FULL_HASH')
            retired=retire_file(cloud,path,ref,row)
            from economic_machine.pump_research.gsfa_index import ChunkStream
            stream=ChunkStream(folder/'download-manifest.json',Budget(scope.effective_deadline_ms,64*1024**2,scope.guard))
            try:
                first=stream.read(65536)
                reader=CloudFile(base,ref,row)
                try:expected=reader.read(0,len(first))
                finally:reader.close()
                if not first or first!=expected:raise ValueError('CLOUD_CANARY_STREAM')
            finally:stream.close()
            result=dict(schema='economic-machine.cloud-live-canary.v1',epoch=1009,chunk_bytes=row['bytes'],sha256=row['sha256'],
                full_cloud_readback=True,independent_full_read_hash=True,reader_after_retirement=True,
                duplicate_local_bytes_retired=retired,cloud_objects_deleted=False,elapsed_seconds=time.monotonic()-start,
                budget=cloud.spend.totals(),at_ms=time.time_ns()//1000000)
            atomic(base/FOLDER/'live-canary.json',result);return result
        finally:cloud.close();scope.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--activate',action='store_true')
    p.add_argument('--canary',action='store_true')
    p.add_argument('--once',action='store_true');a=p.parse_args()
    if a.activate:print(json.dumps(activate(a.base)))
    elif a.canary:print(json.dumps(canary(a.base)))
    else:run(a.base,a.once)
