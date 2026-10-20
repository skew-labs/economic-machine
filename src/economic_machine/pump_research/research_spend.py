"""Owner's KRW monthly ceiling, shared by models and private Blob work.

Admission estimates are deliberately above retained retail rates; they are not
an Azure invoice or a provider-enforced billing cap. Unknown attempts stay
reserved. Persistent storage is charged for a full month on every admission.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .store import atomic, canonical

FOLDER='evidence/research-budget-v1'
GIB=1024**3
KST=timezone(timedelta(hours=9))

def month(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,KST).strftime('%Y-%m')

def ceiling(value):
    return int(Decimal(value).to_integral_value(rounding=ROUND_CEILING))

def build_grant(base, approved_ms=None):
    base=Path(base)
    from .research_continuation import authority as continuation_authority
    if continuation_authority(base) is None:raise ValueError('RESEARCH_BUDGET_PARENT_REQUIRED')
    parent=base/'evidence/solana-continuation-v1/grant.json'
    return dict(schema='economic-machine.research-monthly-budget.v1',
        authorization='owner_latest_monthly_one_million_krw',
        approved_ms=approved_ms or time.time_ns()//1000000,currency='KRW',monthly_limit=1000000,
        replaces_earlier_usd1500_budget=True,month_timezone='Asia/Seoul',
        parent_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),
        monthly_other_cost_reserve_krw=200000,storage_krw_per_gib_month=60,
        internet_read_krw_per_gib=200,operation_krw=1,model_usd_to_krw_reserve=2000,
        maximum_cloud_bytes=4*1024**4,local_storage_bytes=224*GIB,minimum_host_free_bytes=128*GIB,
        account='tickingstorage',container='ticking-cold',object_prefix='research/pump/sha256',
        verified_duplicate_retirement=True,cloud_object_deletion=False,
        production_collector_changes=False,public_deployment=False,trading=False,
        new_servers=False,paid_data_api=False)

def authority(base):
    base=Path(base);path=base/FOLDER/'grant.json'
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>65536:raise ValueError('RESEARCH_BUDGET_GRANT_FILE')
    raw=path.read_bytes();g=json.loads(raw);h=hashlib.sha256(raw).hexdigest()
    if (path.with_suffix('.sha256').read_text().strip()!=h or type(g.get('approved_ms')) is not int
        or g['approved_ms']>time.time_ns()//1000000 or g!=build_grant(base,g['approved_ms'])):
        raise ValueError('RESEARCH_BUDGET_AUTHORITY')
    return g,h

class Spend:
    def __init__(self,base):
        self.base=Path(base);loaded=authority(base)
        if not loaded:raise ValueError('RESEARCH_BUDGET_REQUIRED')
        self.grant,self.hash=loaded;self.out=self.base/FOLDER
        self.db=sqlite3.connect(self.out/'spend.sqlite3',timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY,month TEXT,kind TEXT,
            key TEXT,krw INTEGER,body TEXT,UNIQUE(kind,key));
          CREATE TABLE IF NOT EXISTS objects(digest TEXT PRIMARY KEY,bytes INTEGER,verified INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS model_baseline(month TEXT PRIMARY KEY,krw INTEGER);''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old=self.db.execute('SELECT hash FROM identity').fetchall()
            if old and old!=[(self.hash,)]:raise ValueError('RESEARCH_BUDGET_CHANGED')
            self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(self.hash,))

    def close(self):self.db.close()

    def baseline_models(self):
        """Import prior calls once; new calls are reserved here before dispatch."""
        current=month()
        if self.db.execute('SELECT 1 FROM model_baseline WHERE month=?',(current,)).fetchone():return
        path=self.base/'evidence/wallet-map-v1/model.sqlite3';value=Decimal(0)
        if path.exists():
            with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
                rows=db.execute('SELECT request_hash,reserved,actual,created_ms FROM calls').fetchall()
            tracked={r[0] for r in self.db.execute("SELECT key FROM operations WHERE kind='model'")}
            for key,reserved,actual,stamp in rows:
                if month(stamp/1000)==current and key not in tracked:value+=Decimal(actual if actual is not None else reserved)
        with self.db:self.db.execute('INSERT OR IGNORE INTO model_baseline VALUES(?,?)',
            (current,ceiling(value*self.grant['model_usd_to_krw_reserve'])))

    def totals(self, extra_bytes=0, extra_krw=0):
        g=self.grant;current=month();size=self.db.execute('SELECT coalesce(sum(bytes),0) FROM objects').fetchone()[0]+extra_bytes
        operations=self.db.execute('SELECT coalesce(sum(krw),0) FROM operations WHERE month=?',(current,)).fetchone()[0]
        baseline=self.db.execute('SELECT krw FROM model_baseline WHERE month=?',(current,)).fetchone()
        storage=ceiling(Decimal(size)*g['storage_krw_per_gib_month']/GIB)
        total=storage+operations+(baseline[0] if baseline else 0)+g['monthly_other_cost_reserve_krw']+extra_krw
        return dict(month=current,monthly_limit_krw=g['monthly_limit'],admitted_estimate_krw=total,
            storage_full_month_reserve_krw=storage,operations_and_models_krw=operations,
            prior_model_reserve_krw=baseline[0] if baseline else 0,
            other_cost_reserve_krw=g['monthly_other_cost_reserve_krw'],cloud_reserved_bytes=size,
            remaining_krw=max(0,g['monthly_limit']-total),actual_invoice=False)

    def reserve(self,kind,key,*,bytes_=0,digest=None,usd=None):
        if kind not in ('upload','read','model') or not isinstance(key,str) or not 0<len(key)<=180:
            raise ValueError('RESEARCH_SPEND_REQUEST')
        if type(bytes_) is not int or not 0<=bytes_<=64*1024**2:raise ValueError('RESEARCH_SPEND_BYTES')
        if kind=='model':
            value=Decimal(usd)
            if not value.is_finite() or value<0:raise ValueError('RESEARCH_MODEL_COST')
            cost=ceiling(value*self.grant['model_usd_to_krw_reserve'])
        else:
            cost=ceiling(Decimal(bytes_)*self.grant['internet_read_krw_per_gib']/GIB)+(2 if kind=='upload' else 1)*self.grant['operation_krw']
        self.baseline_models()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            prior=self.db.execute('SELECT body FROM operations WHERE kind=? AND key=?',(kind,key)).fetchone()
            body=canonical(dict(bytes=bytes_,digest=digest,usd=str(usd) if usd is not None else None))
            if prior:
                if prior[0]!=body:raise ValueError('RESEARCH_SPEND_IDENTITY')
                return
            extra=0
            if kind=='upload':
                import re
                if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('RESEARCH_OBJECT_HASH')
                old=self.db.execute('SELECT bytes FROM objects WHERE digest=?',(digest,)).fetchone()
                if old and old[0]!=bytes_:raise ValueError('RESEARCH_OBJECT_SIZE')
                extra=0 if old else bytes_
            t=self.totals(extra,cost)
            if t['cloud_reserved_bytes']>self.grant['maximum_cloud_bytes']:raise ValueError('RESEARCH_CLOUD_CAPACITY_LIMIT')
            if t['admitted_estimate_krw']>self.grant['monthly_limit']:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
            if kind=='upload':self.db.execute('INSERT OR IGNORE INTO objects(digest,bytes) VALUES(?,?)',(digest,bytes_))
            self.db.execute('INSERT INTO operations(month,kind,key,krw,body) VALUES(?,?,?,?,?)',(month(),kind,key,cost,body))

    def model_finish(self,key,actual):
        if actual is None:return
        value=Decimal(actual)
        if not value.is_finite() or value<0:raise ValueError('RESEARCH_MODEL_COST')
        with self.db:
            self.db.execute("UPDATE operations SET krw=? WHERE kind='model' AND key=?",
                (ceiling(value*self.grant['model_usd_to_krw_reserve']),key))

    def verified(self,digest):
        with self.db:self.db.execute('UPDATE objects SET verified=1 WHERE digest=?',(digest,))

    def publish(self):atomic(self.out/'status.json',self.totals())
