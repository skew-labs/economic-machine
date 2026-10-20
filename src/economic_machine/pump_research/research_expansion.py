"""Additive authority and one durable IO ledger for private history expansion.

Old grants and their reservations remain immutable. Model workers never create
their own allowance: every source client reserves against this SQLite ledger.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

GIB = 1024**3
MAX_REQUEST = 64*1024**2
FOLDER = 'evidence/solana-expansion-v2'


def authority(base, parent_hash=None, deadline=None):
    base = Path(base)
    path = base/FOLDER/'grant.json'
    if not path.exists():
        return None
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    g = json.loads(raw)
    parent = base/'evidence/core-trace-v1/grant.json'
    old = parent.read_bytes()
    parent_digest = hashlib.sha256(old).hexdigest()
    old_deadline = json.loads(old)['deadline_ms']
    if (parent_hash is not None and parent_hash != parent_digest or
            deadline is not None and deadline != old_deadline or
            path.with_suffix('.sha256').read_text().strip() != digest or
            g.get('schema') != 'economic-machine.solana-history-expansion.v2' or
            g.get('authorization') != 'owner_all_public_solana_history_hundred_muse_workers' or
            g.get('parent_grant_sha256') != parent_digest or
            g.get('deadline_ms') != old_deadline or
            type(g.get('approved_ms')) is not int or
            not 0 < g['approved_ms'] < old_deadline or
            g.get('additional_io_bytes') != 128*GIB or
            g.get('total_storage_bytes') != 224*GIB or
            g.get('minimum_host_free_bytes') != 128*GIB or
            g.get('maximum_aggregate_cpu_percent') != 100 or
            g.get('epoch_scope') != 'all_public_solana_history' or
            g.get('priority_epochs') != [1009,1008] or
            g.get('original_ledgers_preserved') is not True or
            any(g.get(k) is not False for k in (
                'trading','public_deployment','paid_data_api','new_servers',
                'production_collector_changes'))):
        raise ValueError('CORE_EXPANSION_AUTHORITY')
    legacy = base/'evidence/address-epoch-v1/grant.json'
    internal = base/'evidence/wallet-map-v1/internal-grant.json'
    for name,p in (('previous_epoch_grant_sha256',legacy),
                   ('previous_internal_grant_sha256',internal)):
        if g.get(name) != hashlib.sha256(p.read_bytes()).hexdigest():
            raise ValueError('CORE_EXPANSION_PREVIOUS_AUTHORITY')
    return g,digest


class ExpansionScope:
    def __init__(self, base, parent_hash=None, deadline=None, continuation=None):
        self.base=Path(base)
        loaded=authority(self.base,parent_hash,deadline)
        if loaded is None:raise ValueError('CORE_EXPANSION_MISSING')
        self.grant,self.hash=loaded
        self.deadline=self.grant['deadline_ms']
        if continuation is None:
            from .research_continuation import authority as continued, ContinuationScope
            continuation=ContinuationScope(self.base,parent_hash,deadline) if continued(self.base,parent_hash,deadline) else None
            self._owns_continuation=continuation is not None
        else:self._owns_continuation=False
        self.continuation=continuation
        if continuation is not None and continuation.parent_expansion_hash!=self.hash:
            raise ValueError('CORE_CONTINUATION_EXPANSION_MISMATCH')
        self.effective_deadline_ms=continuation.effective_deadline_ms if continuation else self.deadline
        self.out=self.base/FOLDER
        self.db=sqlite3.connect(self.out/'io.sqlite3',timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS reservations(
                id INTEGER PRIMARY KEY,bytes INTEGER NOT NULL CHECK(bytes>0),
                purpose TEXT NOT NULL,at_ms INTEGER NOT NULL);
        ''')
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                identities=self.db.execute('SELECT hash FROM identity').fetchall()
                if identities and identities != [(self.hash,)]:
                    raise ValueError('CORE_EXPANSION_AUTHORITY_CHANGED')
                self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(self.hash,))
            self.last_guard=0
            self.guard(force=True)
        except BaseException:
            self.db.close()
            if self._owns_continuation:self.continuation.close()
            raise

    def close(self):
        self.db.close()
        if self._owns_continuation:self.continuation.close()

    def guard(self,extra=0,force=False):
        if type(extra) is not int or extra<0:raise ValueError('CORE_EXPANSION_STORAGE_REQUEST')
        if self.continuation:
            self.continuation.guard(extra,force);self.used=self.continuation.used;return
        if time.time_ns()//1000000 >= self.deadline:raise ValueError('CORE_DEADLINE')
        if force or time.monotonic()-self.last_guard>2:
            from .history_core import used_bytes
            self.used=used_bytes(self.base)
            self.last_guard=time.monotonic()
        if self.used+extra+128*1024**2>self.grant['total_storage_bytes']:
            raise ValueError('CORE_TOTAL_STORAGE_LIMIT')
        if shutil.disk_usage(self.base).free-extra<self.grant['minimum_host_free_bytes']:
            raise ValueError('CORE_HOST_FREE_SPACE')

    def reserved(self):
        original=self.db.execute('SELECT coalesce(sum(bytes),0) FROM reservations').fetchone()[0]
        return original+(self.continuation.reserved() if self.continuation else 0)

    def remaining_io(self):
        if self.continuation:return self.continuation.remaining_io()
        return self.grant['additional_io_bytes']-self.reserved()

    def reserve(self,size,purpose):
        if type(size) is not int or not 0<size<=MAX_REQUEST:
            raise ValueError('CORE_REQUEST_BOUND')
        if not isinstance(purpose,str) or not 0<len(purpose)<=512:
            raise ValueError('CORE_RESERVATION_PURPOSE')
        if self.continuation:return self.continuation.reserve(size,purpose)
        self.guard(size,force=True)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            # Acquiring SQLite's writer lock can wait. Admission must still be
            # within the absolute deadline after that wait has ended.
            self.guard(size,force=True)
            if self.reserved()+size>self.grant['additional_io_bytes']:
                raise ValueError('CORE_ADDITIONAL_IO_LIMIT')
            cur=self.db.execute('INSERT INTO reservations(bytes,purpose,at_ms) VALUES(?,?,?)',
                (size,purpose,time.time_ns()//1000000))
        return cur.lastrowid
