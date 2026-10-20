"""Immutable compressed source pages and index shards on the shared Blob ledger.

Each object is uploaded and fully downloaded by the existing native connector.
Arbitrum has its own staging directory: it never writes Solana block.tmp files.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import uuid

from economic_machine.pump_research.research_cloud import Cloud, identity
from economic_machine.pump_research.store import atomic, canonical

BLOCK=8*1024**2


class Storage:
    def __init__(self,base,shared_base,*,chain_id=42161,namespace=None):
        if type(chain_id) is not int or chain_id not in (42161,4663):raise ValueError('STORAGE_CHAIN')
        if namespace not in (None,'crosschain'):raise ValueError('STORAGE_NAMESPACE')
        self.chain_id=chain_id
        self.base=Path(base);self.cloud=Cloud(shared_base)
        # Shared SQLite admission is atomic. The legacy status publisher uses a
        # fixed temporary filename, so this worker publishes its own projection.
        self.cloud.spend.publish=lambda:atomic(self.base/('evidence/shared-budget-snapshot.json' if namespace is None else 'evidence/crosschain-budget-snapshot.json'),self.cloud.spend.totals())
        self.staging=self.base/'staging'
        if namespace:self.staging=self.staging/namespace
        self.staging.mkdir(parents=True,exist_ok=True)
        self.receipts=self.base/'evidence/objects';self.receipts.mkdir(parents=True,exist_ok=True)

    def close(self):self.cloud.close()

    def put(self,raw,kind,source=None):
        if not isinstance(raw,bytes) or not raw or len(raw)>64*1024**2:
            raise ValueError('ARBITRUM_OBJECT_BOUND')
        raw_sha=hashlib.sha256(raw).hexdigest()
        packed=gzip.compress(raw,compresslevel=3,mtime=0)
        if len(packed)>64*1024**2:raise ValueError('ARBITRUM_COMPRESSED_BOUND')
        path=self.staging/(hashlib.sha256(packed).hexdigest()+'.gz')
        if path.exists():
            if path.is_symlink() or path.read_bytes()!=packed:raise ValueError('ARBITRUM_STAGING_CHANGED')
        else:
            with path.open('xb') as f:f.write(packed);f.flush();os.fsync(f.fileno())
        try:
            ref=self.cloud.upload(path)
            proof=dict(schema='economic-machine.arbitrum-object.v1',kind=kind,
                raw_sha256=raw_sha,raw_bytes=len(raw),encoding='gzip',chain_id=self.chain_id,
                cloud=ref,source=source,complete_cloud_readback=True)
            proof_id=hashlib.sha256(canonical(proof).encode()).hexdigest()
            atomic(self.receipts/(proof_id+'.json'),proof)
            # Removal is limited to the verified staging duplicate, never originals.
            path.unlink()
            return proof
        except BaseException:
            # Keep the failed attempt for reconciliation; do not erase it on restart.
            raise

    def read(self,proof):
        if proof.get('chain_id')!=self.chain_id or proof.get('encoding')!='gzip' or proof.get('complete_cloud_readback') is not True:
            raise ValueError('ARBITRUM_OBJECT_PROOF')
        ref=proof['cloud']
        with self.cloud.cached(ref['sha256'],ref['bytes']) as f:packed=f.read()
        import zlib
        decoder=zlib.decompressobj(31)
        raw=decoder.decompress(packed,proof['raw_bytes']+1)
        if (not decoder.eof or decoder.unused_data or len(raw)!=proof['raw_bytes']
                or hashlib.sha256(raw).hexdigest()!=proof['raw_sha256']):
            raise ValueError('ARBITRUM_CLOUD_RAW_HASH')
        return raw

    def archive_index(self,path,meta):
        """Caller closes/checkpoints SQLite; portable manifest commits before unlink."""
        path=Path(path);before=identity(path);parts=[];whole=hashlib.sha256()
        with path.open('rb') as f:
            while raw:=f.read(BLOCK):
                whole.update(raw);parts.append(self.put(raw,'sqlite_index_part'))
        if identity(path)!=before:raise ValueError('ARBITRUM_INDEX_CHANGED')
        value=dict(schema='economic-machine.arbitrum-index-shard.v1',chain_id=self.chain_id,
            bytes=before['bytes'],sha256=whole.hexdigest(),parts=parts,metadata=meta)
        proof=self.put((canonical(value)+'\n').encode(),'sqlite_index_catalog')
        catalog=self.base/'evidence/shards';catalog.mkdir(exist_ok=True)
        atomic(catalog/(value['sha256']+'.json'),dict(manifest=value,cloud_proof=proof))
        if identity(path)!=before:raise ValueError('ARBITRUM_INDEX_CHANGED')
        # All constituent objects and the reconstructible manifest are verified.
        path.unlink()
        return dict(manifest=value,cloud_proof=proof)
