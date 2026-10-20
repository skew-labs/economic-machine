"""Additive chain authority; the existing shared monthly ledger is immutable."""
import hashlib
import json
import os
from pathlib import Path
import time

from economic_machine.pump_research.research_spend import authority as shared_authority
from economic_machine.pump_research.store import canonical

SCHEMA = 'economic-machine.robinhood-genesis-research.v1'
MODEL = 'meta/muse-spark-1.3-contributor'


def grant(shared_base, approved_ms):
    loaded = shared_authority(shared_base)
    if loaded is None:
        raise ValueError('SHARED_BUDGET_REQUIRED')
    return dict(schema=SCHEMA, approved_ms=approved_ms,
        authorization='owner_robinhood_10_muse_and_arbitrum_ethereum_crosschain',
        chain_id=4663, start_block=0, target='continuous_provider_finalized_head',
        all_transactions=True, all_contract_logs=True, all_available_traces=True,
        protocol_allowlist=None, investigators=10, resource_managers=0, model=MODEL,
        resource_assignment='shared_host_native_admission', crosschain_ids=[1,42161,4663], counterpart_matching='protocol_message_and_success_receipts', never_infer_private_ledger=True,
        shared_monthly_budget_sha256=loaded[1], monthly_limit_krw=1000000,
        shared_blob_content_addressed_namespace=True, private_only=True,
        independent_consensus_verified=False, private_exchange_ledger_available=False,
        production_service_changes=False, trading=False, new_servers=False,
        paid_data_api=False, cloud_object_deletion=False,
        verified_local_duplicate_retirement=True, old_grants_and_journals_preserved=True)


def activate(base, shared_base):
    base=Path(base); out=base/'evidence'; out.mkdir(parents=True, exist_ok=True)
    p=out/'grant.json'
    if p.exists():
        return authority(base, shared_base)
    value=grant(shared_base, time.time_ns()//1_000_000)
    raw=(canonical(value)+'\n').encode(); digest=hashlib.sha256(raw).hexdigest()
    for path, data in [(p,raw),(p.with_suffix('.sha256'),(digest+'\n').encode())]:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
    return value,digest


def authority(base, shared_base):
    p=Path(base)/'evidence/grant.json'
    if p.is_symlink() or p.stat().st_size>16384:
        raise ValueError('ARBITRUM_AUTHORITY_FILE')
    raw=p.read_bytes();value=json.loads(raw);digest=hashlib.sha256(raw).hexdigest()
    stamp=value.get('approved_ms')
    if (type(stamp) is not int or stamp>time.time_ns()//1_000_000
            or value!=grant(shared_base,stamp)
            or p.with_suffix('.sha256').read_text().strip()!=digest):
        raise ValueError('ARBITRUM_AUTHORITY_CHANGED')
    return value,digest
