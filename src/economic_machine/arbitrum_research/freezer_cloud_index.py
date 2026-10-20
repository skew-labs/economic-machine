"""Verified sealed SQLite shards in Blob with a bounded local read cache.

The immutable registry retains the original index hash. A cloud manifest is
committed only after full byte restoration and query parity. Originals are not
retired until this module can open the restored sealed file by that hash.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from .storage import Storage
from .freezer_archive_storage import archive_storage
from economic_machine.pump_research.research_cloud import identity, digest_file
from economic_machine.pump_research.store import atomic, canonical

SHARED = Path('/srv/economic-machine/solana')
PART = 32 * 1024**2
CACHE = 2 * 1024**3
MAX_INDEX = 1024**3


def load_manifest(path, expected_sha, index_sha):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 2 * 1024**2 or digest_file(path) != expected_sha:
        raise ValueError('CLOUD_INDEX_MANIFEST_HASH')
    value = json.loads(path.read_bytes())
    if (value.get('schema') != 'economic-machine.freezer-cloud-index.v1' or value.get('index_sha256') != index_sha
            or not 0 < value.get('bytes', 0) <= MAX_INDEX or value.get('chain_id') not in (42161,4663)):
        raise ValueError('CLOUD_INDEX_MANIFEST_SCOPE')
    total = 0
    for part in value.get('parts', []):
        if part['offset'] != total or not 0 < part['proof']['raw_bytes'] <= PART:
            raise ValueError('CLOUD_INDEX_MANIFEST_PARTS')
        total += part['proof']['raw_bytes']
    if total != value['bytes']:
        raise ValueError('CLOUD_INDEX_MANIFEST_SIZE')
    return value


def restore(storage, manifest, path):
    path = Path(path)
    if path.exists():
        if path.is_symlink() or path.stat().st_size != manifest['bytes'] or digest_file(path) != manifest['index_sha256']:
            raise ValueError('CLOUD_INDEX_EXISTING_CHANGED')
        return
    storage.cloud.guard_local(manifest['bytes'] + PART * 2)
    temporary = path.with_name(path.name + '.partial')
    # A failed local duplicate is retained rather than truncating it on resume.
    if temporary.exists():
        temporary = path.with_name(path.name + f'.partial-{time.time_ns()}')
    with temporary.open('xb') as stream:
        whole = hashlib.sha256(); size = 0
        for part in manifest['parts']:
            raw = storage.read(part['proof']); stream.write(raw); whole.update(raw); size += len(raw)
        stream.flush(); os.fsync(stream.fileno())
    if size != manifest['bytes'] or whole.hexdigest() != manifest['index_sha256']:
        raise ValueError('CLOUD_INDEX_RESTORE_HASH')
    os.replace(temporary, path)


@contextmanager
def shard_lease(path,exclusive=False):
    lock=(Path(path).parent/'index-read.lock').open('a+b')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:lock.close()


@contextmanager
def resolve(base, row, *, force_cloud=False, _lease=True):
    """Hold the cache lease until the caller has closed its SQLite connection."""
    if _lease:
        with shard_lease(row['path']):
            # A reader may have selected its row before archival publication.
            # Re-read under the same lease that protects local retirement.
            registry=Path(base)/'evidence/freezer-search-v1/catalogue.sqlite3'
            if registry.exists():
                db=sqlite3.connect('file:'+str(registry)+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
                try:
                    latest=db.execute('select * from shards where id=?',(row['id'],)).fetchone()
                    if not latest:raise ValueError('CLOUD_INDEX_REGISTRY_MISSING')
                    row=latest
                finally:db.close()
            with resolve(base,row,force_cloud=force_cloud,_lease=False) as path:yield path
        return
    original = Path(row['path']); record = json.loads(row['body'])
    cloud_ref = record.get('cloud_index')
    if not cloud_ref or not force_cloud and not record.get('prefer_cloud_index'):
        yield original
        return
    manifest = load_manifest(cloud_ref['path'], cloud_ref['sha256'], row['id'])
    if manifest['chain_id'] != row['chain']:
        raise ValueError('CLOUD_INDEX_CHAIN')
    root = Path(base) / 'evidence/freezer-search-v1/cloud-cache'
    root.mkdir(exist_ok=True)
    lock = (root / 'cache.lock').open('a+b')
    fcntl.flock(lock, fcntl.LOCK_EX)
    storage = None
    try:
        target = root / (row['id'] + '.sqlite3')
        files = [(p.stat().st_mtime_ns,p,p.stat().st_size) for p in root.glob('*.sqlite3') if p != target]
        size = sum(x[2] for x in files) + (target.stat().st_size if target.exists() else manifest['bytes'])
        for _, path, n in sorted(files):
            if size <= CACHE: break
            path.unlink(); size -= n  # Content addressed, verified Blob-backed duplicate only.
        if size > CACHE:
            raise ValueError('CLOUD_INDEX_CACHE_CAPACITY')
        storage = Storage(root, SHARED, chain_id=row['chain'])
        restore(storage, manifest, target)
        yield target
    finally:
        if storage: storage.close()
        lock.close()


def parity(original, restored):
    a = sqlite3.connect('file:' + str(original) + '?mode=ro',uri=True)
    b = sqlite3.connect('file:' + str(restored) + '?mode=ro',uri=True)
    try:
        addresses = [r[0] for r in a.execute('select address from addresses order by address limit 2')]
        addresses += [r[0] for r in a.execute('select address from addresses order by address desc limit 1')]
        checked = []
        for wallet in addresses:
            for table in ('edges','interactions'):
                sql = f'select * from {table} where sender=? or recipient=? order by block desc,id limit 101'
                x=a.execute(sql,(wallet,wallet)).fetchall(); y=b.execute(sql,(wallet,wallet)).fetchall()
                if x != y: raise ValueError('CLOUD_INDEX_QUERY_PARITY')
                checked.append(dict(wallet=wallet,table=table,rows=len(x)))
        return checked
    finally: a.close(); b.close()


def prepare(base, folder):
    base, folder = Path(base).resolve(), Path(folder).resolve()
    if not folder.is_relative_to(base/'evidence/freezer-continuous-v1'):
        raise ValueError('CLOUD_INDEX_RETIREMENT_SCOPE')
    if json.loads((folder.parent/'status.json').read_bytes())['state'] != 'COMPLETE_BOUNDED_WINDOW':
        raise ValueError('CLOUD_INDEX_BATCH_NOT_COMPLETE')
    path=folder/'index.sqlite3'
    registry=sqlite3.connect(base/'evidence/freezer-search-v1/catalogue.sqlite3',timeout=30)
    registry.row_factory=sqlite3.Row
    row=registry.execute('select * from shards where path=?',(str(path),)).fetchone()
    if not row: registry.close(); raise ValueError('CLOUD_INDEX_UNREGISTERED')
    record=json.loads(row['body'])
    if record.get('cloud_index'):
        registry.close()
        return record['cloud_index']
    before=identity(path)
    if before['bytes']>MAX_INDEX or digest_file(path)!=row['id']:
        registry.close();raise ValueError('CLOUD_INDEX_SOURCE_CHANGED')
    storage=archive_storage(folder/'cloud-index-work',SHARED,chain_id=row['chain'],ordinary_storage=Storage)
    try:
        parts=[];offset=0
        with path.open('rb') as source:
            while raw:=source.read(PART):
                proof=storage.put(raw,'freezer_sealed_index_part',dict(index_sha256=row['id'],offset=offset))
                parts.append(dict(offset=offset,proof=proof));offset+=len(raw)
        manifest=dict(schema='economic-machine.freezer-cloud-index.v1',chain_id=row['chain'],bytes=offset,
                      index_sha256=row['id'],parts=parts,original_identity=before,first=row['first'],last=row['last'])
        manifest_path=folder/'cloud-index-manifest.json';atomic(manifest_path,manifest)
        manifest_proof=storage.put((canonical(manifest)+'\n').encode(),'freezer_sealed_index_manifest')
        restored=folder/'cloud-index-work'/'restored.sqlite3';restore(storage,manifest,restored)
        checks=parity(path,restored)
        if identity(path)!=before:raise ValueError('CLOUD_INDEX_SOURCE_MUTATED')
        evidence=dict(at_ms=time.time_ns()//1000000,source_identity=before,index_sha256=row['id'],
                      full_blob_readback=True,full_index_bytes_restored=offset,query_parity=checks,
                      manifest_proof=manifest_proof,original_retired=False)
        atomic(folder/'cloud-index-verification.json',evidence)
        # Adopt the already fully restored and queried duplicate into the bounded
        # reader cache. Do not immediately discard it and restore it a second time.
        cache=base/'evidence/freezer-search-v1/cloud-cache';cache.mkdir(exist_ok=True)
        with (cache/'cache.lock').open('a+b') as cache_lock:
            fcntl.flock(cache_lock,fcntl.LOCK_EX)
            target=cache/(row['id']+'.sqlite3')
            existing=[(p.stat().st_mtime_ns,p,p.stat().st_size) for p in cache.glob('*.sqlite3') if p!=target]
            total=sum(x[2] for x in existing)+before['bytes']
            for _,p,n in sorted(existing):
                if total<=CACHE:break
                p.unlink();total-=n
            if total>CACHE:raise ValueError('CLOUD_INDEX_CACHE_CAPACITY')
            if target.exists():
                if digest_file(target)!=row['id']:raise ValueError('CLOUD_INDEX_CACHE_CHANGED')
                restored.unlink()
            else:os.replace(restored,target)
        cloud_ref=dict(path=str(manifest_path),sha256=digest_file(manifest_path),proof=manifest_proof)
        record.update(cloud_index=cloud_ref,prefer_cloud_index=True)
        with registry:
            current=registry.execute('select body from shards where id=?',(row['id'],)).fetchone()[0]
            if current!=row['body']:raise ValueError('CLOUD_INDEX_REGISTRY_CHANGED')
            registry.execute('update shards set body=? where id=?',(canonical(record),row['id']))
        return cloud_ref
    finally:storage.close();registry.close()


def _retire_locked(base,folder):
    """The registered cloud route is exercised while the original still exists."""
    base,folder=Path(base).resolve(),Path(folder).resolve()
    if not folder.is_relative_to(base/'evidence/freezer-continuous-v1'):raise ValueError('CLOUD_INDEX_RETIREMENT_SCOPE')
    path=folder/'index.sqlite3';registry=sqlite3.connect(base/'evidence/freezer-search-v1/catalogue.sqlite3')
    registry.row_factory=sqlite3.Row
    try: row=registry.execute('select * from shards where path=?',(str(path),)).fetchone()
    finally:registry.close()
    if not row or not json.loads(row['body']).get('prefer_cloud_index'):raise ValueError('CLOUD_INDEX_NOT_ROUTED')
    proof=json.loads((folder/'cloud-index-verification.json').read_bytes())
    if not path.exists():
        if proof.get('original_retired') is not True:
            if not proof.get('retirement_started_at_ms'):raise ValueError('CLOUD_INDEX_MISSING_ORIGINAL')
            with resolve(base,row,force_cloud=True,_lease=False) as restored:
                if digest_file(restored)!=row['id']:raise ValueError('CLOUD_INDEX_RECOVERY_HASH')
            proof.update(original_retired=True,retired_at_ms=time.time_ns()//1000000,reconciled_retirement=True)
            atomic(folder/'cloud-index-verification.json',proof)
        return proof
    with resolve(base,row,force_cloud=True,_lease=False) as restored:
        checks=parity(path,restored)
        if identity(path)!=proof['source_identity'] or digest_file(path)!=row['id']:
            raise ValueError('CLOUD_INDEX_RETIRE_CHANGED')
        proof.update(cloud_route_query_parity=checks,retirement_started_at_ms=time.time_ns()//1000000)
        atomic(folder/'cloud-index-verification.json',proof)
        path.unlink()
        proof.update(original_retired=True,retired_at_ms=time.time_ns()//1000000)
        atomic(folder/'cloud-index-verification.json',proof)
    return proof


def retire(base,folder):
    with shard_lease(Path(folder)/'index.sqlite3',exclusive=True):
        return _retire_locked(base,folder)


def derived_expectations(report, binary_sha256, raw):
    """Validate immutable original native evidence; no second native run implied."""
    proof=report['source_proof'];derived=report.get('derived_reconstruction')
    if derived is None:return None
    if (derived.get('schema')!='economic-machine.freezer-derived-provenance.v1'
            or derived.get('binary_sha256')!=binary_sha256
            or proof.get('source',{}).get('binary_sha256')!=binary_sha256
            or derived.get('native_input_sha256')!=hashlib.sha256(raw).hexdigest()
            or proof.get('raw_sha256')!=derived.get('native_input_sha256')
            or proof.get('raw_bytes')!=len(raw) or proof.get('complete_cloud_readback') is not True
            or derived.get('native_root_checks') is not True
            or derived.get('decoded_materialization') not in (None,'memory_v1')
            or derived.get('blocks')!=report['blocks'] or derived.get('chain_id')!=report['graph']['chain_id']
            or type(derived.get('decoded_bytes')) is not int or not 0<derived['decoded_bytes']<=1024**3
            or not isinstance(derived.get('decoded_sha256'),str) or len(derived['decoded_sha256'])!=64
            or any(c not in '0123456789abcdef' for c in derived['decoded_sha256'])):
        raise ValueError('CLOUD_DERIVED_PROVENANCE')
    return {'candidate-records.json':(len(raw),hashlib.sha256(raw).hexdigest()),
            'candidate-decoded.json':(derived['decoded_bytes'],derived['decoded_sha256'])}


def _retire_derived_locked(base,folder):
    """Rebuildable duplicates use registered native evidence or legacy replay."""
    base,folder=Path(base).resolve(),Path(folder).resolve()
    if not folder.is_relative_to(base/'evidence/freezer-continuous-v1'):raise ValueError('CLOUD_DERIVED_SCOPE')
    retirement=folder/'cloud-derived-retirement.json'
    if retirement.exists() and json.loads(retirement.read_bytes()).get('retired'):
        return json.loads(retirement.read_bytes())
    index_proof=json.loads((folder/'cloud-index-verification.json').read_bytes())
    if index_proof.get('original_retired') is not True:raise ValueError('CLOUD_DERIVED_INDEX_PENDING')
    report_path=folder/'verification.json';report_identity=identity(report_path)
    registry=sqlite3.connect('file:'+str(base/'evidence/freezer-search-v1/catalogue.sqlite3')+'?mode=ro',uri=True)
    try:row=registry.execute('select body from shards where path=?',(str(folder/'index.sqlite3'),)).fetchone()
    finally:registry.close()
    if not row or digest_file(report_path)!=json.loads(row[0])['report_sha256']:raise ValueError('CLOUD_DERIVED_REPORT_CHANGED')
    report=json.loads(report_path.read_bytes());proof=report['source_proof']
    from .freezer_decoder import replay_binary,native_environment
    binary,decoder_profile=replay_binary(base,report);binary_sha=digest_file(binary)
    storage=archive_storage(folder/'cloud-index-work',SHARED,chain_id=report['graph']['chain_id'],ordinary_storage=Storage)
    try:
        # read validates every retained Blob byte against the source proof.
        raw=storage.read(proof)
        expected=derived_expectations(report,binary_sha,raw);native_replayed=expected is None
        if expected is None:
            decoded=subprocess.run([str(binary)],input=raw,capture_output=True,timeout=120,env=native_environment(decoder_profile))
            if decoded.returncode:raise ValueError('CLOUD_DERIVED_NATIVE_REPLAY')
            value=json.loads(decoded.stdout)
            if not value.get('root_checks') or len(value['blocks'])!=report['blocks']:raise ValueError('CLOUD_DERIVED_ROOT_CHECKS')
            recreated=(canonical(value)+'\n').encode()
            expected={'candidate-records.json':(len(raw),hashlib.sha256(raw).hexdigest()),
                      'candidate-decoded.json':(len(recreated),hashlib.sha256(recreated).hexdigest())}
        retained=[]
        for name,(size,digest) in expected.items():
            p=folder/name
            if p.exists():
                if identity(p)['bytes']!=size or digest_file(p)!=digest:raise ValueError('CLOUD_DERIVED_PARITY')
                retained.append(dict(name=name,identity=identity(p),sha256=digest))
            elif not retirement.exists():
                from .freezer_decoded_memory import missing_is_memory_derived
                # Only an immutable successful new-page report may declare the
                # decoded copy never materialized. Raw source remains required.
                if not missing_is_memory_derived(report,name):raise ValueError('CLOUD_DERIVED_ORIGINAL_MISSING')
        if identity(report_path)!=report_identity or digest_file(binary)!=binary_sha:raise ValueError('CLOUD_DERIVED_EVIDENCE_MUTATED')
        result=dict(at_ms=time.time_ns()//1000000,source_proof=proof,binary_sha256=binary_sha,
                    native_root_replay=native_replayed,native_root_verified_in_original_extraction=True,
                    bound_report_sha256=json.loads(row[0])['report_sha256'],derived_byte_parity=True,files=retained,retired=False,
                    decoded_materialization=report.get('derived_reconstruction',{}).get('decoded_materialization','disk'))
        atomic(retirement,result)
        for item in retained:
            p=folder/item['name']
            if identity(p)!=item['identity']:raise ValueError('CLOUD_DERIVED_MUTATED')
            p.unlink()
        result.update(retired=True,retired_at_ms=time.time_ns()//1000000);atomic(retirement,result)
        return result
    finally:storage.close()


def retire_derived(base,folder):
    # A query/retirement reader lease also serializes concurrent archivists.
    with shard_lease(Path(folder)/'index.sqlite3',exclusive=True):
        return _retire_derived_locked(base,folder)
