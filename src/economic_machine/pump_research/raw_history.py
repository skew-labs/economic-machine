"""Free sparse archive acquisition, metered by the original research IO grant.

Dataset signatures locate evidence; only signed raw transactions can corroborate
their transfer fields. This does not reconstruct exchange private ledgers.
"""
import hashlib
import json
import os
import time
from decimal import Decimal, InvalidOperation
from .archive import Archive, get
from .archive_gate import ArchiveGate, ArchiveDeferred
from .address_index import read_indexed_slot
from .store import atomic, canonical, decode


class ScopedArchive(Archive):
    def __init__(self, graph, scope, store, meta_type, index, wait=True):
        super().__init__(store, meta_type)
        self.graph, self.scope, self.index = graph, scope, index
        self.wait=wait
        self.folder = graph.out/'archive-ranges'
        self.folder.mkdir(exist_ok=True)

    def read_slot(self, slot):
        continuation=getattr(self.scope,'continuation',None)
        if continuation:continuation.assert_slot(slot)
        return read_indexed_slot(self, slot, self.index, self.scope.guard)

    def request(self, url, start, end):
        size = end-start+1
        if not 0 < size <= 16*1024**2:
            raise ValueError('MAP_ARCHIVE_RANGE_BOUND')
        ident = hashlib.sha256(f'{url}:{start}:{end}'.encode()).hexdigest()
        path, manifest = self.folder/(ident+'.range'), self.folder/(ident+'.json')
        if path.exists() and manifest.exists():
            raw = path.read_bytes(); m = json.loads(manifest.read_bytes())
            if (m['url'] != url or m['start'] != start or m['bytes'] != size or
                len(raw) != size or hashlib.sha256(raw).hexdigest() != m['sha256']):
                raise ValueError('MAP_ARCHIVE_CACHE_CORRUPT')
            return raw
        # Adjacent 12-byte lookups share one small range request. In particular,
        # a launch window does not hammer the provider once per empty slot.
        if url.endswith('-slot-ranges.raw') and size==12:
            page=(start//3072)*3072;last=min(page+3071,432000*12-1)
            return self.request(url,page,last)[start-page:start-page+12]
        cached = sum(p.stat().st_size for p in (self.graph.base/'evidence/core-trace-v1/ranges').glob('*.range'))
        remaining=self.scope.remaining_io() if hasattr(self.scope,'remaining_io') else self.scope.grant['additional_io_bytes']-self.scope.reserved()
        if remaining is not None and size+max(0,9139504504-cached)+64*1024**2 > remaining:
            raise ValueError('MAP_IO_RESERVED_FOR_CORE_TABLES')
        with ArchiveGate(self.folder,self.scope.guard,wait=self.wait):
            self.scope.reserve(size, 'wallet-map:'+ident)
            raw = get(url, size, (start,end))
        with path.open('wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        atomic(manifest, dict(url=url,start=start,bytes=size,sha256=hashlib.sha256(raw).hexdigest()))
        self.bytes += size
        return raw


def corroborate(edge, block):
    body = json.loads(edge['body'])
    signature = body.get('signature')
    try:valid_signature=isinstance(signature,str) and len(decode(signature))==64
    except ValueError:valid_signature=False
    if not valid_signature:
        return dict(state='invalid_dataset_signature')
    if block is None:
        return dict(state='provider_index_absent',not_proven_skipped=True)
    tx = next((t for t in block['transactions'] if t['signature']==signature), None)
    if tx is None:
        return dict(state='signature_not_decoded',decode_errors=block['decode_errors'])
    result = dict(signature=signature,tx_order=tx['index'],transaction_success=tx['success'],
                  signature_verified=True,independent_consensus_verified=False)
    if not tx['success']:
        return dict(result,state='transaction_failed')
    if body.get('symbol') != 'SOL':
        return dict(result,state='unsupported_asset_units')
    try:
        if not isinstance(body['amount'],str) or len(body['amount'])>128:raise ValueError('amount')
        amount=Decimal(body['amount'])
        if not amount.is_finite() or not 0<amount<=Decimal('18446744073.709551615'):raise ValueError('amount')
        lamports = amount*1000000000
        if not lamports.is_finite() or lamports<=0 or lamports!=lamports.to_integral_value():
            raise ValueError('amount')
    except (InvalidOperation,ValueError,KeyError):
        return dict(result,state='invalid_dataset_amount')
    matched = [t for t in tx['transfers'] if t['source']==edge['source'] and
               t['destination']==edge['target'] and t['mint']=='SOL' and int(t['amount'])==int(lamports)]
    return dict(result,state='raw_transfer_corroborated' if matched else 'transfer_fields_mismatch',
                matched_instructions=matched,expected_lamports=str(int(lamports)))


class RawHistory:
    def __init__(self, graph):
        self.g = graph
        graph.db.executescript('''
          CREATE TABLE IF NOT EXISTS raw_history_jobs(job TEXT PRIMARY KEY,edge TEXT UNIQUE);
          CREATE TABLE IF NOT EXISTS raw_history_results(edge TEXT PRIMARY KEY,body TEXT);
        ''')

    def seed(self, guard):
        # One outstanding raw block per root; custody entry evidence comes first.
        # Source records and original frontier cursors are never overwritten.
        with self.g.db:
            for root, in self.g.db.execute('SELECT mint FROM roots').fetchall():
                guard()
                if self.g.db.execute("SELECT 1 FROM frontier WHERE root=? AND source='archive_transfer' AND state IN ('pending','source_blocked')",(root,)).fetchone():
                    continue
                edge=self.g.db.execute('''SELECT e.* FROM edges e WHERE root=? AND kind='funding_record'
                  AND slot BETWEEN 0 AND 1000000000
                  AND NOT EXISTS(SELECT 1 FROM raw_history_jobs j WHERE j.edge=e.id)
                  ORDER BY EXISTS(SELECT 1 FROM custody_cases c WHERE c.entry_edge=e.id) DESC,e.rowid LIMIT 1''',(root,)).fetchone()
                if not edge:continue
                job=hashlib.sha256(canonical(['raw_history',edge['id']]).encode()).hexdigest()[:24]
                self.g.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',
                    (job,root,edge['source'],edge['slot'],edge['stamp'],0,'pending','null','archive_transfer'))
                self.g.db.execute('INSERT OR IGNORE INTO raw_history_jobs VALUES(?,?)',(job,edge['id']))

    def run(self, job, scope):
        from .address_index import AddressIndex
        from .cli import proto
        row=self.g.db.execute('SELECT e.* FROM raw_history_jobs j JOIN edges e ON e.id=j.edge WHERE j.job=?',(job['id'],)).fetchone()
        if row is None:raise ValueError('RAW_HISTORY_EDGE_MISSING')
        edge=dict(row);scope.guard(64*1024**2,force=True)
        _,meta=proto('/srv/economic-machine/collector')
        index=AddressIndex(self.g.out/'address-index.sqlite3')
        try:
            reader=ScopedArchive(self.g,scope,None,meta,index,wait=False)
            block,receipt=reader.read_slot(edge['slot'])
            result=corroborate(edge,block)
            result.update(edge_id=edge['id'],job_id=job['id'],slot=edge['slot'],
                          source_record_retained=True,full_wallet_history=False,
                          same_funds_continuity_proven=False,private_ledger_recovered=False)
            if receipt:
                result['receipt']={k:v for k,v in receipt.items() if k!='raw'}
                result['block_transactions_decoded']=len(block['transactions'])
            with self.g.db:
                self.g.db.execute('INSERT OR REPLACE INTO raw_history_results VALUES(?,?)',(edge['id'],canonical(result)))
                self.g.db.execute('UPDATE frontier SET state=? WHERE id=?',(result['state'],job['id']))
                if result['state']=='raw_transfer_corroborated':
                    for transfer in result['matched_instructions']:
                        # Independently observed edge retains the original record link.
                        self.g.edge(edge['root'],edge['source'],edge['target'],'raw_archive_transfer',edge['slot'],block['time']*1000 if block['time'] is not None else None,
                            signature=result['signature'],tx_order=result['tx_order'],ix=transfer['ix'],amount=transfer['amount'],symbol='SOL',
                            transfer_kind=transfer.get('kind'),authority=transfer.get('authority'),
                            source='sparse_archive',source_edge_id=edge['id'],source_sha256=receipt['car_sha256'],
                            finality='archive_content_verified',independent_consensus_verified=False,same_funds_continuity_proven=False)
                        # Recipient history remains an unattributed followup after a custody boundary.
                        cases=self.g.db.execute('SELECT id FROM custody_cases WHERE entry_edge=?',(edge['id'],)).fetchall()
                        for source in ('indexed_forward','core_forward'):
                            nxt=self.g.enqueue(edge['root'],edge['target'],edge['slot'] if source=='indexed_forward' else None,
                                block['time']*1000 if block['time'] is not None else edge['stamp'],1,source,
                                after_order=(result['tx_order'],transfer['ix']) if source=='indexed_forward' else None)
                            for case in cases:self.g.db.execute('INSERT OR IGNORE INTO custody_jobs VALUES(?,?)',(nxt,case[0]))
            return result
        finally:index.close()

    def snapshot(self):
        return dict(states=dict(self.g.db.execute("SELECT state,count(*) FROM frontier WHERE source='archive_transfer' GROUP BY state")),
            results=[json.loads(r[0]) for r in self.g.db.execute('SELECT body FROM raw_history_results ORDER BY rowid DESC LIMIT 100')],
            source='Old Faithful public CAR ranges',paid_api=False,full_wallet_history=False,
            independent_consensus_verified=False,private_ledger_recovered=False)
