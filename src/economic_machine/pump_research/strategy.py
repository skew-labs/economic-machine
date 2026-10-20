"""Frozen causal research rules and size-aware, explicitly hypothetical quotes.

Not a transaction builder. No model output can alter the frozen experiment.
"""
from collections import defaultdict
from .events import valuation_reason, ZERO, WSOL

CONFIG = dict(version='pump-causal-demand-v6', observation_slots=12,
              minimum_buyers=6, minimum_active_slots=3, minimum_net_lamports=300000000,
              maximum_buyer_share_bps=3500, maximum_funder_share_bps=6000,
              maximum_roundtrip_bps=3500, notional_lamports=50000000,
              entry_delay_slots=[2,8,20], maximum_quote_wait_slots=20,
              maximum_hold_slots=150, take_profit_bps=10000, stop_loss_bps=1500,
              adverse_execution_bps=25, network_cost_each_lamports=100000,
              unrecovered_rent_lamports=2039280,
              strategies=['demand_filter','breadth_only','all_launches'],
              minimum_holdout_launches=1000, minimum_resolved_entries=200,
              minimum_observation_hours=168)

def point(e): return e['slot'], e.get('tx_order',0), e.get('ix',0)
def ceildiv(a,b): return (a+b-1)//b

def fees(amount,e):
    return sum(ceildiv(amount*e[k],10000) for k in ('fee_basis_points','creator_fee_basis_points'))

def buy_quote(e,budget):
    reason=valuation_reason(e)
    if reason: raise ValueError(reason)
    lo,hi=0,budget
    while lo<hi:
        mid=(lo+hi+1)//2
        if mid+fees(mid,e)<=budget: lo=mid
        else: hi=mid-1
    # Conservative integer rounding: reserve invariants and own price impact.
    tokens=e['virtual_token_reserves']*lo//(e['virtual_sol_reserves']+lo)
    tokens=max(0,tokens-1)
    if tokens<=0 or tokens>=e['real_token_reserves']: raise ValueError('entry_crosses_migration_or_empty')
    return tokens

def sell_quote(e,tokens):
    reason=valuation_reason(e)
    if reason: raise ValueError(reason)
    gross=tokens*e['virtual_sol_reserves']//(e['virtual_token_reserves']+tokens)
    if gross>e['real_sol_reserves']: raise ValueError('insufficient_real_sol')
    return max(0,gross-fees(gross,e))

def features(events,launch,cutoff,funders=None):
    """No later events, later funding links, or ex-post winner labels allowed."""
    rows=[e for e in events if point(e)>=point(launch) and e['slot']<cutoff
          and e.get('quote_mint') in (ZERO,WSOL) and e.get('layout_supported')]
    buys=defaultdict(int);sells=defaultdict(int);per_tx=defaultdict(lambda:[0,0])
    developer={x for x in (launch.get('payer'),launch.get('creator')) if x}
    for e in rows:
        amount=e['sol_amount']; (buys if e['is_buy'] else sells)[e['user']]+=amount
        per_tx[(e['signature'],e['user'])][0 if e['is_buy'] else 1]+=e['token_amount']
    total=sum(buys.values());net=total-sum(sells.values())
    roundtrip=sum(min(buys[w],sells[w]) for w in buys)
    funders=funders or {};groups=defaultdict(int)
    for wallet,amount in buys.items():
        # Only observed shared funding, never inferred common ownership.
        groups[funders.get(wallet,'unlinked:'+wallet)]+=amount
    return dict(events=len(rows),buyer_wallets=len(buys),active_slots=len({e['slot'] for e in rows}),
                buy_lamports=total,net_lamports=net,
                largest_buyer_bps=max(buys.values(),default=0)*10000//max(total,1),
                observed_shared_funder_bps=max(groups.values(),default=0)*10000//max(total,1),
                funding_linked_buyers=sum(w in funders for w in buys),
                roundtrip_bps=roundtrip*10000//max(total,1),
                atomic_roundtrip_count=sum(min(x)>0 for x in per_tx.values()),
                developer_sell_lamports=sum(sells[w] for w in developer),
                funding_history_complete=False,ownership_inferred=False)

def signal(f,cfg,strategy):
    if f['events']==0:return 'no_observed_trades'
    if strategy=='all_launches': return 'candidate'
    if f['buyer_wallets']<cfg['minimum_buyers']:return 'insufficient_buyer_breadth'
    if strategy=='breadth_only':return 'candidate'
    checks=[(f['active_slots']<cfg['minimum_active_slots'],'single_burst'),
            (f['net_lamports']<cfg['minimum_net_lamports'],'weak_net_demand'),
            (f['largest_buyer_bps']>cfg['maximum_buyer_share_bps'],'concentrated_buyer'),
            (f['observed_shared_funder_bps']>cfg['maximum_funder_share_bps'],'shared_funding_concentration'),
            (f['roundtrip_bps']>cfg['maximum_roundtrip_bps'] or f['atomic_roundtrip_count']>0,'roundtrip_activity'),
            (f['developer_sell_lamports']>0,'developer_selling')]
    return next((reason for failed,reason in checks if failed),'candidate')

def evaluate(events,launch,watermark,cfg=CONFIG,strategy='demand_filter',delay=2,funders=None):
    cutoff=launch['slot']+cfg['observation_slots'];rows=sorted(events,key=point)
    base=dict(cutoff_slot=cutoff,strategy=strategy,delay_slots=delay,
              validated_pnl_lamports=None,live_trade=False)
    if watermark<cutoff:return dict(base,status='pending_observation')
    # Use information already received when the first cutoff marker arrived.
    markers=[e for e in rows if e['slot']>=cutoff]
    knowledge=min((e.get('observed',float('inf')) for e in markers),default=float('inf'))
    known=[e for e in rows if e.get('observed',0)<=knowledge]
    f=features(known,launch,cutoff,funders);reason=signal(f,cfg,strategy)
    base.update(features=f,signal=reason,knowledge_at=knowledge if knowledge!=float('inf') else None)
    if reason!='candidate':return dict(base,status='no_entry')
    target=cutoff+delay
    future=[e for e in rows if e['slot']>=target]
    if not future:return dict(base,status='pending_entry' if watermark<=target+cfg['maximum_quote_wait_slots'] else 'entry_unobserved')
    entry=future[0]
    if entry['slot']>target+cfg['maximum_quote_wait_slots']:return dict(base,status='entry_unobserved')
    try:tokens=buy_quote(entry,cfg['notional_lamports'])
    except ValueError as e:return dict(base,status='entry_unsupported',reason=str(e))
    tokens=tokens*(10000-cfg['adverse_execution_bps'])//10000
    cost=cfg['notional_lamports']+2*cfg['network_cost_each_lamports']+cfg['unrecovered_rent_lamports']
    base.update(entry_slot=entry['slot'],entry_signature=entry['signature'],tokens=tokens,
                assumed_total_cost_lamports=cost,worst_case_loss_lamports=-cost,
                quote_method='observed_post_event_reserve_sensitivity_not_counterfactual_fill')
    held=[e for e in future if point(e)>point(entry)]
    deadline=entry['slot']+cfg['maximum_hold_slots'];trigger=None
    for e in held:
        if e['slot']>deadline+cfg['maximum_quote_wait_slots']:break
        try:value=sell_quote(e,tokens)*(10000-cfg['adverse_execution_bps'])//10000
        except ValueError as ex:return dict(base,status='exit_censored',reason=str(ex))
        dev_sell=not e['is_buy'] and e['user'] in (launch.get('payer'),launch.get('creator'))
        if trigger is None and (dev_sell or e['slot']>=deadline or value*10000>=cost*(10000+cfg['take_profit_bps']) or value*10000<=cost*(10000-cfg['stop_loss_bps'])):
            trigger=e['slot'];base['exit_trigger']='developer_sell' if dev_sell else 'timeout' if e['slot']>=deadline else 'profit_or_loss'
        # Signal and hypothetical fill can never occur on the same event/slot.
        if trigger is not None and e['slot']>=trigger+delay:
            if e['slot']>trigger+delay+cfg['maximum_quote_wait_slots']:break
            return dict(base,status='indicative_resolved',exit_slot=e['slot'],exit_signature=e['signature'],
                        quote_return_lamports=value-cost,quote_return_bps=(value-cost)*10000//cost)
    return dict(base,status='pending_exit' if watermark<=deadline+delay+cfg['maximum_quote_wait_slots'] else 'exit_censored',reason='no_observed_executable_exit')
