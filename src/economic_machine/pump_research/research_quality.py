"""Reproducible evidence audit. Model hypotheses are not audit observations."""
from collections import Counter,defaultdict
from contextlib import closing
import hashlib
import json
import sqlite3
import time
from .store import canonical


def tier(e):
    b=e['evidence'];source=b.get('source','')
    if e['kind']=='provider_creator_claim':return 'provider_claim'
    if source.startswith('core_') or 'dataset' in e['kind'] or e['kind']=='funding_record':return 'external_dataset'
    if source=='ticking_retained' or b.get('finality')=='filtered_processed_stream':return 'processed_stream'
    if source in ('reconstructed_public_block','sparse_archive') or b.get('finality')=='archive_content_verified':return 'archive_content'
    return 'unclassified'


def event_key(e):
    b=e['evidence'];kind='transfer' if e['kind'] in ('sol_transfer','indexed_transfer','raw_archive_transfer') else e['kind']
    if b.get('source_event_identity'):return ('verified_source_event_group',b['source_event_identity'])
    if b.get('signature'):
        return ('signature',kind,b['signature'],e['slot'],b.get('ix'),e['source'],e['target'])
    if b.get('source_rowid') is not None:return ('dataset_row',kind,b.get('source'),b['source_rowid'],e['source'],e['target'])
    return ('observation',e['id'])


def annotate_source_identity(e,identities):
    b=e['evidence']
    if b.get('role_correction')=='launch_payer' and e['kind']=='later_create':e['kind']='later_launch_payer'
    if b.get('source')=='core_pump_event_default':
        identity=identities.get(str(b.get('source_rowid')))
        if identity:
            b.update(source_event_identity=identity['event_identity'],dataset_original_signature=identity['original_signature'],
                     parse_version=identity['parse_version'],source_pair_verified=True,independently_chain_verified=False)
    return e


def audit_root(root,rows,identities=None):
    unique={};raw=0;invalid=0
    for r in rows:
        raw+=1;e=dict(r);e['evidence']=json.loads(e.pop('body'));annotate_source_identity(e,identities or {})
        if e['evidence'].get('dead_branch') or e['evidence'].get('invalid_address'):invalid+=1;continue
        k=event_key(e);old=unique.get(k)
        if old is None or e['evidence'].get('precision_version',0)>old['evidence'].get('precision_version',0):unique[k]=e
    values=list(unique.values());sources=Counter(tier(e) for e in values)
    initial={e['source'] for e in values if e['kind'] in ('early_buy','create','launch_payer')}
    observed_early={e['source'] for e in values if e['kind']=='early_buy'}
    sampled_early={e['source'] for e in values if e['kind']=='dataset_early_buy'}
    launches=[e for e in values if e['kind'] in ('create','launch_payer')]
    motifs=defaultdict(lambda:dict(events=0,mints=set(),examples=[]));owners=Counter();violations=[]
    hashes=[]
    for e in values:
        b=e['evidence'];hashes.append(hashlib.sha256(canonical(e).encode()).hexdigest())
        a=b.get('anchor')
        if a and b.get('ordering')=='recorded_instruction_order' and (e['slot'],b['tx_order'],b['ix'])<=tuple(a):violations.append(e['id'])
        for side in ('source','destination'):
            if side+'_owner_evidence' in b:owners[b[side+'_owner_evidence']['status']]+=1
        if e['source'] in initial and e['target']!=root['mint'] and e['kind'] in ('later_create','later_launch_payer','later_buy','later_dataset_create','later_dataset_buy'):
            m=motifs[(e['source'],tier(e),e['kind'])];m['events']+=1;m['mints'].add(e['target'])
            if len(m['examples'])<3:m['examples'].append(dict(mint=e['target'],slot=e['slot'],stamp=e['stamp'],signature=b.get('signature'),source_rowid=b.get('source_rowid')))
    return dict(mint=root['mint'],symbol=root.get('symbol'),provider_rank=root.get('provider_rank'),
        raw_observations=raw,excluded_dead_or_invalid=invalid,distinct_observation_records=len(values),
        repeated_event_observations=raw-invalid-len(values),evidence_tiers=dict(sources),
        observed_launch_events=len(launches),creator_field_observed=any(e['kind']=='create' for e in launches),
        launch_payer_only=bool(launches) and not any(e['kind']=='create' for e in launches),
        observed_early_buyers=len(observed_early),sampled_first_seen_buyers=len(sampled_early),
        known_date_events=sum(e['stamp'] is not None for e in values),undated_events=sum(e['stamp'] is None for e in values),
        token_owner_evidence=dict(owners),precision_order_violations=violations,
        same_slot_ordered_events=sum(bool(e['evidence'].get('anchor')) and e['slot']==e['evidence']['anchor'][0] and e['evidence'].get('ordering')=='recorded_instruction_order' for e in values),
        direct_participant_later_activity=[dict(wallet=k[0],source_tier=k[1],kind=k[2],events=v['events'],distinct_later_mints=len(v['mints']),examples=v['examples'])
            for k,v in sorted(motifs.items(),key=lambda kv:(-len(kv[1]['mints']),kv[0]))[:20]],
        complete_launch_window=False,complete_wallet_history=False,independent_finality_verified=False,
        same_funds_continuity_proven=False,bundle_membership_proven=False,
        evidence_digest=hashlib.sha256(''.join(sorted(hashes)).encode()).hexdigest())


def audit(base,guard=lambda:None):
    out=base/'evidence/wallet-map-v1';report=dict(schema='economic-machine.wallet-research-quality.v1',at_ms=time.time_ns()//1000000)
    identity_path=out/'source-identities.json'
    identity=json.loads(identity_path.read_bytes()) if identity_path.exists() else {}
    report['source_parser_audit']={k:v for k,v in identity.items() if k not in ('identities','selected_original_records')}
    with closing(sqlite3.connect(f'file:{out}/graph.sqlite3?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        report['roots']=[]
        for r in db.execute('SELECT * FROM roots ORDER BY rowid').fetchall():
            guard();root=json.loads(r['body'])
            report['roots'].append(audit_root(root,db.execute('SELECT * FROM edges WHERE root=?',(r['mint'],)),identity.get('identities',{})))
        report['frontier']=dict(db.execute('SELECT state,count(*) FROM frontier GROUP BY state'))
        report['anchored_jobs']=db.execute('SELECT count(*) FROM frontier_anchors').fetchone()[0]
        report['migration_preserved_cursors']=db.execute('SELECT count(*) FROM precision_cursor_history').fetchone()[0]
    report.update(orders_submitted=0,ranking_verified=False,alpha_proven=False,
        selection_bias='Ten provider-ATH candidates are a survivor-selected exploratory cohort, not a trading backtest.',
        validation_required=['Launch-to-present address coverage with explicit missing ranges.',
            'Unselected and failed-launch controls matched on launch date and initial liquidity.',
            'Freeze signals before future evaluation; include fees, priority fees, slippage, failed fills and exits.',
            'Separate repeated public-account behavior from owner identity, common funder hubs and bundle claims.'])
    report['totals']={k:sum(r[k] for r in report['roots']) for k in ('raw_observations','distinct_observation_records','repeated_event_observations','observed_early_buyers','same_slot_ordered_events')}
    report['totals']['roots_with_observed_launch']=sum(r['observed_launch_events']>0 for r in report['roots'])
    report['totals']['order_violations']=sum(len(r['precision_order_violations']) for r in report['roots'])
    return report
