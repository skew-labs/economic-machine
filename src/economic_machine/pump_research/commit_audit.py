"""Bounded offline CPI-policy audit of already cached, receipt-pinned CARs.

No downloader or network fallback exists here. Historical blocks, transfers,
activity, graph edges and cursors are retained. Only new policy evidence and
immutable audit receipts are appended; consumers decide admissibility.
"""
import hashlib
import json
from pathlib import Path
from .address_index import parse_index_block,install_commit_revision_tracking,RAW_COMMIT_BLOCK_SQL
from .archive import BASE
from .store import canonical,decode


POLICY_VERSION=1


def _retained_raw(row,receipt,folder,object_roots,remaining):
    if receipt.get('car_sha256')!=row['hash']:return dict(state='unsupported_cache_receipt')
    locator=receipt.get('locator')
    if locator is not None and not isinstance(locator,str):raise ValueError('COMMIT_AUDIT_OBJECT_LOCATOR')
    if locator in ('original_research_object:'+row['hash'],'recovered_research_object:'+row['hash']):
        from . import object_store
        for root in object_roots:
            root=Path(root);plain,packed=object_store.paths(root,row['hash'])
            if root.is_symlink() or (root/'objects').is_symlink() or plain.is_symlink() or packed.is_symlink():
                raise ValueError('COMMIT_AUDIT_CACHE_SYMLINK')
            candidate=packed if packed.exists() else plain
            if not candidate.exists():continue
            size=candidate.stat().st_size
            if not 0<size<=object_store.MAX_RAW+65536:raise ValueError('COMMIT_AUDIT_OBJECT_BOUND')
            bound=object_store.MAX_RAW if candidate==packed else size
            if bound>remaining:return dict(state='byte_bound_reached')
            raw=object_store.read(root,row['hash'])
            if len(raw)>remaining:raise ValueError('COMMIT_AUDIT_OBJECT_BOUND')
            return dict(state='object_loaded',raw=raw,identity='retained_object:'+row['hash'])
        return dict(state='cache_unavailable')
    if locator and ('research_object:' in locator):raise ValueError('COMMIT_AUDIT_OBJECT_LOCATOR')
    size=receipt.get('car_size');offset=receipt.get('car_offset')
    if type(size) is not int or not 0<size<=16*1024**2 or type(offset) is not int or offset<0:
        return dict(state='unsupported_cache_receipt')
    if size>remaining:return dict(state='byte_bound_reached')
    epoch=row['slot']//432000;url=f'{BASE}/{epoch}/epoch-{epoch}.car'
    ident=hashlib.sha256(f'{url}:{offset}:{offset+size-1}'.encode()).hexdigest()
    raw_path=folder/(ident+'.range');manifest_path=folder/(ident+'.json')
    if raw_path.is_symlink() or manifest_path.is_symlink():raise ValueError('COMMIT_AUDIT_CACHE_SYMLINK')
    if not raw_path.exists() or not manifest_path.exists():return dict(state='cache_unavailable')
    if raw_path.stat().st_size!=size or not 0<manifest_path.stat().st_size<=4096:
        raise ValueError('COMMIT_AUDIT_CACHE_BOUND')
    manifest=json.loads(manifest_path.read_bytes())
    if (manifest.get('url')!=url or manifest.get('start')!=offset or manifest.get('bytes')!=size or
        manifest.get('sha256')!=row['hash']):raise ValueError('COMMIT_AUDIT_CACHE_MANIFEST')
    with raw_path.open('rb') as stream:raw=stream.read(size+1)
    if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=row['hash']:
        raise ValueError('COMMIT_AUDIT_CACHE_HASH')
    return dict(state='range_loaded',raw=raw,identity=ident)


def audit_cached_commits(index,folder,meta_type,guard,*,after_id=0,limit=16,max_bytes=64*1024**2,object_roots=()):
    if (type(after_id) is not int or after_id<0 or type(limit) is not int or not 1<=limit<=128 or
        type(max_bytes) is not int or not 1<=max_bytes<=256*1024**2 or
        not isinstance(object_roots,(tuple,list)) or len(object_roots)>2):
        raise ValueError('COMMIT_AUDIT_BOUND')
    guard();folder=Path(folder)
    with index.db:
        index.db.execute('''CREATE TABLE IF NOT EXISTS commit_cache_audits(
            id TEXT PRIMARY KEY,block INTEGER,policy_version INTEGER,body TEXT)''')
        install_commit_revision_tracking(index.db)
    rows=index.db.execute('SELECT * FROM blocks WHERE '+RAW_COMMIT_BLOCK_SQL+' AND id>? ORDER BY id LIMIT ?',(after_id,limit)).fetchall()
    report=dict(schema='economic-machine.cached-commit-audit.v1',after_id=after_id,next_after_id=after_id,
        scanned_blocks=0,cache_bytes_read=0,policies_appended=0,blocks=[],network_requests=0,
        transfer_rows_modified=0,full_history_verified=False,independent_consensus_verified=False)
    for row in rows:
        guard();record=dict(block=row['id'],slot=row['slot'],state='cache_unavailable')
        receipt=json.loads(row['receipt'])
        loaded=_retained_raw(row,receipt,folder,object_roots,max_bytes-report['cache_bytes_read'])
        record['state']=loaded['state']
        if loaded['state']=='byte_bound_reached':
            report['byte_bound_reached']=True;break
        else:
            raw=loaded.get('raw');ident=loaded.get('identity')
            if raw is not None:
                report['cache_bytes_read']+=len(raw)
                block=parse_index_block(raw,row['slot'],meta_type)
                candidates=[];unknown=[];without_policy=[]
                for tx in block['transactions']:
                    prior=index.db.execute('SELECT id,tx_order,successful FROM transactions WHERE block=? AND signature=?',
                        (row['id'],decode(tx['signature']))).fetchone()
                    if prior is None:
                        unknown.append(tx['signature']);continue
                    if prior['tx_order']!=tx['index'] or bool(prior['successful'])!=bool(tx['success']):
                        raise ValueError('COMMIT_AUDIT_TRANSACTION_CONFLICT')
                    evidence=tx.get('instruction_commit_evidence')
                    if not evidence:
                        without_policy.append(prior['id']);continue
                    if type(evidence.get('version')) is not int or evidence['version']!=POLICY_VERSION:
                        raise ValueError('COMMIT_AUDIT_POLICY_VERSION')
                    candidates.append((prior['id'],canonical(evidence)))
                guard()
                with index.db:
                    added=0
                    for tid,body in candidates:
                        old=index.db.execute('SELECT body FROM tx_commit_evidence WHERE tx=? AND version=?',(tid,POLICY_VERSION)).fetchone()
                        if old and old[0]!=body:raise ValueError('INDEX_COMMIT_EVIDENCE_CONFLICT')
                        if old is None:
                            index.db.execute('INSERT INTO tx_commit_evidence VALUES(?,?,?)',(tid,POLICY_VERSION,body));added+=1
                    audit=dict(block=row['id'],slot=row['slot'],car_sha256=row['hash'],policy_version=POLICY_VERSION,
                        cache_identity=ident,policy_transactions=[tid for tid,_ in candidates],
                        unindexed_decoded_signatures=unknown,transactions_without_policy=without_policy,
                        decode_errors=block['decode_errors'],transaction_nodes=block['transaction_nodes'],
                        decoded_transactions=len(block['transactions']),raw_content_reverified=True,
                        transaction_signatures_reverified=True,independent_consensus_verified=False,
                        existing_projection_unchanged=True)
                    body=canonical(audit);identity=hashlib.sha256(body.encode()).hexdigest()
                    index.db.execute('INSERT OR IGNORE INTO commit_cache_audits VALUES(?,?,?,?)',
                        (identity,row['id'],POLICY_VERSION,body))
                record.update(state='audited',audit_id=identity,policies_appended=added,
                    decoded_transactions=len(block['transactions']),unindexed_transactions=len(unknown),
                    decode_errors=block['decode_errors'])
                report['policies_appended']+=added
        report['blocks'].append(record);report['scanned_blocks']+=1;report['next_after_id']=row['id']
    report['more_blocks']=index.db.execute('SELECT 1 FROM blocks WHERE '+RAW_COMMIT_BLOCK_SQL+' AND id>? LIMIT 1',(report['next_after_id'],)).fetchone() is not None
    return report
