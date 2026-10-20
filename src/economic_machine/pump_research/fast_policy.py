"""Muse selects a finite research policy; it cannot edit code or grant orders."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
from .store import atomic, canonical

POLICY_SCHEMA='economic-machine.pump-native-policy.v1'
BASE=dict(window_slots=12,buyers=6,positive_buckets=3,net_lamports=300000000,
          buyer_bps=3500,funder_bps=6000,roundtrip_bps=3500,notional=50000000,
          minimum_real_sol=500000000,maximum_quote_age_slots=2)
CATALOG={
    'persistent_demand':BASE,
    'liquid_breadth':dict(BASE,buyers=8,net_lamports=500000000,minimum_real_sol=1000000000),
    'patient_demand':dict(BASE,window_slots=24,positive_buckets=4,buyers=8,net_lamports=500000000),
}
CATALOG_SHA256=hashlib.sha256(canonical(CATALOG).encode()).hexdigest()

def install(root,profile,call,response_sha256,now_ms,deadline_ms):
    if profile not in CATALOG or type(call) is not int or call<=0:
        raise ValueError('FAST_POLICY_SELECTION')
    if len(response_sha256)!=64 or any(c not in '0123456789abcdef' for c in response_sha256):
        raise ValueError('FAST_POLICY_PROVENANCE')
    if now_ms>=deadline_ms:raise ValueError('FAST_POLICY_EXPIRED')
    path=Path(root)/'fast-policy.json'
    if path.exists() and json.loads(path.read_bytes())['version']>=call:
        raise ValueError('FAST_POLICY_ROLLBACK')
    policy=dict(schema=POLICY_SCHEMA,profile=profile,version=call,created_ms=now_ms,
                expires_ms=min(now_ms+3600000,deadline_ms),catalog_sha256=CATALOG_SHA256,
                model='meta/muse-spark-1.3-contributor',response_sha256=response_sha256,
                parameters=CATALOG[profile],execution_authority=False,profitability_proven=False)
    atomic(path,policy);return policy

def validate(p,now_ms,deadline_ms):
    expected={'schema','profile','version','created_ms','expires_ms','catalog_sha256','model',
              'response_sha256','parameters','execution_authority','profitability_proven'}
    if type(p) is not dict or set(p)!=expected or p['schema']!=POLICY_SCHEMA:
        raise ValueError('FAST_POLICY_SCHEMA')
    if (p['profile'] not in CATALOG or p['parameters']!=CATALOG[p['profile']]
        or p['catalog_sha256']!=CATALOG_SHA256 or p['model']!='meta/muse-spark-1.3-contributor'
        or p['execution_authority'] is not False or p['profitability_proven'] is not False):
        raise ValueError('FAST_POLICY_SCOPE')
    for k in ('version','created_ms','expires_ms'):
        if type(p[k]) is not int or p[k]<=0:raise ValueError('FAST_POLICY_INTEGER')
    if not p['created_ms']<=now_ms<p['expires_ms']<=min(deadline_ms,p['created_ms']+3600000):
        raise ValueError('FAST_POLICY_EXPIRED')
    digest=p['response_sha256']
    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('FAST_POLICY_PROVENANCE')
    return p

def load(root,now_ms,deadline_ms):
    path=Path(root)/'fast-policy.json'
    if path.stat().st_size>4096:raise ValueError('FAST_POLICY_SIZE')
    p=validate(json.loads(path.read_bytes()),now_ms,deadline_ms)
    # Publication is allowed only after the same reserved call is cost-reconciled.
    import sqlite3
    # sqlite3's connection context manages transactions, not file lifetime.
    # Policy polling must close the handle even if validation subsequently fails.
    with closing(sqlite3.connect(f'file:{Path(root)/"model-budget.sqlite3"}?mode=ro',uri=True)) as db:
        row=db.execute('SELECT status,response_hash,actual FROM calls WHERE id=?',(p['version'],)).fetchone()
        breach=db.execute("SELECT 1 FROM calls WHERE status='cost_breach' LIMIT 1").fetchone()
    if breach or not row or row[0]!='valid' or row[1]!=p['response_sha256'] or row[2] is None:
        raise ValueError('FAST_POLICY_UNCONFIRMED_MODEL_CALL')
    return p

def feedback(root,now_ms):
    """A bounded numeric summary, never a model-authored success/PnL label."""
    path=Path(root)/'fast-status.json'
    if not path.exists():return None
    if path.stat().st_size>32768:raise ValueError('FAST_FEEDBACK_SIZE')
    value=json.loads(path.read_bytes())
    if value.get('schema')!='economic-machine.pump-fast-status.v1' or value.get('execution_authority') is not False:
        raise ValueError('FAST_FEEDBACK_SCHEMA')
    def number(n):
        if type(n) is not int or not 0<=n<10**15:raise ValueError('FAST_FEEDBACK_NUMBER')
        return n
    stamp=number(value['updated_ms']);version=number(value['policy_version'])
    if stamp>now_ms:raise ValueError('FAST_FEEDBACK_FUTURE')
    from .fast_native import REASONS
    allowed=('frames','pump_transactions','launches_admitted','native_events','decisions','late_after_decision','dead_slot','stale_source_frame','missing_trade_event','decode_error')
    return dict(policy_version=version,age_ms=now_ms-stamp,stale=now_ms-stamp>600000,
                counts={k:number(value.get('counts',{}).get(k,0)) for k in allowed},
                reasons={k:number(value.get('reasons',{}).get(k,0)) for k in REASONS},
                observations_are_not_outcomes=True,profitability_proven=False,execution_authority=False)
