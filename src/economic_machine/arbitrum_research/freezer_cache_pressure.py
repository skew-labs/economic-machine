"""Evict only verified search-cache duplicates under the existing reader lease.

This does not change the cache capacity, the source ledger, or admission floors.
No network request is made. Every candidate is proved before any unlink occurs.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time

from economic_machine.pump_research.research_cloud import identity, digest_file
from economic_machine.pump_research.store import canonical

MAX_EVICTION = 2 * 1024**3
DEADLINE = 1792351694298


def _receipt(path, value):
    with Path(path).open('x') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _prove(path, registry, objects):
    before = identity(path)
    row = registry.execute('select id,chain,path,body from shards where id=?', (path.stem,)).fetchone()
    if not row or row[1] != 42161:
        raise ValueError('PRESSURE_CACHE_UNKNOWN_SHARD')
    body = json.loads(row[3]); ref = body.get('cloud_index')
    if body.get('prefer_cloud_index') is not True or not ref:
        raise ValueError('PRESSURE_CACHE_NOT_PORTABLE')
    mp = Path(ref['path'])
    if mp.is_symlink() or mp.stat().st_size > 2 * 1024**2:
        raise ValueError('PRESSURE_CACHE_MANIFEST_TYPE')
    raw = mp.read_bytes(); manifest = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != ref['sha256']
            or manifest.get('schema') != 'economic-machine.freezer-cloud-index.v1'
            or manifest.get('chain_id') != 42161 or manifest.get('index_sha256') != path.stem
            or manifest.get('bytes') != before['bytes']):
        raise ValueError('PRESSURE_CACHE_MANIFEST_SCOPE')
    proofs = [ref['proof']]; offset = 0
    for part in manifest.get('parts', []):
        proof = part['proof']
        if part['offset'] != offset or not 0 < proof['raw_bytes'] <= 32 * 1024**2:
            raise ValueError('PRESSURE_CACHE_PARTS')
        offset += proof['raw_bytes']; proofs.append(proof)
    if offset != before['bytes'] or ref['proof']['raw_sha256'] != ref['sha256'] or ref['proof']['raw_bytes'] != len(raw):
        raise ValueError('PRESSURE_CACHE_MANIFEST_PROOF')
    for proof in proofs:
        if (proof.get('schema') != 'economic-machine.arbitrum-object.v1'
                or proof.get('chain_id') != 42161 or proof.get('encoding') != 'gzip'
                or proof.get('complete_cloud_readback') is not True):
            raise ValueError('PRESSURE_CACHE_UNVERIFIED_PROOF')
        ref_ = proof['cloud']
        if objects.execute('select bytes,verified from objects where digest=?', (ref_['sha256'],)).fetchone() != (ref_['bytes'], 1):
            raise ValueError('PRESSURE_CACHE_UNVERIFIED_OBJECT')
    if digest_file(path) != path.stem or identity(path) != before:
        raise ValueError('PRESSURE_CACHE_FILE_CHANGED')
    wal = path.with_name(path.name + '-wal')
    if wal.exists() and (wal.is_symlink() or wal.stat().st_size != 0):
        raise ValueError('PRESSURE_CACHE_NONEMPTY_WAL')
    return dict(path=str(path), identity=before, sha256=path.stem, manifest=str(mp),
                manifest_sha256=ref['sha256'], verified_objects=len(proofs),
                registry_body_sha256=hashlib.sha256(row[3].encode()).hexdigest())


def evict_for_headroom(base, cloud, extra):
    if type(extra) is not int or extra != 8 * 1024**3:
        raise ValueError('PRESSURE_CACHE_HEADROOM')
    if time.time_ns() // 1000000 >= DEADLINE:
        raise ValueError('PRESSURE_CACHE_DEADLINE')
    base = Path(base); root = base / 'evidence/freezer-search-v1/cloud-cache'
    required = cloud.spend.grant['minimum_host_free_bytes'] + extra
    free = shutil.disk_usage(base).free
    if free >= required:
        return dict(state='NOT_NEEDED', removed_bytes=0, free=free)
    if not root.exists():
        return dict(state='NO_CACHE', removed_bytes=0, free=free)
    if root.is_symlink():
        raise ValueError('PRESSURE_CACHE_ROOT_TYPE')
    with (root / 'cache.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(state='READER_BUSY', removed_bytes=0, free=free)
        needed = max(0, required - shutil.disk_usage(base).free)
        candidates = sorted((p for p in root.glob('*.sqlite3') if re.fullmatch('[0-9a-f]{64}\\.sqlite3', p.name)),
                            key=lambda p: p.lstat().st_mtime_ns)
        registry = sqlite3.connect('file:' + str(base/'evidence/freezer-search-v1/catalogue.sqlite3') + '?mode=ro', uri=True)
        checked = []; total = 0
        try:
            for path in candidates:
                if total >= needed:
                    break
                if total + path.lstat().st_size > MAX_EVICTION:
                    break
                item = _prove(path, registry, cloud.spend.db)
                checked.append(item); total += item['identity']['bytes']
        finally:
            registry.close()
        output = base / 'evidence/freezer-continuous-v1/cache-pressure'; output.mkdir(exist_ok=True)
        ident = str(time.time_ns())
        _receipt(output/(ident+'-before.json'), dict(at_ms=time.time_ns()//1000000,
                 required_free=required, free_before=free, candidates=checked,
                 exclusive_reader_lease=True, network_calls=0))
        if time.time_ns() // 1000000 >= DEADLINE:
            raise ValueError('PRESSURE_CACHE_DEADLINE')
        for item in checked:
            if identity(item['path']) != item['identity']:
                raise ValueError('PRESSURE_CACHE_MUTATED_BEFORE_UNLINK')
        for item in checked:
            Path(item['path']).unlink()
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        result = dict(state='EVICTED' if checked else 'NO_ELIGIBLE_CACHE', removed_bytes=total,
                      paths=[x['path'] for x in checked], free=shutil.disk_usage(base).free,
                      required_free=required, before_receipt=str(output/(ident+'-before.json')))
        _receipt(output/(ident+'-after.json'), result)
        return result
