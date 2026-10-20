"""Append-only instruction-commit assessment of existing graph evidence.

Research cursors, edge bodies and funding observations are never rewritten.
Reassessment uses a separate bounded pass, including when raw policy evidence
arrives after an edge was previously classified as unchecked.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager,ExitStack
from .address_index import transfer_commit_status
from .store import canonical,decode,encode


RAW_EDGE_KINDS=frozenset(('indexed_transfer','raw_archive_transfer','sol_transfer','create','launch_payer',
    'early_buy','early_sell','later_buy','later_sell','later_create','later_launch_payer'))
TRANSFER_KINDS=frozenset(('indexed_transfer','raw_archive_transfer','sol_transfer'))
_REVISION_UNSET=object()


def edge_requires_commit(edge):
    return edge['kind'] in RAW_EDGE_KINDS


def _sha(value):return hashlib.sha256(value.encode()).hexdigest()


def _schema(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS edge_commit_assessments(id INTEGER PRIMARY KEY,edge TEXT,revision TEXT,
        pass_id INTEGER,edge_rowid INTEGER,status TEXT,body TEXT,UNIQUE(edge,revision));
      CREATE TABLE IF NOT EXISTS edge_commit_passes(id INTEGER PRIMARY KEY,revision TEXT,start_cursor INTEGER,
        upper_cursor INTEGER,body TEXT);
      CREATE TABLE IF NOT EXISTS edge_commit_checkpoints(id INTEGER PRIMARY KEY,pass_id INTEGER,cursor INTEGER,
        revision TEXT,done INTEGER,body TEXT);
      CREATE INDEX IF NOT EXISTS edge_commit_checkpoint_pass ON edge_commit_checkpoints(pass_id,id);
    ''')


def _revision(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commit_source_revision'").fetchone():
        row=db.execute('SELECT identity,version FROM commit_source_revision WHERE id=1').fetchone()
        if row is None:raise ValueError('EDGE_COMMIT_SOURCE_REVISION_MISSING')
        value=dict(source_identity=row[0],source_revision=row[1],assessment_policy=1)
        return _sha(canonical(value)),value
    # Compatibility only for old read-only fixtures prior to AddressIndex migration.
    policy=db.execute('SELECT count(*),coalesce(max(tx),0),coalesce(max(version),0) FROM tx_commit_evidence').fetchone()
    tx=db.execute('SELECT coalesce(max(id),0),count(*),coalesce(sum(successful),0) FROM transactions').fetchone()
    blocks=db.execute("SELECT coalesce(max(id),0),count(*),sum(CASE WHEN state='dead_branch' THEN 1 ELSE 0 END) FROM blocks").fetchone()
    cache=[0,0]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commit_cache_audits'").fetchone():
        cache=list(db.execute('SELECT count(*),coalesce(max(rowid),0) FROM commit_cache_audits').fetchone())
    value=dict(policy=list(policy),transactions=list(tx),blocks=list(blocks),cache_audits=cache,assessment_policy=1)
    return _sha(canonical(value)),value


def _checkpoint_revision(graph):
    fallback=None
    if graph.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_commit_checkpoints'").fetchone():
        row=graph.db.execute('SELECT revision FROM edge_commit_checkpoints ORDER BY id DESC LIMIT 1').fetchone()
        fallback=row[0] if row else None
    return fallback


class _Snapshot:
    def __init__(self,revision,db,deadline):self.revision,self.db,self.deadline=revision,db,deadline


@contextmanager
def assessment_snapshot(graph,maximum_seconds=3):
    """Share one bounded read-only index snapshot across a single UI request."""
    if not 0<maximum_seconds<=10:raise ValueError('EDGE_COMMIT_READ_TIME_BOUND')
    deadline=time.monotonic()+maximum_seconds;db=None
    if not hasattr(graph,'out'):
        yield _Snapshot(_checkpoint_revision(graph),None,deadline);return
    try:
        path=Path(graph.out)/'address-index.sqlite3'
        if not path.exists():
            yield _Snapshot(None,None,deadline);return
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.25);db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-4096')
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000);db.execute('BEGIN')
            revision=_revision(db)[0]
        except (sqlite3.Error,OSError,ValueError):
            if db is not None:db.close();db=None
            revision=None
        yield _Snapshot(revision,db,deadline)
    finally:
        if db is not None:db.close()


def read_current_revision(graph):
    """Revision-only convenience; use assessment_snapshot for many batches."""
    with assessment_snapshot(graph) as snapshot:return snapshot.revision


def _assess(edge,db):
    out=dict(edge=edge['id'],edge_body_sha256=_sha(edge['body']),status='legacy_unchecked',
        independent_consensus_verified=False,same_funds_continuity_proven=False)
    def finish(status,reason,**extra):return dict(out,status=status,instruction_commit_status=status,reason=reason,**extra)
    if not edge_requires_commit(edge):return finish('not_applicable','non_instruction_or_provider_evidence')
    try:body=json.loads(edge['body'])
    except (ValueError,TypeError):return finish('legacy_unchecked','malformed_edge_evidence')
    if not isinstance(body,dict):return finish('legacy_unchecked','malformed_edge_evidence')
    if body.get('dead_branch') or body.get('finality')=='dead_branch':return finish('rejected','dead_branch')
    ix=body.get('ix');slot=edge['slot'];signature=body.get('signature')
    if type(ix) is not int or ix<0:return finish('legacy_unchecked','missing_encoded_instruction')
    try:raw_signature=decode(signature)
    except (ValueError,TypeError,AttributeError):return finish('legacy_unchecked','missing_signature')
    if len(raw_signature)!=64 or type(slot) is not int:return finish('legacy_unchecked','missing_transaction_identity')
    params=[raw_signature,slot,slot];where='t.signature=? AND t.slot=? AND b.slot=?'
    block_id=body.get('index_block_id');source_hash=body.get('source_sha256')
    if block_id is not None:
        if type(block_id) is not int or block_id<1:return finish('rejected','invalid_block_identity')
        where+=' AND b.id=?';params.append(block_id)
    if source_hash is not None:
        if not isinstance(source_hash,str) or len(source_hash)!=64:return finish('rejected','invalid_source_hash')
        where+=' AND b.hash=?';params.append(source_hash)
    rows=db.execute('''SELECT t.*,b.hash,b.state,b.receipt FROM transactions t JOIN blocks b ON b.id=t.block
        WHERE '''+where+' LIMIT 3',params).fetchall()
    if len(rows)!=1:return finish('legacy_unchecked','transaction_missing_or_branch_ambiguous',matching_transactions=len(rows))
    tx=rows[0];out.update(index_block_id=tx['block'],index_transaction_id=tx['id'],source_sha256=tx['hash'],ix=ix)
    if tx['state']=='dead_branch':return finish('rejected','dead_branch')
    if not tx['successful']:return finish('rejected','transaction_failed')
    if body.get('tx_order') is not None and body['tx_order']!=tx['tx_order']:return finish('rejected','transaction_order_conflict')
    receipt=json.loads(tx['receipt'])
    signed=receipt.get('transaction_signatures_verified') is True
    if not signed and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commit_cache_audits'").fetchone():
        signed=db.execute('''SELECT 1 FROM commit_cache_audits c WHERE block=?
            AND json_extract(c.body,'$.transaction_signatures_reverified')=1
            AND EXISTS(SELECT 1 FROM json_each(c.body,'$.policy_transactions') p WHERE p.value=?) LIMIT 1''',
            (tx['block'],tx['id'])).fetchone() is not None
    if not signed:return finish('legacy_unchecked','raw_signature_proof_missing')
    policy=db.execute('SELECT body FROM tx_commit_evidence WHERE tx=? ORDER BY version DESC LIMIT 1',(tx['id'],)).fetchone()
    evidence=json.loads(policy[0]) if policy else None
    status=transfer_commit_status(ix,evidence)
    if status!='committed':return finish(status,'cpi_policy_excluded' if status=='rejected' else 'cpi_policy_missing')
    # Commitment of an unrelated instruction cannot validate this edge's fields.
    if edge['kind'] in TRANSFER_KINDS:
        transfer=db.execute('''SELECT f.*,s.key AS source_key,d.key AS destination_key FROM transfers f
            JOIN keys s ON s.id=f.source JOIN keys d ON d.id=f.destination WHERE f.tx=? AND f.ix=?''',(tx['id'],ix)).fetchone()
        if not transfer:return finish('legacy_unchecked','decoded_transfer_not_indexed')
        amount=body.get('amount_lamports') if edge['kind']=='sol_transfer' else body.get('amount')
        mint='SOL' if edge['kind']=='sol_transfer' else body.get('symbol')
        if (encode(transfer['source_key'])!=edge['source'] or encode(transfer['destination_key'])!=edge['target'] or
            str(amount)!=transfer['amount'] or mint!=transfer['mint']):return finish('rejected','transfer_field_conflict')
        out.update(transfer_kind=transfer['kind'],token_account_not_owner=transfer['mint']!='SOL')
    else:
        action=edge['kind'].removeprefix('later_').removeprefix('early_')
        activity=db.execute('''SELECT 1 FROM activity a JOIN keys k ON k.id=a.wallet
            WHERE a.tx=? AND a.ix=? AND a.action=? AND a.mint=? AND k.key=? LIMIT 1''',
            (tx['id'],ix,action,edge['target'],decode(edge['source']))).fetchone()
        if not activity:return finish('legacy_unchecked','decoded_activity_not_indexed')
    return finish('committed','successful_raw_instruction',transaction_success=True)


def assess_edges(graph,index,scope,limit=300):
    if type(limit) is not int or not 1<=limit<=1000:raise ValueError('EDGE_COMMIT_BATCH_BOUND')
    if graph.db.in_transaction:raise ValueError('EDGE_COMMIT_OPEN_TRANSACTION')
    scope.guard();db=getattr(index,'db',index);_schema(graph.db)
    revision,inputs=_revision(db)
    latest=graph.db.execute('SELECT * FROM edge_commit_passes ORDER BY id DESC LIMIT 1').fetchone()
    checkpoint=graph.db.execute('SELECT * FROM edge_commit_checkpoints WHERE pass_id=? ORDER BY id DESC LIMIT 1',
        (latest['id'],)).fetchone() if latest else None
    upper=graph.db.execute('SELECT coalesce(max(rowid),0) FROM edges').fetchone()[0]
    if latest is None or (checkpoint and checkpoint['done']):
        start=checkpoint['cursor'] if latest and latest['revision']==revision else 0
        if latest and latest['revision']==revision and start>=upper:
            return dict(assessed=0,revision=revision,pass_complete=True,cursor=start,status='current')
        with graph.db:
            pid=graph.db.execute('INSERT INTO edge_commit_passes(revision,start_cursor,upper_cursor,body) VALUES(?,?,?,?)',
                (revision,start,upper,canonical(inputs))).lastrowid
        latest=graph.db.execute('SELECT * FROM edge_commit_passes WHERE id=?',(pid,)).fetchone();checkpoint=None
    cursor=checkpoint['cursor'] if checkpoint else latest['start_cursor']
    rows=graph.db.execute('SELECT rowid AS edge_rowid,* FROM edges WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',
        (cursor,latest['upper_cursor'],limit)).fetchall()
    counts={};assessments=[]
    for row in rows:
        scope.guard();result=_assess(dict(row),db)
        result.update(assessment_revision=revision,assessment_current=True,pass_id=latest['id'])
        counts[result['status']]=counts.get(result['status'],0)+1
        assessments.append((row,result));cursor=row['edge_rowid']
    done=cursor>=latest['upper_cursor'] or len(rows)<limit
    scope.guard()
    with graph.db:
        for row,result in assessments:
            graph.db.execute('INSERT OR IGNORE INTO edge_commit_assessments(edge,revision,pass_id,edge_rowid,status,body) VALUES(?,?,?,?,?,?)',
                (row['id'],revision,latest['id'],row['edge_rowid'],result['status'],canonical(result)))
        graph.db.execute('INSERT INTO edge_commit_checkpoints(pass_id,cursor,revision,done,body) VALUES(?,?,?,?,?)',
            (latest['id'],cursor,revision,int(done),canonical(dict(counts=counts,index_inputs=inputs))))
    return dict(assessed=len(rows),revision=revision,pass_id=latest['id'],pass_complete=done,cursor=cursor,counts=counts,
        source_edges_unchanged=True,original_cursors_unchanged=True,full_history_verified=False)


def assess_new_edges(graph,index,scope,limit=64):
    """Admit new raw evidence without waiting behind a historical review pass.

    This lane only selects edges that have never had an assessment, newest
    first. It uses the same raw proof checks and appends to the same assessment
    ledger, but never creates or advances a historical pass/checkpoint.
    Previously unchecked/rejected edges remain the historical lane's work.
    """
    if type(limit) is not int or not 1<=limit<=64:raise ValueError('EDGE_COMMIT_PRIORITY_BOUND')
    if graph.db.in_transaction:raise ValueError('EDGE_COMMIT_OPEN_TRANSACTION')
    scope.guard();db=getattr(index,'db',index);_schema(graph.db)
    revision,_=_revision(db)
    kinds=sorted(RAW_EDGE_KINDS);marks=','.join('?' for _ in kinds)
    rows=graph.db.execute('''SELECT e.rowid AS edge_rowid,e.* FROM edges e
        WHERE e.kind IN ('''+marks+''') AND NOT EXISTS(
          SELECT 1 FROM edge_commit_assessments a WHERE a.edge=e.id)
        ORDER BY e.rowid DESC LIMIT ?''',(*kinds,limit)).fetchall()
    counts={};assessments=[]
    for row in rows:
        scope.guard();result=_assess(dict(row),db)
        result.update(assessment_revision=revision,assessment_current=True,pass_id=None,
                      assessment_lane='new_edge_priority')
        counts[result['status']]=counts.get(result['status'],0)+1
        assessments.append((row,result))
    scope.guard()
    with graph.db:
        for row,result in assessments:
            graph.db.execute('''INSERT OR IGNORE INTO edge_commit_assessments(
                edge,revision,pass_id,edge_rowid,status,body) VALUES(?,?,?,?,?,?)''',
                (row['id'],revision,None,row['edge_rowid'],result['status'],canonical(result)))
    return dict(assessed=len(rows),revision=revision,counts=counts,lane='new_edge_priority',
        edge_ids=[row['id'] for row in rows],historical_checkpoints_unchanged=True,
        source_edges_unchanged=True,original_cursors_unchanged=True,full_history_verified=False)


def latest_assessments(graph,edge_ids,require_current=True,*,revision=_REVISION_UNSET):
    ids=list(dict.fromkeys(edge_ids))
    if len(ids)>1000 or any(not isinstance(i,str) or len(i)>128 for i in ids):raise ValueError('EDGE_COMMIT_PROJECTION_BOUND')
    if not ids:return {}
    exists=graph.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_commit_assessments'").fetchone()
    with ExitStack() as contexts:
        snapshot=revision if isinstance(revision,_Snapshot) else None
        if snapshot is not None:revision=snapshot.revision
        elif revision is _REVISION_UNSET:
            if require_current:
                snapshot=contexts.enter_context(assessment_snapshot(graph));revision=snapshot.revision
            else:revision=_checkpoint_revision(graph)
        results={}
        for offset in range(0,len(ids),400):
            part=ids[offset:offset+400];marks=','.join('?' for _ in part)
            edges={r['id']:dict(r) for r in graph.db.execute('SELECT * FROM edges WHERE id IN ('+marks+')',part)}
            assessed={}
            if exists:
                for row in graph.db.execute('''SELECT a.id,a.edge,a.body FROM edge_commit_assessments a
                    JOIN (SELECT edge,max(id) id FROM edge_commit_assessments WHERE edge IN ('''+marks+''') GROUP BY edge) m ON m.id=a.id''',part):
                    assessed[row['edge']]=dict(json.loads(row['body']),assessment_id=row['id'])
            for ident,edge in edges.items():
                raw=edge_requires_commit(edge);value=assessed.get(ident)
                if not value:
                    status='legacy_unchecked' if raw else 'not_applicable'
                    value=dict(edge=ident,status=status,instruction_commit_status=status,reason='assessment_pending' if raw else 'non_instruction_or_provider_evidence',assessment_current=not raw)
                else:
                    value=dict(value);current=value.get('assessment_revision')==revision and value.get('edge_body_sha256')==_sha(edge['body'])
                    value['assessment_current']=current
                    if raw and require_current and not current:
                        fresh=None
                        if value['status']=='committed':
                            if snapshot is None:snapshot=contexts.enter_context(assessment_snapshot(graph))
                            if snapshot.db is not None and time.monotonic()<snapshot.deadline:
                                try:fresh=_assess(edge,snapshot.db)
                                except (sqlite3.Error,ValueError,TypeError,KeyError):pass
                        if fresh is not None:
                            fresh.update(assessment_revision=snapshot.revision,assessment_current=True,read_revalidated=True,
                                original_assessment_id=value['assessment_id'],original_assessment_revision=value['assessment_revision'])
                            value=fresh
                        else:
                            value.update(previous_status=value['status'],status='legacy_unchecked',instruction_commit_status='legacy_unchecked',reason='reassessment_pending')
                results[ident]=value
        return results
