"""Read-only validation of additive model-planning capacity authority.

This changes neither model accounting nor archive grants. All original files
and rows remain in place; the research coordinator owns every evidence write.
"""
import hashlib
import json
from pathlib import Path
import time

from ..muse_maker import MODEL, unique


def _read(path):
    path=Path(path)
    if path.stat().st_size>65536:raise ValueError('MAP_PARALLEL_GRANT_SIZE')
    raw=path.read_bytes()
    if len(raw)>65536:raise ValueError('MAP_PARALLEL_GRANT_SIZE')
    value=json.loads(raw,object_pairs_hook=unique)
    if not isinstance(value,dict):raise ValueError('MAP_PARALLEL_GRANT_SHAPE')
    return raw,value,hashlib.sha256(raw).hexdigest()


def load_parallel_authority(graph,scope,path=None,now_ms=None):
    """Validate the pinned child; caller decides activation and pins its hash."""
    scope.guard()
    path=Path(path) if path is not None else graph.out/'parallel-grant.json'
    raw,grant,digest=_read(path)
    pin=path.with_suffix('.sha256')
    if pin.stat().st_size>256 or pin.read_text().strip()!=digest:
        raise ValueError('MAP_PARALLEL_GRANT_HASH')
    _,legacy,legacy_hash=_read(graph.out/'internal-grant.json')
    legacy_pin=graph.out/'internal-grant.sha256'
    if legacy_pin.stat().st_size>256 or legacy_pin.read_text().strip()!=legacy_hash:
        raise ValueError('MAP_PARALLEL_LEGACY_HASH')
    _,model,model_hash=_read(graph.out/'model-scope.json')
    deadline=scope.grant['deadline_ms']
    if (grant.get('schema')!='economic-machine.parallel-research-scope.v1'
            or grant.get('authorization')!='owner_private_solana_research_hundred_muse_workers'
            or grant.get('parallel_models')!=100 or type(grant.get('parallel_models')) is not int
            or grant.get('model')!=MODEL or grant.get('parent_grant_sha256')!=scope.hash
            or grant.get('supersedes_internal_grant_sha256')!=legacy_hash
            or grant.get('original_model_scope_sha256')!=model_hash
            or type(grant.get('deadline_ms')) is not int or grant['deadline_ms']!=deadline
            or grant.get('source_fetch_parallelism')!=1 or type(grant.get('source_fetch_parallelism')) is not int
            or grant.get('evidence_writer_parallelism')!=1 or type(grant.get('evidence_writer_parallelism')) is not int
            or grant.get('maximum_aggregate_cpu_percent')!=100
            or grant.get('total_storage_bytes')!=scope.storage_limit
            or any(grant.get(flag) is not False for flag in ('trading','public_deployment','paid_data_api',
                'new_servers','production_collector_changes'))):
        raise ValueError('MAP_PARALLEL_AUTHORITY')
    if (legacy.get('authorization')!='owner_private_solana_research_ten_muse_workers'
            or legacy.get('parallel_models')!=10 or legacy.get('deadline_ms')!=deadline
            or legacy.get('parent_grant_sha256')!=scope.hash
            or legacy.get('original_model_scope_sha256')!=model_hash
            or legacy.get('model')!=MODEL
            or model.get('authorization')!='owner_unlimited_muse_top10_forward_wallet_research'
            or model.get('model')!=MODEL or model.get('deadline_ms')!=deadline
            or model.get('trade_authority') is not False or model.get('maximum_usd') is not None
            or model.get('max_calls') is not None):
        raise ValueError('MAP_PARALLEL_PARENT_AUTHORITY')
    pinned=graph.state('parallel_authority_sha256')
    if pinned is not None and pinned!=digest:raise ValueError('MAP_PARALLEL_AUTHORITY_CHANGED')
    now_ms=time.time_ns()//1000000 if now_ms is None else now_ms
    continuation=getattr(scope,'continuation',None)
    if type(now_ms) is not int or (continuation is None and now_ms>=deadline):raise ValueError('MAP_PARALLEL_DEADLINE')
    if continuation is not None:continuation.guard()
    return dict(parallel_models=100,grant_sha256=digest,deadline_ms=continuation.effective_deadline_ms if continuation else deadline,model=MODEL,
        continuation_sha256=continuation.hash if continuation else None,original_deadline_ms=deadline,
        supersedes_internal_grant_sha256=legacy_hash,original_model_scope_sha256=model_hash,
        source_fetch_parallelism=1,evidence_writer_parallelism=1,
        maximum_aggregate_cpu_percent=100,total_storage_bytes=scope.storage_limit,
        original_grants_preserved=True,model_ledger_preserved=True)
