"""Project retained graph evidence, not block or lifetime coverage.

One edge and its high-water receipt commit atomically. Reaching the retained
graph head says nothing about missing chain history. In-place changes at the
checkpoint fail closed; this append-only projection is not a retraction scanner.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import re

from .coverage import Coverage
from .store import address, canonical, decode


_RAW = {'sparse_archive', 'reconstructed_public_block', 'public_car', 'sparse_public_car'}
_TRANSFERS = {'sol_transfer', 'indexed_transfer', 'raw_archive_transfer'}
_ACTIVITY = {'create', 'launch_payer', 'early_buy', 'later_create', 'later_launch_payer', 'later_buy'}
_SHA = re.compile(r'^[0-9a-f]{64}$')
_REF = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


class _AtomicCoverage(Coverage):
    @contextmanager
    def _write(self):
        # Coverage's public operations participate in the enclosing per-edge
        # transaction, so neither endpoint nor the checkpoint can commit alone.
        if self.db.in_transaction:
            self._guard()
            yield
        else:
            with super()._write():
                yield


def close_retained(graph):
    """Release the research process's writer when its owning Graph closes.

    Read-only HTTP workers must not own this connection. Retaining the writer
    between bounded sync calls keeps SQLite's WAL and shared-memory sidecars
    available to readers whose filesystem namespace cannot create those files.
    """
    coverage = getattr(graph, '_coverage_projection_writer', None)
    if coverage is not None:
        graph._coverage_projection_writer = None
        coverage.close()


def _retained_writer(graph, scope):
    path = graph.out/'coverage.sqlite3'
    coverage = getattr(graph, '_coverage_projection_writer', None)
    if coverage is None:
        coverage = _AtomicCoverage(path, guard=scope.guard)
        graph._coverage_projection_writer = coverage
    else:
        if coverage.path.resolve() != path.resolve():
            raise ValueError('COVERAGE_PROJECTION_WRITER_PATH_CHANGED')
        coverage.guard = scope.guard
        coverage._guard()
    return coverage


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _edge_hash(row):
    # Calendar timestamp enrichment does not change the projected slot record.
    return _hash({k:row[k] for k in ('id','root','source','target','kind','slot','body','body_bytes')})


def _assessments(graph, edge_ids):
    from .commit_graph import latest_assessments
    return latest_assessments(graph,edge_ids,require_current=True)


def _classify(row, assessment=None):
    if row['body_bytes'] > 65536:
        return None, 'oversized_evidence'
    try:
        evidence = json.loads(row['body'])
    except (ValueError, TypeError):
        return None, 'invalid_evidence_json'
    if not isinstance(evidence, dict):
        return None, 'invalid_evidence_shape'
    if (any(evidence.get(k) for k in ('dead_branch','retracted','invalid_address','invalidated')) or
            evidence.get('finality') in ('dead_branch','retracted','failed') or
            evidence.get('success') is False or evidence.get('transaction_success') is False or
            evidence.get('failed') is True or evidence.get('err') is not None):
        return None, 'failed_dead_retracted_or_invalid'
    if not all(address(row[k]) for k in ('root','source','target')):
        return None, 'invalid_address'
    if type(row['slot']) is not int or not 0 <= row['slot'] < 2**63:
        return None, 'missing_or_invalid_slot'
    if row['kind'] not in _TRANSFERS | _ACTIVITY:
        return None, 'unsupported_relationship_kind'
    if evidence.get('instruction_commit_status') in ('rejected','legacy_unchecked'):
        return None, 'stored_instruction_commit_inadmissible'
    ix=evidence.get('ix')
    if type(ix) is not int or ix<0:
        return None, 'instruction_position_unverified'
    status=(assessment or {}).get('instruction_commit_status',(assessment or {}).get('status'))
    if status!='committed' or (assessment or {}).get('assessment_current') is not True:
        return None, 'instruction_commit_rejected' if status=='rejected' else 'instruction_commit_pending'
    signature = evidence.get('signature')
    try:
        valid_signature = isinstance(signature,str) and 64 <= len(signature) <= 88 and len(decode(signature)) == 64
    except ValueError:
        valid_signature = False
    if not valid_signature:
        return None, 'missing_or_invalid_signature'
    raw_proof = ((evidence.get('transaction_signature_verified') is True and evidence.get('raw_wallet_membership_verified') is True) or
        (assessment.get('transaction_success') is True and isinstance(assessment.get('source_sha256'),str)))
    if evidence.get('source') == 'ticking_retained' or evidence.get('finality') == 'filtered_processed_stream':
        tier = 'ticking_processed_observation'
    elif evidence.get('source') in _RAW or raw_proof:
        if evidence.get('transaction_signature_verified') is False:
            return None, 'raw_signature_not_verified'
        tier = 'retained_raw_transaction_record'
    else:
        return None, 'unclassified_or_provider_only'
    digest = evidence.get('source_sha256') or evidence.get('car_sha256') or assessment.get('source_sha256')
    old_ref = evidence.get('evidence')
    if isinstance(digest,str) and _SHA.fullmatch(digest):
        origin = 'object:'+digest
    elif tier == 'ticking_processed_observation' and isinstance(old_ref,str) and _REF.fullmatch(old_ref):
        origin = 'original_evidence:'+old_ref
    else:
        return None, 'missing_original_provenance'
    if assessment.get('transfer_kind') is not None:
        evidence=dict(evidence,transfer_kind=assessment['transfer_kind'],
            token_account_not_owner=assessment.get('token_account_not_owner',False))
    return dict(tier=tier,origin=origin,evidence=evidence,signature=signature), None


def _prepare(coverage, graph):
    path = graph.out/'graph.sqlite3'
    stat = path.stat()
    metadata = dict(schema='economic-machine.retained-coverage-projection.v1',
        graph_path=str(path.resolve()),device=stat.st_dev,inode=stat.st_ino,
        scope='append_only_retained_transaction_observations',
        unit='one_graph_edge_at_one_observed_slot',
        whole_slot_inspected=False,whole_block_inspected=False,
        signature_verification='reuse_retained_source_result_no_new_crypto_verification',
        independent_consensus_verified=False,complete_wallet_history=False,
        retractions_behind_checkpoint_reconciled=False)
    encoded = canonical(metadata);digest = _hash(metadata)
    with coverage._write():
        coverage.db.execute('CREATE TABLE IF NOT EXISTS coverage_projection_meta(id INTEGER PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL)')
        coverage.db.execute('CREATE TABLE IF NOT EXISTS coverage_projection_cursor(id INTEGER PRIMARY KEY,last_rowid INTEGER NOT NULL,last_edge TEXT,last_hash TEXT)')
        coverage.db.execute('CREATE TABLE IF NOT EXISTS coverage_projection_edges(graph_rowid INTEGER PRIMARY KEY,edge_id TEXT NOT NULL,edge_hash TEXT NOT NULL,disposition TEXT NOT NULL,body TEXT NOT NULL)')
        coverage.db.execute('CREATE TABLE IF NOT EXISTS coverage_projection_review(id INTEGER PRIMARY KEY,last_assessment_rowid INTEGER NOT NULL)')
        coverage.db.execute('CREATE TABLE IF NOT EXISTS coverage_projection_versions(edge TEXT NOT NULL,assessment_hash TEXT NOT NULL,edge_hash TEXT NOT NULL,disposition TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(edge,assessment_hash,edge_hash))')
        old = coverage.db.execute('SELECT body FROM coverage_projection_meta WHERE id=1').fetchone()
        if old and old['body'] != encoded:
            raise ValueError('COVERAGE_PROJECTION_CORPUS_CHANGED')
        coverage.db.execute('INSERT OR IGNORE INTO coverage_projection_meta VALUES(1,?,?)', (encoded,digest))
        coverage.db.execute('INSERT OR IGNORE INTO coverage_projection_cursor VALUES(1,0,NULL,NULL)')
        coverage.db.execute('INSERT OR IGNORE INTO coverage_projection_review VALUES(1,0)')
    return digest


def _project(coverage, row, classified, corpus_hash):
    slot = row['slot'];tier = classified['tier']
    source = 'retained:'+str(slot)+':'+_hash(dict(tier=tier,origin=classified['origin']))[:32]
    coverage.register_source(source, kind=tier, start_slot=slot, end_slot=slot,
        evidence_refs=['coverage_projection_meta:'+corpus_hash, classified['origin']])
    refs = ['graph_edge:'+row['id'], classified['origin']]
    method = 'retained_single_transaction_reference' if tier == 'retained_raw_transaction_record' else 'retained_single_processed_observation'
    participants = [row['source']]
    if row['kind'] in _TRANSFERS and row['target'] != row['source']:
        participants.append(row['target'])
    for wallet in participants:
        frontier = coverage.register_wallet(wallet, source)
        coverage.record_span(wallet, source, 'inspected', slot, slot, method=method, evidence_refs=refs,
            reason='Only the referenced transaction observation was inspected; the rest of this slot and block is unknown.')
        if row['kind'] in _TRANSFERS:
            direction = 'self' if row['source'] == row['target'] else 'outgoing' if wallet == row['source'] else 'incoming'
            counterparty = row['target'] if wallet == row['source'] else row['source']
            relation = 'account_creation_funding' if classified['evidence'].get('transfer_kind') in ('create_account','create_account_with_seed','account_creation_funding') else 'transfer'
            coverage.record_link(wallet, source, counterparty, direction, slot, evidence_refs=refs, relation=relation)
        # Activity targets are token identifiers; do not invent a recipient
        # wallet's history or turn a buy/create instruction into a funds link.
        previous=frontier['cursor']
        if not isinstance(previous,dict) or previous.get('graph_rowid',-1)<row['graph_rowid']:
            coverage.advance(wallet, source, cursor=dict(graph_rowid=row['graph_rowid'],graph_edge=row['id']),
                state='scanning',expected_revision=frontier['revision'],evidence_refs=refs,latest_observed_slot=slot,
                reason='Retained observation appended; no complete source, slot or wallet-history claim.')
    return dict(source=source,tier=tier,participants=participants,graph_edge=row['id'],
        whole_slot_inspected=False,whole_block_inspected=False,same_funds_continuity_proven=False)


_SELECT = '''SELECT rowid AS graph_rowid,id,root,source,target,kind,slot,
    substr(body,1,65537) AS body,length(CAST(body AS BLOB)) AS body_bytes FROM edges'''


def _project_version(coverage,row,assessment,corpus_hash):
    fingerprint=_hash(assessment or {})
    edge_hash=_edge_hash(row)
    old=coverage.db.execute('SELECT disposition,body FROM coverage_projection_versions WHERE edge=? AND assessment_hash=? AND edge_hash=?',
        (row['id'],fingerprint,edge_hash)).fetchone()
    classified,reason=_classify(row,assessment)
    if old:
        return old['disposition'],json.loads(old['body']),classified,reason
    receipt=_project(coverage,row,classified,corpus_hash) if classified else dict(reason=reason,graph_edge=row['id'])
    disposition='projected' if classified else 'skipped'
    coverage.db.execute('INSERT INTO coverage_projection_versions VALUES(?,?,?,?,?)',
        (row['id'],fingerprint,edge_hash,disposition,canonical(receipt)))
    return disposition,receipt,classified,reason


def sync_retained(graph, scope, limit=300):
    """Bounded incremental projection; graph/frontier/model state stays untouched."""
    if type(limit) is not int or not 1 <= limit <= 300:
        raise ValueError('COVERAGE_PROJECTION_LIMIT')
    if graph.db.in_transaction:
        raise ValueError('COVERAGE_PROJECTION_GRAPH_TRANSACTION_ACTIVE')
    scope.guard()
    coverage = _retained_writer(graph, scope)
    try:
        corpus_hash = _prepare(coverage,graph)
        checkpoint = coverage.db.execute('SELECT * FROM coverage_projection_cursor WHERE id=1').fetchone()
        last = checkpoint['last_rowid']
        if last:
            previous = graph.db.execute(_SELECT+' WHERE rowid=?',(last,)).fetchone()
            if previous is None or previous['id'] != checkpoint['last_edge'] or _edge_hash(previous) != checkpoint['last_hash']:
                raise ValueError('COVERAGE_PROJECTION_CHECKPOINT_CHANGED')
        has_assessments=graph.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_commit_assessments'").fetchone() is not None
        review_start=coverage.db.execute('SELECT last_assessment_rowid FROM coverage_projection_review WHERE id=1').fetchone()[0]
        review_pending=bool(has_assessments and graph.db.execute('SELECT 1 FROM edge_commit_assessments WHERE rowid>? LIMIT 1',(review_start,)).fetchone())
        forward_limit=max(1,limit//2) if review_pending and limit>1 else limit
        rows = graph.db.execute(_SELECT+' WHERE rowid>? ORDER BY rowid LIMIT ?', (last,forward_limit)).fetchall()
        assessments=_assessments(graph,[row['id'] for row in rows]) if rows else {}
        projected = 0;skipped = Counter();tiers = Counter()
        for row in rows:
            scope.guard()
            edge_hash = _edge_hash(row)
            with coverage._write():
                current = coverage.db.execute('SELECT last_rowid FROM coverage_projection_cursor WHERE id=1').fetchone()[0]
                if current != last:
                    raise ValueError('COVERAGE_PROJECTION_CONCURRENT_ADVANCE')
                disposition,receipt,classified,reason=_project_version(coverage,row,assessments.get(row['id']),corpus_hash)
                coverage.db.execute('INSERT INTO coverage_projection_edges VALUES(?,?,?,?,?)',
                    (row['graph_rowid'],row['id'],edge_hash,disposition,canonical(receipt)))
                coverage.db.execute('UPDATE coverage_projection_cursor SET last_rowid=?,last_edge=?,last_hash=? WHERE id=1',
                    (row['graph_rowid'],row['id'],edge_hash))
            last = row['graph_rowid']
            if classified:
                projected += 1;tiers[classified['tier']] += 1
            else:
                skipped[reason] += 1
        reviewed=review_projected=0
        remaining=limit-len(rows)
        review_rows=graph.db.execute('SELECT rowid AS assessment_rowid,edge FROM edge_commit_assessments WHERE rowid>? ORDER BY rowid LIMIT ?',
            (review_start,remaining)).fetchall() if has_assessments and remaining else []
        review_assessments=_assessments(graph,[row['edge'] for row in review_rows]) if review_rows else {}
        for event in review_rows:
            row=graph.db.execute(_SELECT+' WHERE id=?',(event['edge'],)).fetchone()
            with coverage._write():
                current=coverage.db.execute('SELECT last_assessment_rowid FROM coverage_projection_review WHERE id=1').fetchone()[0]
                if current!=review_start:raise ValueError('COVERAGE_PROJECTION_CONCURRENT_REVIEW')
                if row is not None:
                    disposition,receipt,classified,reason=_project_version(coverage,row,review_assessments.get(row['id']),corpus_hash)
                    review_projected+=classified is not None
                coverage.db.execute('UPDATE coverage_projection_review SET last_assessment_rowid=? WHERE id=1',(event['assessment_rowid'],))
            review_start=event['assessment_rowid'];reviewed+=1
        more = graph.db.execute('SELECT 1 FROM edges WHERE rowid>? LIMIT 1',(last,)).fetchone() is not None
        review_more=bool(has_assessments and graph.db.execute('SELECT 1 FROM edge_commit_assessments WHERE rowid>? LIMIT 1',(review_start,)).fetchone())
        return dict(schema='economic-machine.retained-coverage-sync.v1',scanned=len(rows),projected=projected,
            skipped=sum(skipped.values()),skip_reasons=dict(skipped),tiers=dict(tiers),last_graph_rowid=last,
            reviewed_assessments=reviewed,review_projected=review_projected,last_assessment_rowid=review_start,
            assessment_review_pending=review_more,
            caught_up_to_retained_edges=not more,complete_wallet_history=False,whole_block_coverage=False,
            model_calls=0,network_calls=0,live_frontier_modified=False,
            retractions_behind_checkpoint_reconciled=False)
    finally:
        # Keep the writer alive, but never retain a failed/open transaction
        # between research batches or hold a write lock while the model runs.
        if coverage.db.in_transaction:
            coverage.db.rollback()


def report_retained(graph,wallet,limit=64):
    """Read-through admissibility view over immutable historical receipts.

    Old receipts are retained. A changed/dead/retracted source or a stale commit
    assessment cannot remain a current movement or latest wallet observation.
    Only displayed evidence and bounded latest-slot candidates are checked.
    """
    coverage=Coverage(graph.out/'coverage.sqlite3',read_only=True)
    try:
        report=coverage.report(wallet,limit=limit)
        latest=coverage.db.execute('SELECT source,latest_slot,latest_refs FROM coverage_frontiers WHERE wallet=? AND latest_slot IS NOT NULL ORDER BY latest_slot DESC,source LIMIT ?',
            (wallet,limit+1)).fetchall()
        refs=set()
        def collect(values):
            for ref in values:
                if isinstance(ref,str) and ref.startswith('graph_edge:'):refs.add(ref[11:])
        for source in report['sources']:collect(source['latest_observation_refs'])
        for rows in report['spans'].values():
            for row in rows:collect(row['evidence_refs'])
        for rows in report['trace_evidence'].values():
            for row in rows:collect(row['evidence_refs'])
        for row in latest[:limit]:collect(json.loads(row['latest_refs']))
        rows={};assessments={};ordered=sorted(refs)
        for start in range(0,len(ordered),256):
            part=ordered[start:start+256]
            for row in graph.db.execute(_SELECT+' WHERE id IN ('+','.join('?' for _ in part)+')',part):rows[row['id']]=row
            assessments.update(_assessments(graph,part))
        classified={ident:_classify(row,assessments.get(ident))[0] for ident,row in rows.items()}
        def valid_refs(values,slot,link=None):
            accepted=[]
            for ref in values:
                if not isinstance(ref,str) or not ref.startswith('graph_edge:'):continue
                ident=ref[11:];row=rows.get(ident)
                if row is None or classified.get(ident) is None or row['slot']!=slot:continue
                participants=(row['source'],row['target']) if row['kind'] in _TRANSFERS else (row['source'],)
                if wallet not in participants:continue
                if link is not None:
                    if row['kind'] not in _TRANSFERS:continue
                    direction='self' if row['source']==row['target'] else 'outgoing' if wallet==row['source'] else 'incoming'
                    counterparty=row['target'] if wallet==row['source'] else row['source']
                    if direction!=link['direction'] or counterparty!=link['counterparty']:continue
                    expected_relation='account_creation_funding' if classified[ident]['evidence'].get('transfer_kind') in ('create_account','create_account_with_seed','account_creation_funding') else 'transfer'
                    if link['relation']!=expected_relation:continue
                accepted.append(ref)
            return sorted(set(accepted))
        omitted=0
        for direction,links in report['trace_evidence'].items():
            kept=[]
            for link in links:
                checked=valid_refs(link['evidence_refs'],link['slot'],link)
                if checked:kept.append(dict(link,current_evidence_checked=True,current_graph_refs=checked))
                else:omitted+=1
            report['trace_evidence'][direction]=kept
        kept=[]
        for span in report['spans']['inspected']:
            if not any(ref.startswith('graph_edge:') for ref in span['evidence_refs']):
                kept.append(span);continue
            checked=valid_refs(span['evidence_refs'],span['start_slot']) if span['start_slot']==span['end_slot'] else []
            if checked:kept.append(dict(span,current_evidence_checked=True,current_graph_refs=checked))
            else:omitted+=1
        report['spans']['inspected']=kept
        for source in report['sources']:
            checked=valid_refs(source['latest_observation_refs'],source['latest_observed_slot'])
            source['current_observation_checked']=bool(checked)
            source['latest_observation_refs']=checked
            if not checked:source['latest_observed_slot']=None
        report.update(latest_observed_slot=None,latest_observation_source=None,latest_observation_refs=[])
        for row in latest[:limit]:
            checked=valid_refs(json.loads(row['latest_refs']),row['latest_slot'])
            if checked:
                report.update(latest_observed_slot=row['latest_slot'],latest_observation_source=row['source'],latest_observation_refs=checked)
                break
        report['current_evidence_view']=dict(checked_graph_references=len(refs),omitted_historical_records=omitted,
            latest_candidate_list_truncated=len(latest)>limit,whole_ledger_reconciled=False,
            historical_receipts_preserved=True,requires_current_commit_assessment=True,
            retraction_or_commit_review_pending=omitted>0 or any(classified.get(ident) is None for ident in refs),
            scope='Displayed records and bounded latest-slot candidates only.')
        report['limitations'].append('Historical projection receipts are retained; movement and latest-slot fields are filtered against current graph flags and commit assessments on this read.')
        return report
    finally:
        coverage.close()
