"""Complete GSFA inspection/import under the additive shared research scope."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from economic_machine.pump_research.gsfa_index import Budget,GsfaIndex,inspect_download
from economic_machine.pump_research.store import atomic,address
from epochs_download import execution_authority

GIB=1024**3


def import_authority(folder,scope,epoch):
    authority=execution_authority(scope)
    continuation=getattr(scope,'continuation',None)
    if continuation is not None:
        continuation.assert_epoch(epoch);scope.guard()
        path=folder/'execution-authorities'/f'{continuation.hash}.json'
        if path.exists():
            if path.is_symlink() or json.loads(path.read_bytes())!=authority:
                raise ValueError('EPOCH_IMPORT_EXECUTION_AUTHORITY_CHANGED')
        else:
            path.parent.mkdir(exist_ok=True)
            atomic(path,authority)
    return authority['effective_deadline_ms']


def publish_registry(base,epoch,scope,receipt):
    base=Path(base);folder=base/'evidence/address-epochs-v2';path=folder/'registry.json'
    if epoch==685:raise ValueError('EPOCH_LEGACY_REGISTRY_OVERRIDE')
    proof=json.loads(receipt.read_bytes());extraction=proof.get('extraction',{})
    if (proof.get('schema')!='economic-machine.gsfa-import.v1' or proof.get('epoch')!=epoch or
        proof.get('index_query_verified') is not True or proof.get('directory')!=f'gsfa-index-{epoch}' or
        any(extraction.get(k) is not True for k in ('format_inspected','extracted','downloaded_chunk_hashes_verified'))):
        raise ValueError('EPOCH_REGISTRY_IMPORT_NOT_VERIFIED')
    entry=dict(epoch=epoch,receipt_path=str(receipt.relative_to(base)),
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        grant_path='evidence/solana-expansion-v2/grant.json',grant_sha256=scope.hash)
    value=json.loads(path.read_bytes()) if path.exists() else dict(schema='economic-machine.gsfa-import-registry.v1',entries=[])
    if value.get('schema')!='economic-machine.gsfa-import-registry.v1' or not isinstance(value.get('entries'),list):
        raise ValueError('EPOCH_REGISTRY_SCHEMA')
    existing=[r for r in value['entries'] if r['epoch']==epoch]
    if existing and existing!=[entry]:raise ValueError('EPOCH_REGISTRY_ENTRY_CHANGED')
    if not existing:
        value['entries'].append(entry);atomic(path,value)
    return entry


def targets(base,epoch):
    path=Path(base)/'evidence/wallet-map-v1/graph.sqlite3'
    if not path.exists():return []
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        rows=db.execute('''SELECT wallet FROM frontier WHERE slot BETWEEN 0 AND 1000000000
            AND source IN ('local_forward','indexed_forward','gsfa_history')
            GROUP BY wallet ORDER BY max(slot BETWEEN ? AND ?) DESC,
            max(slot<=?) DESC,min(depth),min(rowid) LIMIT 32''',
            (epoch*432000,(epoch+1)*432000-1,(epoch+1)*432000-1)).fetchall()
        return [r[0] for r in rows if address(r[0])]
    finally:db.close()


def verify_members(destination,extracted,scope):
    expected={'manifest','linked-log','pubkey-to-offset-and-size.index'}
    files=extracted.get('files',[])
    if len(files)!=3 or {r.get('name') for r in files}!=expected:raise ValueError('EPOCH_IMPORT_MEMBER_LIST')
    for row in files:
        p=destination/row['name']
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=row['bytes']:raise ValueError('EPOCH_IMPORT_MEMBER_SIZE')
        h=hashlib.sha256()
        with p.open('rb') as f:
            while raw:=f.read(1024**2):scope.guard();h.update(raw)
        if h.hexdigest()!=row['sha256']:raise ValueError('EPOCH_IMPORT_MEMBER_HASH')


def inspect_and_import(base,epoch,scope,manifest,notify=None):
    base=Path(base);folder=base/'evidence/address-epochs-v2/epochs'/str(epoch)
    manifest=Path(manifest)
    if manifest!=folder/'download-manifest.json' or manifest.is_symlink() or epoch==685:
        raise ValueError('EPOCH_IMPORT_PATH')
    if scope.grant['epoch_scope']!='all_public_solana_history':raise ValueError('EPOCH_IMPORT_SCOPE')
    effective_deadline=import_authority(folder,scope,epoch)
    scope.guard(force=True);download=json.loads(manifest.read_bytes());inspected_path=folder/'inspection.json'
    status=folder/'import-status.json';receipt_path=folder/'import-receipt.json'
    destination=folder/f'gsfa-index-{epoch}'
    def report(state,**body):
        atomic(status,dict(state=state,epoch=epoch,**body))
        if notify:notify(state,**body)
    if receipt_path.exists():
        # A completed import can only be republished with the same immutable receipt.
        value=json.loads(receipt_path.read_bytes())
        if value.get('epoch')!=epoch or value.get('source_sha256')!=download['sha256'] or value.get('index_query_verified') is not True or value.get('directory')!=destination.name:
            raise ValueError('EPOCH_IMPORT_EXISTING_RECEIPT')
        verify_members(destination,value['extraction'],scope)
        with GsfaIndex(destination,epoch,Budget(effective_deadline,64*1024**2,scope.guard)) as index:
            if index.identity!=value['index_identity'] or index.root_cid!=value['root_cid']:
                raise ValueError('EPOCH_IMPORT_INDEX_IDENTITY')
        publish_registry(base,epoch,scope,receipt_path);return receipt_path
    if inspected_path.exists():
        inspected=json.loads(inspected_path.read_bytes())
        if inspected.get('compressed_sha256')!=download['sha256'] or inspected.get('format_inspected') is not True:
            raise ValueError('EPOCH_INSPECTION_IDENTITY')
    else:
        report('INSPECTING_COMPLETE_ARCHIVE',address_index_usable=False)
        # Independent expansion ceiling, separate from archive compressed size.
        inspected=inspect_download(manifest,Budget(effective_deadline,96*GIB,scope.guard))
        atomic(inspected_path,inspected)
    report('INSPECTED',member_bytes=inspected['member_bytes'],address_index_usable=False)
    needed=inspected['member_bytes']
    extraction_path=folder/'extraction-receipt.json'
    if destination.exists():
        if destination.is_symlink() or not extraction_path.exists():raise ValueError('EPOCH_PARTIAL_IMPORT_PRESERVED')
        extracted=json.loads(extraction_path.read_bytes())
        if extracted.get('files')!=inspected['files'] or extracted.get('compressed_sha256')!=download['sha256']:
            raise ValueError('EPOCH_IMPORT_MEMBER_IDENTITY')
        verify_members(destination,extracted,scope)
    else:
        scope.guard(needed+128*1024**2,force=True)
        report('EXTRACTING_VERIFIED_MEMBERS',member_bytes=needed,address_index_usable=False)
        extracted=inspect_download(manifest,Budget(effective_deadline,inspected['decompressed_tar_bytes']+1024**2,scope.guard),destination)
        if extracted['files']!=inspected['files']:raise ValueError('EPOCH_IMPORT_MEMBER_IDENTITY')
        atomic(extraction_path,extracted)
    wallet_targets=targets(base,epoch);queries=[]
    with GsfaIndex(destination,epoch,Budget(effective_deadline,64*1024**2,scope.guard)) as index:
        identity,root_cid=index.identity,index.root_cid
        for wallet in wallet_targets:queries.append(index.page(wallet,limit=8))
    if not queries:
        report('EXTRACTED_WAITING_TARGET_QUERY',address_index_usable=False)
        raise ValueError('EPOCH_IMPORT_NO_OBSERVED_TARGET')
    value=dict(schema='economic-machine.gsfa-import.v1',epoch=epoch,index_identity=identity,root_cid=root_cid,
        directory=destination.name,source_sha256=download['sha256'],extraction=extracted,queries=queries,
        at_ms=time.time_ns()//1000000,index_query_verified=True,address_membership_verified=False,
        independent_consensus_verified=False,complete_address_history=False,network_requests=0)
    atomic(receipt_path,value);publish_registry(base,epoch,scope,receipt_path)
    report('ADDRESS_CANDIDATE_INDEX_READY',index_query_verified=True,
        address_membership_verified=False,independent_consensus_verified=False,complete_address_history=False)
    return receipt_path
