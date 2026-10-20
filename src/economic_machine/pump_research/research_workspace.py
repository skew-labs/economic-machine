"""Private investigation queue and evidence-derived reports, no trade authority."""
import hashlib
import json
import os
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from .store import address,canonical


_TRANSFER_KINDS={'sol_transfer','indexed_transfer','raw_archive_transfer','funding_record'}
_ACTIVITY_ROLES={'create':'creator','later_create':'creator',
    'launch_payer':'launch_payer','later_launch_payer':'launch_payer',
    'early_buy':'buyer','later_buy':'buyer'}
_CLAIM_ROLES={'dataset_create':'provider_creator_claim','later_dataset_create':'provider_creator_claim',
    'provider_creator_claim':'provider_creator_claim','dataset_early_buy':'sampled_buy_claim',
    'later_dataset_buy':'sampled_buy_claim'}


def _acquisition_summary(base,now_ms):
    """Read one bounded progress receipt, without opening archive/index files."""
    result=dict(available=False,read_only=True,complete_solana_history=False,
        independent_consensus_verified=False,counts_source='acquisition_status_receipt')
    path=Path(base)/'evidence/address-epochs-v2/status.json'
    try:
        if path.is_symlink() or not path.is_file():return dict(result,reason='status_unavailable')
        with path.open('rb') as source:raw=source.read(65537)
        if len(raw)>65536:return dict(result,reason='status_bound')
        value=json.loads(raw)
    except (OSError,ValueError):return dict(result,reason='status_unavailable')
    if not isinstance(value,dict) or value.get('schema')!='economic-machine.epoch-acquisition-status.v2':
        return dict(result,reason='status_schema')
    def number(v,maximum=100001):
        return v if type(v) is int and 0<=v<=maximum else None
    states=value.get('states')
    if not isinstance(states,dict) or len(states)>64:return dict(result,reason='status_schema')
    imported=number(value.get('imported_epochs'));legacy=number(value.get('legacy_imports_reused'))
    # Missing or inconsistent counters remain unknown, never invented zeroes.
    if imported!=number(states.get('IMPORTED',0)):imported=None
    if legacy!=number(states.get('LEGACY_IMPORT_REUSED',0)):legacy=None
    pending=value.get('pending_epochs')
    pending_count=len(pending) if isinstance(pending,list) and all(number(v,100000) is not None for v in pending) else None
    updated=number(value.get('at_ms'),10**15)
    known_states={'DOWNLOADING','SOURCE_BACKOFF','DOWNLOADED_PENDING_INSPECTION',
        'INSPECTING_COMPLETE_ARCHIVE','INSPECTED','EXTRACTING_VERIFIED_MEMBERS','EXTRACTED_WAITING_TARGET_QUERY'}
    active=value.get('active_epochs');current=None
    for row in active[:8] if isinstance(active,list) else []:
        if (not isinstance(row,dict) or number(row.get('epoch'),100000) is None
            or not isinstance(row.get('state'),str) or row['state'] not in known_states):continue
        current=dict(epoch=row['epoch'],state=row['state'],searchable=False)
        received=number(row.get('received_bytes'),2**53-1);total=number(row.get('total_bytes'),2**53-1)
        if received is not None and total is not None and total>0 and received<=total:
            current.update(received_bytes=received,total_bytes=total)
        compressed=number(row.get('compressed_bytes'),2**53-1)
        if compressed is not None:current['compressed_bytes']=compressed
        break
    return dict(result,available=True,updated_ms=updated,
        stale=updated is None or updated>now_ms+60000 or now_ms-updated>900000,
        ready_index_count=imported+legacy if imported is not None and legacy is not None else None,
        new_ready_index_count=imported,legacy_ready_index_count=legacy,
        queued_epoch_count=number(states.get('QUEUED',0)),pending_epoch_count=pending_count,
        catalogued_epoch_count=number(value.get('catalogued_epochs')),
        current=current,needs_attention=bool(value.get('pending_reason')),
        downloaded_is_searchable=False)


def _exact_amount(edge):
    """Return the unit actually supplied by this source, never a valuation."""
    ev=edge['evidence'];kind=edge['kind']
    field='amount_lamports' if kind=='sol_transfer' else 'amount'
    value=ev.get(field)
    if isinstance(value,bool) or not isinstance(value,(str,int)):return None
    text=str(value)
    if len(text)>128:return None
    try:
        amount=Decimal(text)
        if not amount.is_finite() or amount<0 or abs(amount.as_tuple().exponent)>128:return None
    except (InvalidOperation,ValueError):return None
    asset='SOL' if kind=='sol_transfer' else ev.get('symbol')
    if not isinstance(asset,str) or not asset or len(asset)>100:return None
    if kind=='funding_record':
        unit='provider_reported_'+asset+'_units'
    else:
        if amount!=amount.to_integral_value():return None
        unit='lamports' if asset=='SOL' else 'token_base_units'
    return dict(amount=amount,asset=asset,unit=unit,source_field=field)


def _instruction_key(edge):
    ev=edge['evidence'];ix=ev.get('ix')
    if ev.get('signature') and isinstance(edge.get('slot'),int) and ix is not None:
        return ('instruction',ev['signature'],edge['slot'],canonical(ix),edge['source'],edge['target'])
    # Unsigned or unpositioned observations must not become unique trades.
    return ('observation',edge['id'])


def _transfer_class(edge):
    kind=edge['evidence'].get('transfer_kind')
    if kind in ('create_account','create_account_with_seed','account_creation_funding'):return 'account_creation_funding'
    if kind in ('system_transfer','system_transfer_with_seed','spl_transfer','spl_transfer_checked',
        'spl_token_account_transfer','spl_token_account_transfer_checked') or edge['kind']=='sol_transfer':return 'transfer_instruction'
    return 'instruction_kind_unspecified'


def _failed_record(edge):
    ev=edge['evidence']
    return ev.get('success') is False or ev.get('transaction_success') is False or ev.get('failed') is True or ev.get('err') is not None


def _current_edge_view(graph, retained):
    """Read-only admissibility projection. Never rewrite the retained evidence."""
    from .commit_graph import edge_requires_commit, latest_assessments
    assessments=latest_assessments(graph,[row['id'] for row in retained],require_current=True)
    edges=[];reasons=Counter();statuses=Counter();examples=[]
    raw_considered=committed=quarantined=failed=failed_transfers=0
    def exclude(row,reason,assessment,raw=False):
        nonlocal quarantined
        reasons[reason]+=1
        if raw:
            quarantined+=1
            statuses[assessment.get('status','legacy_unchecked')]+=1
        if len(examples)<12:
            examples.append(dict(record_id=row['id'],kind=row['kind'],reason=reason,
                assessment_status=assessment.get('status','legacy_unchecked'),
                assessment_current=assessment.get('assessment_current') is True))
    for row in retained:
        assessment=assessments.get(row['id'],{})
        try:ev=json.loads(row['body'])
        except (ValueError,TypeError):ev=None
        if not isinstance(ev,dict):
            raw=edge_requires_commit(row);raw_considered+=raw
            exclude(row,'invalid_evidence',assessment,raw);continue
        raw_source=ev.get('source') in ('sparse_archive','reconstructed_public_block','public_car','sparse_public_car')
        raw_proof=ev.get('transaction_signature_verified') is True and ev.get('raw_wallet_membership_verified') is True
        requires_commit=edge_requires_commit(row) or raw_source or raw_proof
        raw_considered+=requires_commit
        if (any(ev.get(k) for k in ('dead_branch','invalid_address','retracted','invalidated')) or
                ev.get('finality') in ('dead_branch','retracted')):
            exclude(row,'dead_retracted_or_invalid',assessment,requires_commit);continue
        if _failed_record(dict(evidence=ev)) or ev.get('finality')=='failed':
            failed+=1;failed_transfers+=row['kind'] in _TRANSFER_KINDS
            exclude(row,'failed_record',assessment,requires_commit);continue
        if ev.get('transaction_signature_verified') is False:
            exclude(row,'raw_signature_not_verified',assessment,requires_commit);continue
        if requires_commit:
            status=assessment.get('instruction_commit_status',assessment.get('status'))
            if status!='committed' or assessment.get('assessment_current') is not True:
                exclude(row,assessment.get('reason','assessment_pending'),assessment,True);continue
            # An explicit rejection in the original evidence is not silently
            # cleared by a differently scoped assessment.
            if ev.get('instruction_commit_status') in ('rejected','legacy_unchecked'):
                exclude(row,'stored_instruction_commit_inadmissible',assessment,True);continue
            committed+=1
            ev=dict(ev,instruction_commit_status='committed',instruction_commit_assessment_current=True,
                instruction_commit_assessment_revision=assessment.get('assessment_revision'))
            if assessment.get('transfer_kind') is not None:
                ev.update(transfer_kind=assessment['transfer_kind'],
                    token_account_not_owner=assessment.get('token_account_not_owner',False))
        processed=ev.get('source')=='ticking_retained' or ev.get('finality')=='filtered_processed_stream'
        tier='raw_transaction_record' if not processed and ev.get('signature') and (raw_source or raw_proof or
            (requires_commit and assessment.get('transaction_success') is True and assessment.get('source_sha256'))) else 'provider_or_local_observation'
        edges.append(dict(id=row['id'],root=row['root'],source=row['source'],target=row['target'],kind=row['kind'],
            slot=row['slot'],stamp=row['stamp'],evidence=ev,evidence_tier=tier))
    return edges,dict(checked_retained_records=len(retained),raw_instruction_records_considered=raw_considered,
        current_committed_records=committed,raw_records_quarantined=quarantined,
        excluded_records=sum(reasons.values()),failed_records_excluded=failed,
        failed_transfer_records_excluded=failed_transfers,reasons=dict(sorted(reasons.items())),
        assessment_statuses=dict(sorted(statuses.items())),examples=examples,
        source_records_preserved=True,requires_current_commit_assessment=True,
        excluded_from_graph_and_totals=True,whole_ledger_reconciled=False)


def _report_details(wallet,edges,truncated):
    """Bounded deterministic analysis of retained records, not a wallet ledger."""
    tiers=Counter(e['evidence_tier'] for e in edges)
    known=[e['stamp'] for e in edges if isinstance(e.get('stamp'),int) and not isinstance(e['stamp'],bool) and 0<e['stamp']<=253402300799999 and not e['evidence'].get('timestamp_unknown')]
    slots=[e['slot'] for e in edges if isinstance(e.get('slot'),int) and not isinstance(e['slot'],bool) and e['slot']>=0]
    def date(value):return datetime.fromtimestamp(value/1000,timezone.utc).isoformat(timespec='milliseconds')
    period=dict(known_timestamp_records=len(known),unknown_timestamp_records=len(edges)-len(known),
        first_known_ms=min(known) if known else None,last_known_ms=max(known) if known else None,
        first_known_utc=date(min(known)) if known else None,last_known_utc=date(max(known)) if known else None,
        first_observed_slot=min(slots) if slots else None,last_observed_slot=max(slots) if slots else None,
        full_interval_covered=False)
    groups=defaultdict(list);related=0;failed=0
    for edge in edges:
        if edge['kind'] not in _TRANSFER_KINDS:continue
        if _failed_record(edge):
            failed+=1;continue
        if wallet not in (edge['source'],edge['target']):related+=1;continue
        groups[_instruction_key(edge)].append(edge)
    totals={};directions=Counter();conflicts=[];unknown=0;duplicates=0;unsigned=0;account_funding=0
    for identity,observations in groups.items():
        observations=sorted(observations,key=lambda e:(e['evidence_tier']!='raw_transaction_record',e['id']))
        edge=observations[0];duplicates+=len(observations)-1;unsigned+=identity[0]=='observation'
        direction='self' if edge['source']==wallet==edge['target'] else 'outgoing' if edge['source']==wallet else 'incoming'
        counterparty=edge['target'] if direction=='outgoing' else edge['source']
        directions[direction]+=1
        classes={_transfer_class(e) for e in observations}-{'instruction_kind_unspecified'}
        transfer_class=next(iter(classes)) if len(classes)==1 else 'instruction_kind_unspecified'
        account_funding+=transfer_class=='account_creation_funding'
        quantities=[(e,_exact_amount(e)) for e in observations]
        variants={(a['asset'],a['unit'],a['amount']) for _,a in quantities if a is not None}
        if len(variants)>1 or len(classes)>1:
            conflicts.append(dict(source=edge['source'],target=edge['target'],signature=edge['evidence'].get('signature'),
                slot=edge['slot'],record_ids=[e['id'] for e in observations[:8]],reason='Conflicting quantity, unit or instruction classification; omitted from totals.'))
            continue
        usable=next(((e,a) for e,a in quantities if a is not None),None)
        if usable is None:unknown+=1;continue
        evidence,amount=usable;tier=evidence['evidence_tier']
        key=(direction,counterparty,amount['asset'],amount['unit'],transfer_class,tier)
        if key not in totals:
            totals[key]=dict(direction=direction,counterparty=counterparty,asset=amount['asset'],unit=amount['unit'],
                instruction_class=transfer_class,evidence_tier=tier,amount=Decimal(0),evidence_items=0,
                signed_instruction_items=0,source_fields=set(),signatures=[],record_ids=[])
        item=totals[key]
        with localcontext() as context:
            context.prec=512;item['amount']+=amount['amount']
        item['evidence_items']+=1;item['signed_instruction_items']+=identity[0]=='instruction'
        item['source_fields'].add(amount['source_field'])
        signature=evidence['evidence'].get('signature')
        if signature and signature not in item['signatures'] and len(item['signatures'])<3:item['signatures'].append(signature)
        if len(item['record_ids'])<3:item['record_ids'].append(evidence['id'])
    amounts=[]
    for key in sorted(totals):
        value=totals[key];amounts.append(dict(value,amount=format(value['amount'],'f'),source_fields=sorted(value['source_fields'])))
    # Keep provider-only associations distinct from transaction observations.
    actors=defaultdict(lambda:defaultdict(list));claims=[]
    for edge in edges:
        if _failed_record(edge):continue
        role=_ACTIVITY_ROLES.get(edge['kind'])
        if role:
            actors[edge['source']][edge['target']].append(dict(role=role,root=edge['root'],
                evidence_tier=edge['evidence_tier'],signature=edge['evidence'].get('signature'),slot=edge['slot']))
        elif edge['kind'] in _CLAIM_ROLES:
            claims.append(dict(address=edge['source'],token=edge['target'],role=_CLAIM_ROLES[edge['kind']],
                evidence_tier=edge['evidence_tier'],record_id=edge['id']))
    participants=[]
    for actor,tokens in sorted(actors.items(),key=lambda x:(x[0]!=wallet,-len(x[1]),x[0])):
        if actor!=wallet and len(tokens)<2:continue
        token_rows=[]
        for token,observations in sorted(tokens.items()):
            token_rows.append(dict(token=token,roles=sorted({v['role'] for v in observations}),
                evidence_tiers=sorted({v['evidence_tier'] for v in observations}),
                root_contexts=sorted({v['root'] for v in observations})[:8],examples=observations[:3]))
        participants.append(dict(address=actor,requested_wallet=actor==wallet,distinct_tokens=len(tokens),
            repeated_address_participation=len(tokens)>1,tokens=token_rows[:8],tokens_truncated=len(tokens)>8,
            same_funds_proven=False,common_owner_proven=False))
    transfer=dict(incoming_evidence_items=directions['incoming'],outgoing_evidence_items=directions['outgoing'],
        self_evidence_items=directions['self'],related_downstream_records=related,failed_records_excluded=failed,
        account_creation_funding_items=account_funding,quantity_unavailable_items=unknown,
        conflicting_items=len(conflicts),conflicts=conflicts[:8],
        duplicate_signed_instruction_observations=duplicates,
        unpositioned_or_unsigned_observations=unsigned,counterparty_unit_totals=amounts[:40],totals_truncated=len(amounts)>40,
        totals_meaning='Sums of retained compatible evidence items, not complete cash flow, current holdings or USD value.',
        provider_observations_may_overlap_raw_records=True)
    details=dict(evidence_tiers=dict(sorted(tiers.items())),time_range=period,direct_transfers=transfer,
        token_participation=dict(actors=participants[:20],actors_truncated=len(participants)>20,
            provider_claims=claims[:20],provider_claims_truncated=len(claims)>20,
            interpretation='Repeated public-address participation, not reuse of the same funds or proof of a common owner.'),
        retained_record_limit=600,source_view_truncated=truncated,independent_consensus_finality_verified=False)
    findings=[]
    if groups:
        findings.append(f"Direct wallet records contain {directions['incoming']} incoming, {directions['outgoing']} outgoing and {directions['self']} self-directed evidence items; {related} downstream records are kept separate.")
    for item in amounts[:3]:
        label='account-creation funding' if item['instruction_class']=='account_creation_funding' else 'instruction kind unspecified' if item['instruction_class']=='instruction_kind_unspecified' else 'transfer instruction'
        findings.append(f"{item['direction'].capitalize()} {item['amount']} {item['unit']} ({item['asset']}) with {item['counterparty']}: {label}, {item['evidence_tier']}, {item['evidence_items']} retained evidence item(s).")
    repeated=[a for a in participants if a['repeated_address_participation']]
    if repeated:
        actor=repeated[0];examples=', '.join(t['token']+' ('+'/'.join(t['roles'])+')' for t in actor['tokens'][:3])
        findings.append(f"Address {actor['address']} appears in transaction observations for {actor['distinct_tokens']} token mints: {examples}. This does not establish continuity of the same funds.")
    if known:findings.append(f"Known observation times span {period['first_known_utc']} to {period['last_known_utc']}; {period['unknown_timestamp_records']} records have no usable time, and gaps inside this interval remain unmeasured.")
    else:findings.append('No usable observation timestamps are retained; slots do not establish calendar-time coverage.')
    findings.append(f"Evidence comprises {tiers['raw_transaction_record']} raw-transaction records and {tiers['provider_or_local_observation']} provider/local observations; consensus finality and current balances are not verified.")
    if conflicts:findings.append(f'{len(conflicts)} conflicting instruction evidence item(s) were excluded from quantity totals.')
    if claims:findings.append(f'{len(claims)} provider/sample participation claim(s) remain separate from transaction-based participation.')
    return details,findings[:10]


class Workspace:
    def __init__(self,base):
        self.base=Path(base);self.out=self.base/'evidence/wallet-map-v1'
        queue_folder=self.out/'workspace';queue_folder.mkdir(exist_ok=True)
        self.db=sqlite3.connect(queue_folder/'queue.sqlite3',timeout=5)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,address TEXT UNIQUE,created_ms INTEGER,state TEXT,body TEXT);
          CREATE TABLE IF NOT EXISTS admissions(id TEXT PRIMARY KEY,at_ms INTEGER,body TEXT);''')
    def close(self):self.db.close()
    def submit(self,wallet):
        if not address(wallet):raise ValueError('Enter a valid Solana address.')
        old=self.db.execute('SELECT * FROM requests WHERE address=?',(wallet,)).fetchone()
        if old:return dict(old)
        if self.db.execute("SELECT count(*) FROM requests WHERE state='queued'").fetchone()[0]>=1000:
            raise ValueError('The research queue is full. Existing investigations are preserved.')
        ident=hashlib.sha256(('solana:'+wallet).encode()).hexdigest()[:24]
        now=time.time_ns()//1000000
        with self.db:self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',(ident,wallet,now,'queued',canonical(dict(network='solana',requested_history='all_available',complete_history=False))))
        return dict(self.db.execute('SELECT * FROM requests WHERE id=?',(ident,)).fetchone())
    def admit(self,graph,scope):
        scope.guard()
        for row in self.db.execute("SELECT * FROM requests WHERE state='queued' ORDER BY created_ms LIMIT 10").fetchall():
            wallet=row['address']
            with graph.db:
                graph.db.execute('INSERT OR IGNORE INTO roots VALUES(?,?)',(wallet,canonical(dict(mint=wallet,symbol=wallet[:6],name='Wallet investigation',root_kind='wallet',cohort_kind='internal_wallet_request',creator=None,created_ms=None,ranking_verified=False))))
                graph.node(wallet,'account','user_requested_wallet')
                # Recent discovery is explicit. Missing older history remains
                # a gap and is never inferred from an empty public response.
                job=graph.portal.seed_wallet(wallet,wallet,scope)
                graph.enqueue(wallet,wallet,None,1705618800000,0,'core_forward')
                if (graph.out/'address-index.sqlite3').exists():
                    graph.enqueue(wallet,wallet,0,None,0,'indexed_forward')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO admissions VALUES(?,?,?)',(row['id'],time.time_ns()//1000000,canonical(dict(root=wallet,portal=job))))
                self.db.execute("UPDATE requests SET state='investigating' WHERE id=?",(row['id'],))
    def _json(self,name,default):
        try:return json.loads((self.out/name).read_bytes())
        except (FileNotFoundError,ValueError):return default
    def snapshot(self):
        workers=self._json('workers.json',dict(configured=0,active=0,workers=[]))
        pool_root=os.environ.get('EM_RESEARCH_POOL_ROOT')
        if pool_root:
            # A model batch timestamp is not a source-collector heartbeat.
            # The shared pool reports its own live calls, independently.
            workers=dict(configured=50,active=0,workers=[],shared_pool=True,
                state='STATUS_UNAVAILABLE',updated_ms=0)
            try:
                path=Path(pool_root)/'evidence/muse-pool-v1/status.json'
                if path.is_symlink():raise ValueError('pool status symlink')
                with path.open('rb') as handle:raw=handle.read(262145)
                if len(raw)>262144:raise ValueError('pool status bound')
                current=json.loads(raw)
                if (not isinstance(current,dict) or current.get('configured')!=50
                    or type(current.get('active')) is not int
                    or not 0<=current['active']<=50
                    or type(current.get('updated_ms')) is not int):
                    raise ValueError('pool status schema')
                workers={key:current[key] for key in (
                    'configured','active','updated_ms','state','workers','queue',
                    'observed_peak_concurrent','observed_peak_provider_calls',
                    'last_sent_ms','last_finished_ms','adapter_results',
                    'wait_reason','model_calls','provider_not_before_ms') if key in current}
                workers.update(shared_pool=True,chains=['Solana','Arbitrum','Robinhood'])
            except (OSError,ValueError,TypeError):pass
        if time.time_ns()//1000000-workers.get('updated_ms',0)>90000:
            workers=dict(workers,active=0,stale=True)
        status=self._json('status.json',{})
        roots=[];path=self.out/'graph.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=2)) as db:
                for body, in db.execute('SELECT body FROM roots ORDER BY rowid LIMIT 1100'):
                    item=json.loads(body);roots.append({k:item.get(k) for k in ('mint','symbol','name','cohort_kind','root_kind','usd_market_cap','created_ms')})
        return dict(schema='ticking.internal-research.v1',updated_ms=time.time_ns()//1000000,
            queue=[dict(r) for r in self.db.execute('SELECT * FROM requests ORDER BY created_ms DESC LIMIT 100')],
            workers=workers,cohorts=roots,status=status,
            coverage=dict(complete_solana_history=False,private_cex_ledgers=False,current_balances_verified=False,
                acquisition=_acquisition_summary(self.base,time.time_ns()//1000000),
                description='Retained public observations and raw transaction checks. Coverage varies by address and time.'))
    def report(self,ident):
        request=self.db.execute('SELECT * FROM requests WHERE id=?',(ident,)).fetchone()
        if not request:return None
        wallet=request['address'];edges=[];states={};reasons=[];engine_plans=[];truncated=False;node_records={}
        quarantine=dict(checked_retained_records=0,raw_instruction_records_considered=0,current_committed_records=0,
            raw_records_quarantined=0,excluded_records=0,failed_records_excluded=0,failed_transfer_records_excluded=0,
            reasons={},assessment_statuses={},examples=[],source_records_preserved=True,
            requires_current_commit_assessment=True,excluded_from_graph_and_totals=True,whole_ledger_reconciled=False)
        source_coverage=None
        coverage_status=dict(available=False,state='unavailable',reason='retained_coverage_database_not_present',
            historical_receipts_preserved=True,read_only=True)
        path=self.out/'graph.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=2)) as db:
                db.row_factory=sqlite3.Row
                retained=db.execute('SELECT * FROM edges WHERE root=? OR source=? OR target=? ORDER BY rowid DESC LIMIT 601',(wallet,wallet,wallet)).fetchall()
                truncated=len(retained)>600
                graph=SimpleNamespace(db=db,out=self.out)
                edges,quarantine=_current_edge_view(graph,retained[:600])
                if (self.out/'coverage.sqlite3').exists():
                    from .coverage_projection import report_retained
                    try:
                        source_coverage=report_retained(graph,wallet,limit=64)
                        coverage_status=dict(available=True,state='available',read_only=True,
                            historical_receipts_preserved=True)
                    except sqlite3.Error as exc:
                        # A WAL database without retained sidecars cannot be
                        # opened on the web service's read-only mount. Keep the
                        # graph view usable without inventing empty coverage or
                        # treating a changing database as immutable.
                        coverage_status=dict(available=False,state='unavailable',
                            reason='retained_coverage_database_unavailable',
                            sqlite_error_name=getattr(exc,'sqlite_errorname',None) or 'SQLITE_ERROR',
                            historical_receipts_preserved=True,read_only=True)
                jobs={r[0] for r in db.execute('SELECT id FROM frontier WHERE root=?',(wallet,))}
                states=dict(db.execute('SELECT state,count(*) FROM frontier WHERE root=? GROUP BY state',(wallet,)))
                for row in db.execute('SELECT call_id,body,at FROM investigations ORDER BY id DESC LIMIT 200'):
                    item=json.loads(row['body'])
                    selected=[j for j in item.get('job_ids',[]) if j in jobs]
                    scheduled=[j for j in item.get('scheduled_job_ids',[]) if j in jobs]
                    if selected and len(reasons)<5:
                        reasons.append(dict(call_id=row['call_id'],reason=item.get('reason'),at_ms=row['at'],selected_by_model=True,
                            selected_wallet_job_ids=selected[:8],original_job_ids=item.get('job_ids',[])[:8],
                            purpose='investigation_planning',verified_conclusion=False))
                    supplements=[j for j in scheduled if j not in item.get('job_ids',[])]
                    if supplements and len(engine_plans)<5:
                        engine_plans.append(dict(call_id=row['call_id'],at_ms=row['at'],job_ids=supplements[:8],
                            selected_by_model=False,purpose='engine_coverage_supplement',verified_conclusion=False))
                node_ids=list({wallet}|{v for e in edges for v in (e['source'],e['target'])})
                for start in range(0,len(node_ids),400):
                    part=node_ids[start:start+400]
                    for row in db.execute('SELECT * FROM nodes WHERE id IN ('+','.join('?' for _ in part)+')',part):
                        node_records[row['id']]=dict(id=row['id'],kind=row['kind'],roles=json.loads(row['roles']))
        ids={wallet}|{v for e in edges for v in (e['source'],e['target'])}
        raw=sum(e['evidence_tier']=='raw_transaction_record' for e in edges)
        status='queued' if request['state']=='queued' else 'investigating' if states.get('pending') else 'report_ready_partial'
        limits=['This is a partial public-record investigation, not a complete wallet ledger.',
            'An address relationship does not prove common ownership or continuity of the same funds.',
            'Exchange customer ledgers, current balances and independent consensus finality are not verified.',
            'Quantities retain their source units; provider records can overlap raw records and are not a complete flow or a valuation.']
        if truncated:limits.append('This view is limited to the newest 600 retained records; omitted records can change the findings.')
        details,findings=_report_details(wallet,edges,truncated)
        details['instruction_commit_review']=quarantine
        details['direct_transfers']['failed_records_excluded']+=quarantine['failed_transfer_records_excluded']
        if quarantine['excluded_records']:
            findings.append(f"{quarantine['excluded_records']} retained records are excluded from this graph and quantity totals; {quarantine['raw_records_quarantined']} raw records lack an admissible current committed-instruction result or have failed/dead evidence. Original records remain preserved.")
        coverage=dict(complete=False,current_balance_verified=False,private_ledger_available=False,
            source_coverage_status=coverage_status)
        if source_coverage is not None:
            coverage.update(source_ledger=source_coverage,
                source_count=len(source_coverage['sources']),
                latest_observed_slot=source_coverage['latest_observed_slot'],
                gaps=source_coverage['spans']['gap'])
            details['source_coverage']=source_coverage
            if source_coverage['sources']:
                findings.append(f"Coverage is recorded separately for {len(source_coverage['sources'])} retained sources. The latest evidenced slot is {source_coverage['latest_observed_slot']}; unrecorded periods remain unknown.")
        elif coverage_status['reason']=='retained_coverage_database_unavailable':
            details['source_coverage_unavailable']=coverage_status
            findings.append('The retained source-coverage ledger is temporarily unavailable. Source counts, coverage gaps and the latest coverage slot are unknown in this response; the displayed graph uses its separate current evidence review.')
        details['planning']=dict(muse=reasons,engine_supplements=engine_plans,
            interpretation='Recorded model choices and rationales plan evidence collection; they are not verified research conclusions.')
        if reasons:findings.append(f"Muse selected this wallet's research jobs in {len(reasons)} displayed call(s): "+', '.join(str(r['call_id']) for r in reasons)+'. These rationales are investigation plans, not verified conclusions.')
        summary=findings[0] if edges else ('Retained records are excluded by the current evidence review; those flows are not drawn or totaled.' if quarantine['excluded_records'] else 'No retained relationship evidence is available for this request yet.')
        report=dict(title='Wallet investigation',address=wallet,summary=summary,findings=findings,details=details,limitations=limits,
            generated_ms=time.time_ns()//1000000,muse=reasons,complete_history=False,
            evidence_records=[dict(kind=e['kind'],source=e['source'],target=e['target'],slot=e['slot'],stamp=e['stamp'],evidence_tier=e['evidence_tier'],signature=e['evidence'].get('signature'),source_sha256=e['evidence'].get('source_sha256')) for e in edges[:100]])
        return dict(investigation=dict(request,state=status,status=status,frontier_states=states),
            graph=dict(nodes=[node_records.get(x,dict(id=x,kind='account',roles=['requested_wallet'] if x==wallet else [])) for x in sorted(ids)],edges=edges,view_truncated=truncated,excluded_records=quarantine['excluded_records']),
            analysis=dict(summary=summary,raw_relationships=raw,observed_relationships=len(edges),quarantined_relationships=quarantine['raw_records_quarantined'],muse=reasons,findings=findings,details=details,limitations=limits),
            report=report,coverage=coverage)
