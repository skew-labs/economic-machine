"""Muse proposes research jobs; the typed scheduler admits bounded archive reads.

No external text can introduce shell commands, URLs, credentials or order actions.
"""
import argparse
from decimal import Decimal
import fcntl
import hashlib
import json
import math
from pathlib import Path
import time
from .store import Store, canonical, atomic, address
from .reporting import summary as status_summary
from ..muse_maker import MODEL, unique
from ..muse_policy_worker import Budget, provider_call

class ResearchBudget(Budget):
    def reserve(self, req, now):
        body=canonical(req).encode()
        if len(body)>16000 or req.get('model')!=MODEL or req.get('max_tokens')!=4096: raise ValueError('RESEARCH_REQUEST_BOUND')
        bound=Decimal(len(body)+1024)*Decimal('0.0000001')+Decimal(4096)*Decimal('0.0000002')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE');totals=self.totals()
            if totals['cost_breach'] or now>=self.scope['deadline_ms'] or totals['calls_reserved']>=self.scope['max_calls'] or Decimal(totals['cost_committed_or_reserved_usd'])+bound>Decimal(self.scope['maximum_usd']): raise ValueError('RESEARCH_MODEL_BUDGET_EXHAUSTED')
            row=self.db.execute('INSERT INTO calls(request_hash,reserved,status,created_ms) VALUES(?,?,?,?)',(hashlib.sha256(body).hexdigest(),str(bound),'reserved',now))
        return row.lastrowid


def historical_context(root):
    """Numeric replication evidence only; no downloaded prose or dataset SQL."""
    path=Path(root).parent/'evidence/history-alpha-v1/historical-alpha-result-v2.json'
    if not path.exists():return None
    if path.stat().st_size>128*1024:raise ValueError('HISTORY_REPORT_BOUND')
    raw=path.read_bytes();r=json.loads(raw)
    if r.get('protocol_sha256')!='bc8af0235a275a29f612d714e0921b96780ac57ee7ecd6db0967fc0bdfb0d5fe' or r.get('profitability_proven') is not False or r.get('live_promotion')!='BLOCKED':
        raise ValueError('HISTORY_REPORT_IDENTITY')
    def number(v,lower,upper):
        if type(v) not in (int,float) or not math.isfinite(v) or not lower<=v<=upper:raise ValueError('HISTORY_REPORT_NUMBER')
        return v
    output=[]
    for signal in ('all_eligible','volume_per_trade_top_quartile','wallet_breadth_top_quartile','positive_price_gain_per_trade_top_quartile'):
        rows=[x for x in r['results'] if x['partition']=='test' and x['signal']==signal and x['delay_ms']==800 and x['hold_ms']==30000]
        if len(rows)!=1:raise ValueError('HISTORY_REPORT_VARIANT')
        x=rows[0]
        selected=number(x['selected'],0,2000000)
        states={k:number(x['states'].get(k,0),0,2000000) for k in ('resolved_price_pair','entry_unobserved','exit_unobserved')}
        if sum(states.values())!=selected:raise ValueError('HISTORY_REPORT_COUNTS')
        metrics={k:(None if x[k] is None else number(x[k],-2,1000000)) for k in ('resolved_net_mean','resolved_net_median')}
        output.append(dict(signal=signal,selected=selected,states=states,**metrics))
    return dict(source='third_party_event_triggered_snapshots',sha256=hashlib.sha256(raw).hexdigest(),
                snapshot_mints=number(r['quality']['snapshot_mints'],0,2000000),
                later_partition=output,delay_ms=800,hold_ms=30000,
                figures_are_price_sensitivities_not_executable_pnl=True,
                missing_exits_censored=True,all_time_coverage=False,
                independently_chain_verified=False,profitability_proven=False)

def context(store):
    # Public metadata strings, links and raw website instructions are excluded.
    # The model sees opaque candidate IDs and only bounded numeric observations.
    rows=list(store.db.execute("SELECT * FROM candidates WHERE ath_status='provider_claim_unverified' AND created_ms>1705000000000 ORDER BY provider_rank LIMIT 16"))
    candidates=[];mapping={}
    for r in rows:
        ident=hashlib.sha256(r['mint'].encode()).hexdigest()[:16];mapping[ident]=dict(r)
        launch=store.db.execute('SELECT min(slot),count(*) FROM launches WHERE mint=?',(r['mint'],)).fetchone()
        activity=store.db.execute('SELECT count(*),count(DISTINCT wallet) FROM activity WHERE mint=?',(r['mint'],)).fetchone()
        funding_edges=0
        if launch[0] is not None:
            report=store.token(r['mint']);funding_edges=sum(len(t['edges']) for t in report['prelaunch_funding_paths'])
        candidates.append(dict(id=ident,provider_rank=r['provider_rank'],reported_ath=r['reported_ath'],ath_verified=False,created_ms=r['created_ms'],observed_launch_slot=launch[0],launch_observations=launch[1],activity_observations=activity[0],distinct_wallets=activity[1],indexed_prelaunch_funding_edges=funding_edges,already_queued=bool(store.db.execute('SELECT 1 FROM jobs WHERE id=?',('launch:'+r['mint'],)).fetchone())))
    summary=status_summary(store)
    research=None;path=store.root/'strategy-status.json'
    if path.exists():
        report=json.loads(path.read_bytes())
        research={k:report.get(k) for k in ('protocol_id','cohorts','event_valuation','outcomes','reasons','unindexed_evidence_objects','observation_unusable_by_launch','funding_lookup','event_evidence_paths','promotion','validated_pnl','blockers')}
    return dict(schema='economic-machine.pump-research.v1',counts=summary['counts'],candidates=candidates,quality=summary['ath_quality'],frozen_strategy_research=research,historical_replication=historical_context(store.root),all_time_ranking_verified=False,history_complete=False,synthetic_or_real_profitability_proven=False,upstream_gaps=(summary.get('ticking') or {}).get('upstream'),trade_authority=False),mapping

def validate(value,mapping):
    from .fast_policy import CATALOG
    if type(value) is not dict or set(value) not in ({'candidate_ids','hypotheses','limitations'},{'candidate_ids','hypotheses','limitations','signal_profile'}): raise ValueError('PLAN_KEYS')
    if 'signal_profile' in value and value['signal_profile'] not in CATALOG:raise ValueError('PLAN_SIGNAL_PROFILE')
    ids=value['candidate_ids']
    if type(ids) is not list or not 1<=len(ids)<=2 or len(set(ids))!=len(ids) or any(i not in mapping for i in ids): raise ValueError('PLAN_CANDIDATE_SCOPE')
    for k in ('hypotheses','limitations'):
        if type(value[k]) is not list or not 1<=len(value[k])<=6 or any(type(x) is not str or not 1<=len(x)<=600 for x in value[k]): raise ValueError('PLAN_TEXT_BOUND')
    return value

def cycle(root, key_file):
    s=Store(root);lock=(s.root/'model.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scope_path=s.root/'research-model-scope.json'
    if not scope_path.exists():
        atomic(scope_path,dict(session_id='pump-research-20261015',deadline_ms=int(time.time()*1000)+48*3600000,maximum_usd='0.03',max_calls=12))
    budget=ResearchBudget(s.root/'model-budget.sqlite3',**json.loads(scope_path.read_bytes()))
    from .cli import enqueue_funding_followups
    enqueue_funding_followups(s)
    totals=budget.totals()
    if totals['calls_reserved']>=budget.scope['max_calls'] or time.time()*1000>=budget.scope['deadline_ms'] or Decimal(totals['cost_committed_or_reserved_usd'])+Decimal('0.0026')>Decimal(budget.scope['maximum_usd']):
        result=dict(state='COMPLETE_MODEL_BUDGET',budget=totals,trading_enabled=False)
        atomic(s.root/'model-status.json',result);return result
    ctx,mapping=context(s)
    from .fast_policy import CATALOG, CATALOG_SHA256, install, feedback
    ctx['native_signal_catalog']=dict(sha256=CATALOG_SHA256,profiles=CATALOG,execution_authority=False,
                                    meaning='Choose one precompiled research profile. Selection changes only new cohorts. No proven alpha or live orders.')
    ctx['native_signal_observations']=feedback(s.root,int(time.time()*1000))
    req=dict(model=MODEL,temperature=0.2,max_tokens=4096,response_format={'type':'json_object'},provider={'allow_fallbacks':False,'max_price':{'prompt':0.1,'completion':0.2}},messages=[
        {'role':'system','content':'You are the Economic Machine Pump launch research planner. Frozen model; no training or execution permission. Select up to two candidate_ids from supplied evidence for sparse historical archive reconstruction. Never invent observations or ownership links. Offchain ATH values are unverified. Same funder and same slot are association only; CEX/router boundaries stop ownership inference. Unknown history is not clean. Propose falsifiable funding and early-entry/exit hypotheses; eventual tests must include failed launches, forward holdout, executable liquidity, fees, slippage, latency, and survivorship bias. Return exactly JSON keys candidate_ids (1-2 opaque IDs), hypotheses (1-6 strings), limitations (1-6 strings), signal_profile (one supplied catalog ID). Prefer candidates not already queued. This output can only prioritize public data research.'},
        {'role':'user','content':canonical(ctx)}])
    req['messages'][0]['content'] += ' Also return signal_profile, exactly one supplied native_signal_catalog profile ID. This fourth JSON key selects a bounded, one-hour, research-only native policy. Choose using the censored historical replication, not unverified ATH. Never claim full funding coverage or executable profitability.'
    req['response_format']={'type':'json_schema','json_schema':{'name':'pump_research_and_native_profile','strict':True,'schema':{
        'type':'object','additionalProperties':False,
        'properties':{'signal_profile':{'type':'string','enum':list(CATALOG)},
                      'candidate_ids':{'type':'array','items':{'type':'string','enum':list(mapping)}},
                      'hypotheses':{'type':'array','items':{'type':'string'}},
                      'limitations':{'type':'array','items':{'type':'string'}}},
        'required':['signal_profile','candidate_ids','hypotheses','limitations']}}}
    req['provider']['require_parameters']=True
    call=budget.reserve(req,int(time.time()*1000));atomic(s.root/f'model-request-{call:03}.json',req)
    raw=provider_call(req,key_file)
    atomic(s.root/f'model-response-{call:03}.json',json.loads(raw,object_pairs_hook=unique))
    accepted=False
    try:
        response=json.loads(raw,object_pairs_hook=unique)
        if response.get('model')!=MODEL: raise ValueError('MODEL_IDENTITY')
        value=json.loads(response['choices'][0]['message']['content'],object_pairs_hook=unique)
        value=validate(value,mapping)
        if 'signal_profile' not in value:raise ValueError('PLAN_SIGNAL_PROFILE_REQUIRED')
        accepted=True
    finally: budget.finish(call,raw,accepted)
    ident=hashlib.sha256(raw.encode()).hexdigest();queued=[]
    with s.db:
        s.db.execute('INSERT OR IGNORE INTO plans VALUES(?,?,?)',(ident,canonical(value),time.time()))
        for opaque in value['candidate_ids']:
            row=mapping[opaque];jid='launch:'+row['mint'];args=dict(mint=row['mint'],created_ms=row['created_ms'])
            if s.db.execute('INSERT OR IGNORE INTO jobs(id,kind,args,status) VALUES(?,?,?,?)',(jid,'launch_window',canonical(args),'pending')).rowcount: queued.append(jid)
    installed=None
    reconciled=budget.db.execute('SELECT status,actual FROM calls WHERE id=?',(call,)).fetchone()
    if value.get('signal_profile') and reconciled[0]=='valid' and reconciled[1] is not None and not budget.totals()['cost_breach']:
        installed=install(s.root,value['signal_profile'],call,ident,int(time.time()*1000),budget.scope['deadline_ms'])
    report=dict(model=MODEL,call=call,accepted=True,queued=queued,plan=value,budget=budget.totals(),trading_enabled=False,
                native_policy_installed=installed)
    atomic(s.root/'model-status.json',report);return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--key-file',required=True);a=p.parse_args()
    print(canonical(cycle(a.root,a.key_file)))
