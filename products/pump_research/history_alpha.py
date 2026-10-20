"""Time-split exploratory signal audit; price observations are never fills."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics


def quantile(values, q):
    if not values:
        return None
    a = sorted(values)
    return a[min(len(a)-1, int(q*(len(a)-1)))]


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def decision(rows, start, p):
    candidates = [r for r in rows if start+p['decision_minimum_age_ms'] <= r['timestamp'] <= start+p['decision_maximum_age_ms']]
    if not candidates:
        return None
    # Rows are already collapsed by timestamp, keeping their final recorded id.
    r = candidates[0]
    price, volume, trades, wallets = (r[k] for k in ('current_price', 'total_volume_sol', 'total_trade_count', 'unique_wallets'))
    if not all(finite(v) for v in (price, volume, trades, wallets)) or min(price, trades)<=0 or min(volume, wallets)<0:
        return None
    first = rows[0]['current_price']
    gain = math.log(price/first)/trades if finite(first) and first>0 else None
    return dict(time=r['timestamp'], price=price, volume_per_trade=volume/trades,
                breadth=wallets, gain_per_trade=gain)


def observed_quote(rows, target, wait):
    # The first observation after the target must itself be usable; skip no bad
    # prices to manufacture a better quote. No interpolation across silent spans.
    row = next((r for r in rows if r['timestamp']>=target), None)
    if row is None or row['timestamp']>target+wait:
        return None
    value = row['current_price']
    return row if finite(value) and value>0 else None


def quote_outcome(rows, d, delay, hold, p):
    entry = observed_quote(rows, d['time']+delay, p['entry_maximum_wait_ms'])
    if entry is None:
        return dict(state='entry_unobserved')
    exit = observed_quote(rows, entry['timestamp']+hold, p['exit_maximum_wait_ms'])
    if exit is None:
        return dict(state='exit_unobserved')
    gross = exit['current_price']/entry['current_price']
    cost = p['quote_cost_sensitivity']
    friction = (cost['fee_each_side_bps']+cost['adverse_each_side_bps'])/10000
    fixed = (2*cost['network_each_side_lamports']+cost['unrecovered_rent_lamports'])/(cost['notional_sol']*1e9)
    return dict(state='resolved_price_pair', gross_return=gross-1,
                net_sensitivity_return=gross*(1-friction)/(1+friction)-1-fixed,
                entry_wait_ms=entry['timestamp']-d['time']-delay,
                exit_wait_ms=exit['timestamp']-entry['timestamp']-hold)


def evaluate(groups, p):
    ordered = sorted(groups, key=lambda m:(groups[m][0]['timestamp'], m))
    if len(ordered)<10:
        raise ValueError('HISTORICAL_COHORT_TOO_SMALL')
    split = groups[ordered[int(len(ordered)*p['training_fraction'])]][0]['timestamp']
    embargo = p.get('training_embargo_ms', 0)
    purged = {m for m in ordered if split-embargo <= groups[m][0]['timestamp'] < split}
    decisions, featureless = {}, Counter()
    for mint in ordered:
        if mint in purged:continue
        d = decision(groups[mint], groups[mint][0]['timestamp'], p)
        part = 'train' if groups[mint][0]['timestamp']<split else 'test'
        if d is None:
            featureless[part] += 1
        else:
            decisions[mint] = d
    train = [decisions[m] for m in decisions if groups[m][0]['timestamp']<split]
    thresholds = dict(volume_per_trade=quantile([x['volume_per_trade'] for x in train], .75),
                      breadth=quantile([x['breadth'] for x in train], .75),
                      gain_per_trade=quantile([x['gain_per_trade'] for x in train if finite(x['gain_per_trade']) and x['gain_per_trade']>0], .75))
    results = []
    for part in ('train', 'test'):
        for signal in p['signals']:
            selected = []
            for mint, d in decisions.items():
                if (groups[mint][0]['timestamp']<split) != (part=='train'):
                    continue
                key = {'volume_per_trade_top_quartile':'volume_per_trade', 'wallet_breadth_top_quartile':'breadth', 'positive_price_gain_per_trade_top_quartile':'gain_per_trade'}.get(signal)
                if key and (thresholds[key] is None or not finite(d[key]) or d[key]<thresholds[key] or (key=='gain_per_trade' and d[key]<=0)):
                    continue
                selected.append(mint)
            for delay in p['entry_delays_ms']:
                for hold in p['hold_ms']:
                    outcomes = [quote_outcome(groups[m], decisions[m], delay, hold, p) for m in selected]
                    states = Counter(o['state'] for o in outcomes)
                    net = [o['net_sensitivity_return'] for o in outcomes if o['state']=='resolved_price_pair']
                    gross = [o['gross_return'] for o in outcomes if o['state']=='resolved_price_pair']
                    entries = len(net)+states['exit_unobserved']
                    cost=p['quote_cost_sensitivity']
                    loss = -1-(2*cost['network_each_side_lamports']+cost['unrecovered_rent_lamports'])/(cost['notional_sol']*1e9)
                    results.append(dict(partition=part, signal=signal, delay_ms=delay, hold_ms=hold, selected=len(selected), states=dict(states),
                                        resolved_gross_mean=statistics.mean(gross) if gross else None,
                                        resolved_net_mean=statistics.mean(net) if net else None,
                                        resolved_net_median=statistics.median(net) if net else None,
                                        resolved_net_positive=sum(x>0 for x in net),
                                        resolved_net_p05=quantile(net,.05), resolved_net_p95=quantile(net,.95),
                                        entered_full_loss_missing_exit_mean=(sum(net)+states['exit_unobserved']*loss)/entries if entries else None,
                                        validated_pnl=None))
    return dict(split_timestamp_ms=split, training_embargo_ms=embargo, purged_boundary_mints=len(purged), cohort_sizes=dict(train=sum(groups[m][0]['timestamp']<split and m not in purged for m in ordered), test=sum(groups[m][0]['timestamp']>=split for m in ordered)),
                no_eligible_decision=dict(featureless), eligible_decisions=len(decisions), train_thresholds=thresholds, variants_tested=16,
                results=results, profitability_proven=False, live_promotion='BLOCKED')


def main():
    a=argparse.ArgumentParser();a.add_argument('--evidence',required=True);a.add_argument('--protocol',required=True);a.add_argument('--output',default='historical-alpha-result.json');args=a.parse_args()
    out=Path(args.evidence);protocol=Path(args.protocol).read_bytes();p=json.loads(protocol)
    db=sqlite3.connect('file:'+str(out/'fingerprint-projection.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    groups=defaultdict(list);duplicates=0;triggers=Counter();zero_prices=0
    for row in db.execute('SELECT * FROM fingerprint_snapshots ORDER BY mint,timestamp,id'):
        r=dict(row);triggers[r['trigger_reason']]+=1
        if not finite(r['current_price']) or r['current_price']<=0:zero_prices+=1
        if groups[r['mint']] and groups[r['mint']][-1]['timestamp']==r['timestamp']:
            duplicates+=1;groups[r['mint']][-1]=r
        else:groups[r['mint']].append(r)
    result=evaluate(groups,p)
    timestamps=[r['timestamp'] for rows in groups.values() for r in rows]
    token_mints={r[0] for r in db.execute('SELECT mint FROM tokens')}
    rotation_mints={r[0] for r in db.execute('SELECT DISTINCT mint FROM rotations')}
    result.update(protocol_sha256=hashlib.sha256(protocol).hexdigest(), first_seen_is_not_verified_creation=True,
                  quality=dict(snapshot_mints=len(groups), token_table_mints=len(token_mints), rotation_mints=len(rotation_mints),
                               snapshot_mints_absent_from_token_table=len(set(groups)-token_mints),
                               token_table_mints_without_snapshots=len(token_mints-set(groups)),
                               token_or_rotation_mints_without_snapshots=len((token_mints|rotation_mints)-set(groups)),
                               duplicate_timestamp_rows=duplicates, nonpositive_or_unknown_prices=zero_prices, triggers=dict(triggers),
                               start_utc=datetime.fromtimestamp(min(timestamps)/1000,timezone.utc).isoformat(),
                               end_utc=datetime.fromtimestamp(max(timestamps)/1000,timezone.utc).isoformat()),
                  limitations=['Event-triggered snapshots, not all swaps', 'First seen timestamp need not be launch',
                               'No reserves or independently verified signatures', 'No exact historical fees or counterfactual market impact',
                               'Missing exits are censored; selected resolved pairs have selection bias',
                               'Chronological exploratory replication, not independent live out-of-sample validation',
                               'Same creator/funder may span both halves; no claim of independent creator holdout'])
    if Path(args.output).name!=args.output:raise ValueError('REPORT_OUTPUT_NAME')
    target=out/args.output
    if target.exists():raise ValueError('PRESERVE_EXISTING_REPORT')
    target.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='results'},indent=2))
    for r in result['results']:
        if r['partition']=='test':print(json.dumps(r))


if __name__=='__main__':main()
