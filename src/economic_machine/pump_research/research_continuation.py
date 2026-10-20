"""Pinned continuation of a finite public-history inventory, with additive IO.

The old grants and their deadlines remain evidence. This document supplies the
new operational authority; it never edits or reopens an earlier allowance.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from ..muse_maker import MODEL, unique

GIB=1024**3
MAX_REQUEST=64*1024**2
FOLDER='evidence/solana-continuation-v1'
SCHEMA='economic-machine.solana-history-continuation.v1'
AUTHORIZATION='owner_continue_current_public_solana_history_until_acquired'
CATALOG='https://raw.githubusercontent.com/rpcpool/yellowstone-faithful/gha-report/docs/CAR-REPORT.md'
PARENTS={
    'parent_grant_sha256':'evidence/core-trace-v1/grant.json',
    'previous_epoch_grant_sha256':'evidence/address-epoch-v1/grant.json',
    'previous_internal_grant_sha256':'evidence/wallet-map-v1/internal-grant.json',
    'previous_parallel_grant_sha256':'evidence/wallet-map-v1/parallel-grant.json',
    'previous_expansion_grant_sha256':'evidence/solana-expansion-v2/grant.json',
    'original_model_scope_sha256':'evidence/wallet-map-v1/model-scope.json',
}
DENIED=('trading','public_deployment','paid_data_api','new_servers','production_collector_changes')


def _bytes(path,limit):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('CORE_CONTINUATION_FILE')
    with path.open('rb') as source:raw=source.read(limit+1)
    if len(raw)>limit:raise ValueError('CORE_CONTINUATION_FILE_BOUND')
    return raw


def _document(path):
    raw=_bytes(path,65536);value=json.loads(raw,object_pairs_hook=unique)
    if not isinstance(value,dict):raise ValueError('CORE_CONTINUATION_DOCUMENT')
    return value,hashlib.sha256(raw).hexdigest()


def _parents(base):
    docs={};pins={}
    for key,name in PARENTS.items():
        path=base/name;docs[key],pins[key]=_document(path)
        if key not in ('parent_grant_sha256','original_model_scope_sha256'):
            if _bytes(path.with_suffix('.sha256'),256).decode().strip()!=pins[key]:
                raise ValueError('CORE_CONTINUATION_PARENT_PIN')
    old=docs['parent_grant_sha256'];deadline=old.get('deadline_ms')
    if type(deadline) is not int or deadline<=0:raise ValueError('CORE_CONTINUATION_PARENT_DEADLINE')
    if any(doc.get('deadline_ms')!=deadline for doc in docs.values()):
        raise ValueError('CORE_CONTINUATION_PARENT_DEADLINE')
    for key in ('previous_epoch_grant_sha256','previous_internal_grant_sha256',
                'previous_parallel_grant_sha256','previous_expansion_grant_sha256'):
        if docs[key].get('parent_grant_sha256')!=pins['parent_grant_sha256']:
            raise ValueError('CORE_CONTINUATION_PARENT_CHAIN')
    expansion=docs['previous_expansion_grant_sha256'];parallel=docs['previous_parallel_grant_sha256']
    internal=docs['previous_internal_grant_sha256'];model=docs['original_model_scope_sha256']
    if (expansion.get('schema')!='economic-machine.solana-history-expansion.v2'
        or expansion.get('authorization')!='owner_all_public_solana_history_hundred_muse_workers'
        or expansion.get('previous_epoch_grant_sha256')!=pins['previous_epoch_grant_sha256']
        or expansion.get('previous_internal_grant_sha256')!=pins['previous_internal_grant_sha256']
        or expansion.get('total_storage_bytes')!=224*GIB or expansion.get('minimum_host_free_bytes')!=128*GIB
        or parallel.get('authorization')!='owner_private_solana_research_hundred_muse_workers'
        or parallel.get('parallel_models')!=100 or parallel.get('model')!=MODEL
        or parallel.get('supersedes_internal_grant_sha256')!=pins['previous_internal_grant_sha256']
        or parallel.get('original_model_scope_sha256')!=pins['original_model_scope_sha256']
        or internal.get('authorization')!='owner_private_solana_research_ten_muse_workers'
        or internal.get('parallel_models')!=10 or internal.get('model')!=MODEL
        or internal.get('original_model_scope_sha256')!=pins['original_model_scope_sha256']
        or model.get('authorization')!='owner_unlimited_muse_top10_forward_wallet_research'
        or model.get('model')!=MODEL or model.get('maximum_usd') is not None
        or model.get('max_calls') is not None or model.get('trade_authority') is not False
        or any(doc.get(flag) is not False for doc in (expansion,parallel,internal) for flag in DENIED)):
        raise ValueError('CORE_CONTINUATION_PARENT_AUTHORITY')
    return docs,pins,deadline


def _inventory(base):
    folder=base/'evidence/address-epochs-v2'
    receipt,receipt_hash=_document(folder/'catalogue-receipt.json')
    name=receipt.get('path');digest=receipt.get('sha256')
    if (not isinstance(digest,str) or re.fullmatch('[a-f0-9]{64}',digest) is None
        or name!=f'catalogue-{digest}.md' or receipt.get('url')!=CATALOG):
        raise ValueError('CORE_CONTINUATION_INVENTORY_RECEIPT')
    raw=_bytes(folder/name,1024**2)
    if hashlib.sha256(raw).hexdigest()!=digest or receipt.get('bytes')!=len(raw):
        raise ValueError('CORE_CONTINUATION_INVENTORY_HASH')
    epochs=[]
    for line in raw.decode('utf-8').splitlines():
        match=re.match(r'^\|\s*(\d+)\s*\|',line)
        if match:epochs.append(int(match[1]))
    if not epochs or len(epochs)>4096 or len(set(epochs))!=len(epochs) or any(not 0<=e<=100000 for e in epochs):
        raise ValueError('CORE_CONTINUATION_INVENTORY_BOUND')
    return dict(inventory_catalogue_receipt_sha256=receipt_hash,
        inventory_catalogue_sha256=digest,inventory_epochs=sorted(epochs))


def build_grant(base,approved_ms=None):
    """Return a reviewable document only; do not save, approve, or activate it."""
    base=Path(base);_,pins,deadline=_parents(base)
    approved_ms=time.time_ns()//1000000 if approved_ms is None else approved_ms
    if type(approved_ms) is not int or approved_ms<=0:raise ValueError('CORE_CONTINUATION_APPROVAL_TIME')
    return dict(schema=SCHEMA,authorization=AUTHORIZATION,approved_ms=approved_ms,
        deadline_ms=None,original_deadline_ms=deadline,**pins,**_inventory(base),
        stop_condition='current_public_inventory_acquired_or_resource_guard',
        additional_io_bytes=None,max_request_bytes=MAX_REQUEST,
        total_storage_bytes=224*GIB,minimum_host_free_bytes=128*GIB,
        maximum_aggregate_cpu_percent=100,parallel_models=100,model=MODEL,
        source_fetch_parallelism=1,evidence_writer_parallelism=1,
        original_ledgers_preserved=True,original_grants_preserved=True,
        free_public_sources_only=True,extend_inventory_automatically=False,
        **{flag:False for flag in DENIED})


def authority(base,parent_hash=None,legacy_deadline=None,now_ms=None):
    base=Path(base);path=base/FOLDER/'grant.json'
    if not path.exists():return None
    grant,digest=_document(path)
    if _bytes(path.with_suffix('.sha256'),256).decode().strip()!=digest:
        raise ValueError('CORE_CONTINUATION_GRANT_PIN')
    now_ms=time.time_ns()//1000000 if now_ms is None else now_ms
    approved=grant.get('approved_ms')
    if type(now_ms) is not int or type(approved) is not int or not 0<approved<=now_ms:
        raise ValueError('CORE_CONTINUATION_APPROVAL_TIME')
    expected=build_grant(base,approved)
    # Exact fields prevent a missing null deadline or IO cap from masquerading
    # as explicit authority. Type checks also reject bool/float integer aliases.
    if (set(grant)!=set(expected) or any(type(grant[k]) is not type(v) or grant[k]!=v for k,v in expected.items())
        or any(type(epoch) is not int for epoch in grant.get('inventory_epochs',[]))):
        raise ValueError('CORE_CONTINUATION_AUTHORITY')
    if (parent_hash is not None and parent_hash!=grant['parent_grant_sha256']
        or legacy_deadline is not None and legacy_deadline!=grant['original_deadline_ms']):
        raise ValueError('CORE_CONTINUATION_PARENT_ARGUMENT')
    return grant,digest


class ContinuationScope:
    def __init__(self,base,parent_hash=None,legacy_deadline=None):
        self.base=Path(base);loaded=authority(self.base,parent_hash,legacy_deadline)
        if loaded is None:raise ValueError('CORE_CONTINUATION_MISSING')
        self.grant,self.hash=loaded
        self.legacy_deadline_ms=self.grant['original_deadline_ms']
        self.effective_deadline_ms=self.deadline=None
        self.parent_expansion_hash=self.grant['previous_expansion_grant_sha256']
        self.inventory_epochs=frozenset(self.grant['inventory_epochs'])
        self.maximum_slot=(max(self.inventory_epochs)+1)*432000-1
        self.storage_limit=self.grant['total_storage_bytes']
        self.out=self.base/FOLDER;path=self.out/'io.sqlite3'
        self.db=sqlite3.connect(path,timeout=30)
        try:
            self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY,
                    bytes INTEGER NOT NULL CHECK(bytes>0),purpose TEXT NOT NULL,at_ms INTEGER NOT NULL);''')
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                identities=self.db.execute('SELECT hash FROM identity').fetchall()
                if identities and identities!=[(self.hash,)]:raise ValueError('CORE_CONTINUATION_AUTHORITY_CHANGED')
                if not identities and self.db.execute('SELECT 1 FROM reservations LIMIT 1').fetchone():
                    raise ValueError('CORE_CONTINUATION_LEDGER_IDENTITY_MISSING')
                self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(self.hash,))
            self.last_guard=0;self.used=0;self.guard(force=True)
        except BaseException:self.db.close();raise

    def close(self):self.db.close()

    def assert_epoch(self,epoch):
        if type(epoch) is not int or epoch not in self.inventory_epochs:
            raise ValueError('CORE_CONTINUATION_EPOCH_OUTSIDE_INVENTORY')

    def assert_slot(self,slot):
        if type(slot) is not int or slot<0:raise ValueError('CORE_CONTINUATION_SLOT_BOUND')
        self.assert_epoch(slot//432000)

    def guard(self,extra=0,force=False):
        if type(extra) is not int or extra<0:raise ValueError('CORE_CONTINUATION_STORAGE_REQUEST')
        if force or time.monotonic()-self.last_guard>2:
            from .history_core import used_bytes
            self.used=used_bytes(self.base);self.last_guard=time.monotonic()
        if self.used+extra+128*1024**2>self.storage_limit:raise ValueError('CORE_TOTAL_STORAGE_LIMIT')
        if shutil.disk_usage(self.base).free-extra<self.grant['minimum_host_free_bytes']:
            raise ValueError('CORE_HOST_FREE_SPACE')

    def reserved(self):return self.db.execute('SELECT coalesce(sum(bytes),0) FROM reservations').fetchone()[0]

    def remaining_io(self):
        """None means explicitly uncapped cumulative IO, never zero or infinity."""
        return None

    def reserve(self,size,purpose):
        if type(size) is not int or not 0<size<=MAX_REQUEST:raise ValueError('CORE_REQUEST_BOUND')
        if not isinstance(purpose,str) or not 0<len(purpose)<=512:raise ValueError('CORE_RESERVATION_PURPOSE')
        self.guard(size,force=True)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.guard(size,force=True)
            cur=self.db.execute('INSERT INTO reservations(bytes,purpose,at_ms) VALUES(?,?,?)',
                (size,purpose,time.time_ns()//1000000))
        return cur.lastrowid
