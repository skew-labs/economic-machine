"""Optimization can stop only after the full outstanding workload fits.

A decoder microbenchmark or an epoch percentage is never completion evidence.
This gate does not stop existing collection, authorize spending, or extend time.
"""
import math

def number(value, minimum=0):
 return type(value) in (int,float) and math.isfinite(value) and value>=minimum

def evaluate(now_ms,deadline_ms,chains,budget):
 reasons=[];forecasts={};seconds=max(0,(deadline_ms-now_ms)/1000)
 if seconds<=0:reasons.append('DEADLINE_REACHED')
 for name in ('solana','arbitrum','robinhood'):
  v=chains.get(name,{})
  if not v.get('transaction_search_connected'):reasons.append(name+':SEARCH_NOT_CONNECTED')
  if not v.get('remaining_work_known'):reasons.append(name+':REMAINING_WORK_UNKNOWN')
  if v.get('unresolved_required_gaps',1):reasons.append(name+':REQUIRED_DATA_GAPS')
  samples=v.get('sustained_windows',[])
  valid=[x for x in samples if number(x.get('seconds'),600) and x.get('cold_source') is True and x.get('search_committed') is True
     and number(x.get('units')) and x['units']>0 and number(x.get('start_ms'))
     and x['start_ms']+x['seconds']*1000<=now_ms]
  if v.get('workload_scope_matches_remaining') is not True:
   reasons.append(name+':REPRESENTATIVE_WORKLOAD_NOT_PROVEN')
  if len(valid)<3:reasons.append(name+':SUSTAINED_RATE_NOT_PROVEN');continue
  # Consecutive, non-overlapping windows; repeated benchmarks do not count.
  valid=sorted(valid,key=lambda x:x['start_ms'])[-3:]
  if any(a['start_ms']+a['seconds']*1000>b['start_ms'] for a,b in zip(valid,valid[1:])):
   reasons.append(name+':OVERLAPPING_RATE_WINDOWS');continue
  if now_ms-(valid[-1]['start_ms']+valid[-1]['seconds']*1000)>1800000:
   reasons.append(name+':STALE_RATE');continue
  remaining=v.get('remaining_units');rate=min(x['units']/x['seconds'] for x in valid)
  if type(remaining) not in (int,float) or not math.isfinite(remaining) or remaining<0:
   reasons.append(name+':REMAINING_UNITS_UNKNOWN');continue
  eta=remaining/rate;forecasts[name]=dict(rate=rate,eta_seconds=eta,remaining_units=remaining)
  if eta>seconds*.8:reasons.append(name+':INSUFFICIENT_TIME_WITH_MARGIN')
 if not all(number(budget.get(k)) for k in ('projected_completion_krw','projected_completion_bytes','limit_krw','limit_bytes')):
  reasons.append('FULL_COMPLETION_RESOURCE_COST_UNKNOWN')
 elif budget['projected_completion_krw']>budget['limit_krw'] or budget['projected_completion_bytes']>budget['limit_bytes']:
  reasons.append('FULL_COMPLETION_RESOURCE_LIMIT')
 return dict(state='STOP_OPTIMIZATION_KEEP_COLLECTION' if not reasons else 'CONTINUE_OPTIMIZATION',reasons=reasons,forecasts=forecasts,
  full_mapping_complete=False,deadline_ms=deadline_ms,automatic_budget_change=False)
