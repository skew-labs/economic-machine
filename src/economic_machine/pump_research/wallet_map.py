"""Temporal forward associations from original launch participants.

Addresses are public accounts, not identified people. A path is not proof that
the same owner or the same units of money moved along it. Missing history is
published explicitly. Model output selects only supplied frontier job IDs.
"""
import argparse
from contextlib import closing
from datetime import datetime
from decimal import Decimal,InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
import urllib.error

from .store import Store,atomic,canonical,decode,encode,address
from .history_core import Scope
from ..muse_maker import MODEL,unique
from ..muse_policy_worker import Budget,provider_call
from .archive import Archive,get,BASE
from .address_index import AddressIndex,read_indexed_slot,parse_index_block,transfer_commit_status
from .precision import order_bound,owner_evidence
from .research_quality import audit,event_key,annotate_source_identity
from .custody import Custody
from .crosschain import CrossChain
from .raw_history import ScopedArchive,RawHistory,ArchiveDeferred
from .portal_history import PortalHistory,PortalDeferred
from .investigation_menu import build_menu,compact_cursor,supplement_selection
from .research_workspace import Workspace
from .launch_resolver import LaunchResolver
from .gsfa_engine import GsfaEngine
from .commit_graph import latest_assessments,edge_requires_commit,assessment_snapshot
from .commit_maintenance import review_retained
from .coverage_projection import sync_retained,close_retained
from .token_owner_trace import seed_owner_accounts
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED

START_MS=int(datetime.fromisoformat('2024-01-19T00:00:00+09:00').timestamp()*1000)
END_MS=int(datetime.fromisoformat('2026-10-15T23:59:59.999+09:00').timestamp()*1000)


def read_db(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=2)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-16384')
    return db


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


class UncappedMuse(Budget):
    """Explicit owner scope. No total cost/call ceiling; durable cost accounting."""
    def __init__(self,path,scope,continuation=None):
        if (scope.get('maximum_usd') is not None or scope.get('max_calls') is not None or
            scope.get('model')!=MODEL or scope.get('trade_authority') is not False or
            scope.get('authorization')!='owner_unlimited_muse_top10_forward_wallet_research'):
            raise ValueError('MAP_MODEL_SCOPE')
        self.continuation=continuation
        self.effective_deadline_ms=scope['deadline_ms']
        if continuation is not None:
            continuation.guard()
            original=json.loads((continuation.base/'evidence/wallet-map-v1/model-scope.json').read_bytes())
            if canonical(original)!=canonical(scope):raise ValueError('MAP_CONTINUATION_MODEL_SCOPE')
            self.effective_deadline_ms=continuation.effective_deadline_ms
        self.scope=scope;self.db=sqlite3.connect(path)
        from .research_spend import authority as spend_authority, Spend
        self.spend=Spend(continuation.base) if continuation and spend_authority(continuation.base) else None
        if self.spend:self.spend.baseline_models()
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS scope(id INTEGER PRIMARY KEY,body TEXT); CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,request_hash TEXT UNIQUE,reserved TEXT,actual TEXT,status TEXT,response_hash TEXT,created_ms INTEGER);')
        with self.db:self.db.execute('INSERT OR IGNORE INTO scope VALUES(1,?)',(canonical(scope),))
        if self.db.execute('SELECT body FROM scope WHERE id=1').fetchone()[0]!=canonical(scope):raise ValueError('MAP_SCOPE_CHANGED')
    def reserve(self,req,now):
        if self.db.execute("SELECT 1 FROM calls WHERE status='cost_breach' LIMIT 1").fetchone():
            raise ValueError('RESEARCH_MODEL_COST_BREACH')
        body=canonical(req).encode()
        if len(body)>16000 or req.get('model')!=MODEL or req.get('max_tokens')!=4096:raise ValueError('MAP_MODEL_REQUEST_BOUND')
        if self.continuation:self.continuation.guard()
        if self.effective_deadline_ms is not None and now>=self.effective_deadline_ms:raise ValueError('MAP_RESEARCH_DEADLINE')
        bound=Decimal(len(body)+1024)*Decimal('0.0000001')+Decimal(4096)*Decimal('0.0000002')
        if self.spend:self.spend.reserve('model',hashlib.sha256(body).hexdigest(),usd=bound)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.continuation:self.continuation.guard()
            return self.db.execute('INSERT INTO calls(request_hash,reserved,status,created_ms) VALUES(?,?,?,?)',
                (hashlib.sha256(body).hexdigest(),str(bound),'reserved',now)).lastrowid
    def totals(self):
        value=super().totals()
        value.update(responses_received=self.db.execute('SELECT count(*) FROM calls WHERE response_hash IS NOT NULL').fetchone()[0],
                     accepted_responses=self.db.execute("SELECT count(*) FROM calls WHERE status='valid'").fetchone()[0])
        if self.spend:value['monthly_research_budget']=self.spend.totals()
        return value
    def finish(self,ident,raw,accepted):
        try:
            result=super().finish(ident,raw,accepted)
            return result
        except (ValueError,TypeError,AttributeError,InvalidOperation):
            # An invalid received envelope is not an unknown network outcome.
            # Keep its cost reserved, while recording receipt identity.
            with self.db:
                self.db.execute("UPDATE calls SET status='received_rejected_cost_unknown',response_hash=? WHERE id=? AND status='reserved'",
                    (hashlib.sha256(raw.encode()).hexdigest(),ident))
            raise ValueError('MAP_MODEL_RECEIVED_ENVELOPE_INVALID') from None
        finally:
            if self.spend:
                row=self.db.execute('SELECT request_hash,actual FROM calls WHERE id=?',(ident,)).fetchone()
                if row:self.spend.model_finish(*row)
                self.spend.publish()


class Graph:
    def __init__(self,base):
        self.base=Path(base);self.out=self.base/'evidence/wallet-map-v1';self.out.mkdir(exist_ok=True)
        self.db=sqlite3.connect(self.out/'graph.sqlite3');self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA cache_size=-16384')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS roots(mint TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,kind TEXT,roles TEXT);
          CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY,root TEXT,source TEXT,target TEXT,kind TEXT,slot INTEGER,stamp INTEGER,body TEXT);
          CREATE INDEX IF NOT EXISTS edge_root ON edges(root);
          CREATE INDEX IF NOT EXISTS edge_source ON edges(source);
          CREATE INDEX IF NOT EXISTS edge_target ON edges(target);
          CREATE TABLE IF NOT EXISTS frontier(id TEXT PRIMARY KEY,root TEXT,wallet TEXT,slot INTEGER,stamp INTEGER,depth INTEGER,state TEXT,cursor TEXT,source TEXT);
          CREATE TABLE IF NOT EXISTS investigations(id INTEGER PRIMARY KEY,call_id INTEGER,body TEXT,at INTEGER);
          CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS local_activity(wallet TEXT,mint TEXT,action TEXT,slot INTEGER,ix INTEGER,tx_order INTEGER,signature TEXT,finality TEXT,evidence TEXT);
          CREATE INDEX IF NOT EXISTS actor_lookup ON local_activity(wallet,slot);
          CREATE TABLE IF NOT EXISTS frontier_anchors(job TEXT PRIMARY KEY,slot INTEGER,tx_order INTEGER,ix INTEGER);
          CREATE TABLE IF NOT EXISTS precision_cursor_history(job TEXT PRIMARY KEY,state TEXT,cursor TEXT,at INTEGER);
          CREATE INDEX IF NOT EXISTS frontier_root_state ON frontier(root,state,source,depth);
        ''')
        self.custody=Custody(self)
        self.crosschain=CrossChain(self)
        self.raw_history=RawHistory(self)
        self.portal=PortalHistory(self)
        self.launch_resolver=LaunchResolver(self)
        self.gsfa=GsfaEngine(self)
    def close(self):
        try:close_retained(self)
        finally:self.db.close()
    def state(self,k,default=None):
        row=self.db.execute('SELECT body FROM state WHERE key=?',(k,)).fetchone();return json.loads(row[0]) if row else default
    def set(self,k,v):self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(k,canonical(v)))
    def node(self,ident,kind,role):
        if not address(ident):return
        old=self.db.execute('SELECT roles FROM nodes WHERE id=?',(ident,)).fetchone()
        roles=set(json.loads(old[0]) if old else []);roles.add(role)
        self.db.execute('INSERT INTO nodes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET roles=excluded.roles',(ident,kind,canonical(sorted(roles))))
    def edge(self,root,src,dst,kind,slot,stamp,**evidence):
        if not all(address(k) for k in (root,src,dst)):return None
        ident=digest(dict(root=root,source=src,target=dst,kind=kind,slot=slot,stamp=stamp,evidence=evidence))
        self.db.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?,?,?,?)',(ident,root,src,dst,kind,slot,stamp,canonical(evidence)))
        self.custody.observe(ident,root,src,dst,kind,slot,stamp,evidence)
        return ident
    def enqueue(self,root,wallet,slot,stamp,depth,source='local_forward',after_order=None):
        if not address(root) or not address(wallet):return None
        ident=digest([root,wallet,slot,stamp,source,after_order])[:24]
        cursor=canonical([slot,*after_order]) if after_order is not None and source=='local_forward' else 'null'
        self.db.execute('INSERT OR IGNORE INTO frontier VALUES(?,?,?,?,?,?,?,?,?)',(ident,root,wallet,slot,stamp,depth,'pending',cursor,source))
        if after_order is not None:
            self.db.execute('INSERT OR IGNORE INTO frontier_anchors VALUES(?,?,?,?)',(ident,slot,*after_order))
        return self.custody.attach(ident)

    def anchor(self,job):
        r=self.db.execute('SELECT * FROM frontier_anchors WHERE job=?',(job['id'],)).fetchone()
        return dict(r) if r else None

    def migrate_precision(self):
        if self.state('precision_migration_v1'):return
        # A cursor is mutable. Recover an old anchor ONLY when its original
        # identity hash proves it has not advanced; never guess from latest row.
        recovered=0;reset=0
        with self.db:
            for j in self.db.execute("SELECT * FROM frontier WHERE source='local_forward'").fetchall():
                c=json.loads(j['cursor'])
                if not isinstance(c,list) or len(c)!=3 or c[0]!=j['slot']:continue
                if digest([j['root'],j['wallet'],j['slot'],j['stamp'],j['source'],c[1:]])[:24]!=j['id']:continue
                self.db.execute('INSERT OR IGNORE INTO frontier_anchors VALUES(?,?,?,?)',(j['id'],*c));recovered+=1
            for j in self.db.execute("SELECT * FROM frontier WHERE source='indexed_forward' AND state IN ('pending','indexed_source_exhausted')").fetchall():
                a=self.db.execute('''SELECT a.* FROM frontier f JOIN frontier_anchors a ON a.job=f.id
                  WHERE f.source='local_forward' AND f.root=? AND f.wallet=? AND f.slot IS ? AND f.stamp IS ?
                  ORDER BY a.tx_order,a.ix LIMIT 1''',(j['root'],j['wallet'],j['slot'],j['stamp'])).fetchone()
                if a:self.db.execute('INSERT OR IGNORE INTO frontier_anchors VALUES(?,?,?,?)',(j['id'],a['slot'],a['tx_order'],a['ix']))
                self.db.execute('INSERT OR IGNORE INTO precision_cursor_history VALUES(?,?,?,?)',(j['id'],j['state'],j['cursor'],time.time_ns()//1000000))
                self.db.execute("UPDATE frontier SET state='pending',cursor='null' WHERE id=?",(j['id'],));reset+=1
            self.set('precision_migration_v1',dict(recovered_anchors=recovered,requeued_indexed_jobs=reset,prior_cursors_preserved=True))

    def recover_original_anchors(self,scope):
        if self.state('precision_anchor_recovery_v2'):return
        path=self.out/'address-index.sqlite3'
        if not path.exists():return
        recovered=0;reset=0
        with closing(read_db(path)) as db,self.db:
            jobs=self.db.execute("SELECT f.* FROM frontier f LEFT JOIN frontier_anchors a ON a.job=f.id WHERE f.source='local_forward' AND f.slot IS NOT NULL AND a.job IS NULL").fetchall()
            for j in jobs:
                # No-order jobs deliberately exclude same-slot actions. An
                # immutable job hash distinguishes them from advanced cursors.
                if digest([j['root'],j['wallet'],j['slot'],j['stamp'],j['source'],None])[:24]==j['id']:continue
                scope.guard();key=db.execute('SELECT id FROM keys WHERE key=?',(decode(j['wallet']),)).fetchone()
                if not key:continue
                candidates=db.execute('''SELECT t.tx_order,f.ix FROM transfers f JOIN transactions t ON t.id=f.tx
                    JOIN blocks b ON b.id=t.block WHERE f.destination=? AND t.slot=? AND b.state!='dead_branch'
                    UNION SELECT t.tx_order,a.ix FROM activity a JOIN transactions t ON t.id=a.tx
                    JOIN blocks b ON b.id=t.block WHERE a.wallet=? AND t.slot=? AND b.state!='dead_branch' ''',
                    (key[0],j['slot'],key[0],j['slot']))
                for order,ix in candidates:
                    if digest([j['root'],j['wallet'],j['slot'],j['stamp'],j['source'],[order,ix]])[:24]!=j['id']:continue
                    self.db.execute('INSERT INTO frontier_anchors VALUES(?,?,?,?)',(j['id'],j['slot'],order,ix));recovered+=1;break
            for j in self.db.execute("SELECT f.* FROM frontier f LEFT JOIN frontier_anchors a ON a.job=f.id WHERE f.source='indexed_forward' AND a.job IS NULL AND f.state IN ('pending','indexed_source_exhausted')").fetchall():
                a=self.db.execute('''SELECT a.* FROM frontier f JOIN frontier_anchors a ON a.job=f.id
                    WHERE f.source='local_forward' AND f.root=? AND f.wallet=? AND f.slot IS ? AND f.stamp IS ?
                    ORDER BY a.tx_order,a.ix LIMIT 1''',(j['root'],j['wallet'],j['slot'],j['stamp'])).fetchone()
                if not a:continue
                self.set('precision_prior_cursor_v2:'+j['id'],dict(state=j['state'],cursor=j['cursor']))
                self.db.execute('INSERT INTO frontier_anchors VALUES(?,?,?,?)',(j['id'],a['slot'],a['tx_order'],a['ix']))
                self.db.execute("UPDATE frontier SET state='pending',cursor='null' WHERE id=?",(j['id'],));reset+=1
            self.set('precision_anchor_recovery_v2',dict(recovered_from_original_identity=recovered,requeued_indexed_jobs=reset))

    def retry_source_errors(self,now):
        with self.db:
            for j in self.db.execute("SELECT id FROM frontier WHERE state='source_blocked'").fetchall():
                error=self.state('source_error:'+j['id'],{})
                transient=error.get('retryable') or error.get('error_type')=='TimeoutError' or any(error.get('error','').startswith('HTTP Error '+str(c)+':') for c in (429,502,503,504))
                if not transient:continue
                if 'retry_at_ms' not in error:
                    error.update(retry_at_ms=now+60000,retryable=True);self.set('source_error:'+j['id'],error)
                elif now>=error['retry_at_ms']:
                    self.db.execute("UPDATE frontier SET state='pending' WHERE id=?",(j['id'],))

    def seed(self):
        with closing(read_db(self.base/'data/search.sqlite3')) as old:
            if self.db.execute('SELECT count(*) FROM roots').fetchone()[0]:return
            candidates=old.execute("SELECT * FROM candidates WHERE ath_status='provider_claim_unverified' AND created_ms>=? ORDER BY provider_rank LIMIT 10",(START_MS,)).fetchall()
            with self.db:
                for c in candidates:
                    c=dict(c);mint=c['mint'];self.db.execute('INSERT INTO roots VALUES(?,?)',(mint,canonical(c)))
                    self.node(mint,'token','original_launch');self.node(c['creator'],'account','provider_creator_unverified')
                    self.edge(mint,c['creator'],mint,'provider_creator_claim',None,c['created_ms'],source='original_provider_snapshot',chain_verified=False,ath_verified=False)
                    launch=old.execute("SELECT l.*,o.signature,o.tx_order,o.finality,o.evidence FROM launches l JOIN observations o ON o.id=l.observation WHERE l.mint=? AND o.finality!='dead_branch' ORDER BY l.slot,o.tx_order,l.ix LIMIT 1",(mint,)).fetchone()
                    if launch:
                        who=launch['creator'] if address(launch['creator']) else launch['payer']
                        self.node(who,'account','observed_creator' if address(launch['creator']) else 'observed_launch_payer')
                        self.edge(mint,who,mint,'create' if address(launch['creator']) else 'launch_payer',launch['slot'],None,signature=launch['signature'],ix=launch['ix'],finality=launch['finality'],evidence=launch['evidence'],timestamp_unknown=True,creator_field_present=address(launch['creator']))
                        for wallet in set((launch['creator'],launch['payer'])):
                            self.node(wallet,'account','launch_participant')
                            self.enqueue(mint,wallet,launch['slot'],c['created_ms'],0,after_order=(launch['tx_order'],launch['ix']))
                        early=old.execute("SELECT a.*,o.signature,o.tx_order,o.finality,o.evidence FROM activity a JOIN observations o ON o.id=a.observation WHERE a.mint=? AND (a.slot,o.tx_order,a.ix)>(?,?,?) AND a.slot<? AND a.action='buy' AND o.finality!='dead_branch' ORDER BY a.slot,o.tx_order,a.ix",(mint,launch['slot'],launch['tx_order'],launch['ix'],launch['slot']+150)).fetchall()
                        for row in early:
                            self.node(row['wallet'],'account','observed_early_buyer')
                            self.edge(mint,row['wallet'],mint,'early_buy',row['slot'],None,signature=row['signature'],ix=row['ix'],finality=row['finality'],evidence=row['evidence'],timestamp_unknown=True,bundle_membership_proven=False)
                            self.enqueue(mint,row['wallet'],row['slot'],c['created_ms'],0,after_order=(row['tx_order'],row['ix']))
                    else:
                        self.set('missing_launch:'+mint,True)
                        self.enqueue(mint,c['creator'],None,c['created_ms'],0,'recover_launch')
                for r in old.execute("SELECT a.wallet,a.mint,a.action,a.slot,a.ix,o.tx_order,o.signature,o.finality,o.evidence FROM activity a JOIN observations o ON o.id=a.observation WHERE o.finality!='dead_branch'"):
                    self.db.execute('INSERT INTO local_activity VALUES(?,?,?,?,?,?,?,?,?)',tuple(r))
                for r in old.execute("SELECT l.creator,l.mint,'create',l.slot,l.ix,o.tx_order,o.signature,o.finality,o.evidence FROM launches l JOIN observations o ON o.id=l.observation WHERE o.finality!='dead_branch'"):
                    self.db.execute('INSERT INTO local_activity VALUES(?,?,?,?,?,?,?,?,?)',tuple(r))
                self.set('seeded_ms',time.time_ns()//1000000)

    def reconcile_launch_evidence(self,scope):
        if self.state('launch_accounts_reconciled_v2'):return
        from .cli import proto
        from .object_store import read
        _,meta=proto('/srv/economic-machine/collector')
        index=AddressIndex(self.out/'address-index.sqlite3')
        try:
            with closing(read_db(self.base/'data/search.sqlite3')) as old:
                for root in self.db.execute('SELECT mint FROM roots').fetchall():
                    row=old.execute('''SELECT l.*,o.signature,o.tx_order,o.finality,e.hash FROM launches l
                        JOIN observations o ON o.id=l.observation JOIN evidence e ON e.id=o.evidence
                        WHERE l.mint=? AND o.source='old-faithful' AND o.finality!='dead_branch'
                        ORDER BY l.slot,o.tx_order,l.ix LIMIT 1''',(root[0],)).fetchone()
                    if not row:continue
                    scope.guard(32*1024**2)
                    raw=read(self.base/'data',row['hash']);block=parse_index_block(raw,row['slot'],meta)
                    index.ingest(block,dict(car_sha256=row['hash'],source='reconstructed_public_block',
                        locator='original_research_object:'+row['hash'],transaction_signatures_verified=True,independent_consensus_verified=False))
                    who=row['creator'] if address(row['creator']) else row['payer']
                    with self.db:
                        self.node(who,'account','observed_creator' if address(row['creator']) else 'observed_launch_payer')
                        self.edge(root[0],who,root[0],'create' if address(row['creator']) else 'launch_payer',row['slot'],block['time']*1000,
                            signature=row['signature'],ix=row['ix'],source='reconstructed_public_block',source_sha256=row['hash'],creator_field_present=address(row['creator']),independent_consensus_verified=False)
                        self.db.execute('UPDATE edges SET stamp=? WHERE slot=? AND stamp IS NULL',(block['time']*1000,row['slot']))
            with self.db:
                self.db.execute("UPDATE edges SET body=json_set(body,'$.invalid_address',1) WHERE source='' OR target=''")
                self.db.execute("UPDATE frontier SET state='invalid_address' WHERE wallet=''")
                self.set('launch_accounts_reconciled_v2',True)
        finally:index.close()

    def local_forward(self,job):
        cursor=json.loads(job['cursor']);floor,_,_=order_bound(self.anchor(job),job['slot'],job['stamp'])
        if job['slot'] is None:return dict(job_id=job['id'],waiting_for='slot_anchor',source_complete=False)
        start=tuple(cursor) if cursor else floor
        with closing(read_db(self.base/'data/search.sqlite3')) as old:
            old.set_progress_handler(lambda:0,100000)
            rows=old.execute('''SELECT t.*,o.signature,o.tx_order,o.finality,o.evidence
                FROM transfers t JOIN observations o ON o.id=t.observation
                WHERE t.source=? AND t.slot>=? AND t.mint='SOL' AND t.kind='system_transfer'
                AND o.finality!='dead_branch' AND (t.slot,o.tx_order,t.ix)>(?,?,?)
                ORDER BY t.slot,o.tx_order,t.ix LIMIT 200''',(job['wallet'],start[0],*start)).fetchall()
        with self.db:
            for r in rows:
                if int(r['amount'])<=0:continue
                self.node(r['destination'],'account','forward_recipient')
                self.edge(job['root'],r['source'],r['destination'],'sol_transfer',r['slot'],None,
                    signature=r['signature'],ix=r['ix'],amount_lamports=r['amount'],finality=r['finality'],evidence=r['evidence'],timestamp_unknown=True,same_owner_proven=False)
                self.enqueue(job['root'],r['destination'],r['slot'],job['stamp'],job['depth']+1,after_order=(r['tx_order'],r['ix']))
            for r in self.db.execute('SELECT * FROM local_activity WHERE wallet=? AND (slot,tx_order,ix)>(?,?,?) ORDER BY slot,tx_order,ix',(job['wallet'],*start)).fetchall():
                self.node(r['mint'],'token','later_observed_token')
                self.edge(job['root'],r['wallet'],r['mint'],'later_'+r['action'],r['slot'],None,
                    signature=r['signature'],ix=r['ix'],finality=r['finality'],evidence=r['evidence'],timestamp_unknown=True,bundle_membership_proven=False)
            # Exhausted means this local source page, never complete wallet history.
            next_cursor=canonical([rows[-1]['slot'],rows[-1]['tx_order'],rows[-1]['ix']]) if rows else job['cursor']
            self.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',(next_cursor,'pending' if len(rows)==200 else 'local_source_exhausted',job['id']))
        return dict(job_id=job['id'],observed_transfers=len(rows),source_complete=False)

    def add_core(self,scope):
        core=self.base/'evidence/core-trace-v1'
        meta_path=core/'token_metadata.sqlite3';fund_path=core/'arkm_funding.sqlite3'
        if not meta_path.exists():return
        # Process original participants first. Supplemental accounts are queued
        # as indexed core sources become available, without changing old tables.
        if not self.state('core_roots'):
            with closing(read_db(meta_path)) as db, self.db:
                for r in self.db.execute('SELECT body FROM roots').fetchall():
                    root=json.loads(r[0]);m=decode(root['mint'])
                    for row in db.execute('SELECT rowid,* FROM projection WHERE mint=?',(m,)):
                        if row['creator'] is None:continue
                        who=encode(row['creator']);self.node(who,'account','dataset_creator')
                        stamp=row['first_seen']*1000 if row['first_seen'] is not None else root['created_ms']
                        self.edge(root['mint'],who,root['mint'],'dataset_create',None,stamp,source='core_token_metadata',source_rowid=row['rowid'],independently_chain_verified=False)
                        self.enqueue(root['mint'],who,None,stamp,0,'core_forward')
                self.set('core_roots',True)
        if fund_path.exists():
            with self.db:
                last=self.state('derived_frontier_row',0)
                rows=self.db.execute("SELECT rowid,* FROM frontier WHERE source='local_forward' AND rowid>? ORDER BY rowid LIMIT 1000",(last,)).fetchall()
                for j in rows:
                    self.custody.inherit(j['id'],self.enqueue(j['root'],j['wallet'],None,j['stamp'],j['depth'],'core_forward'))
                    a=self.anchor(j)
                    self.custody.inherit(j['id'],self.enqueue(j['root'],j['wallet'],j['slot'],j['stamp'],j['depth'],'indexed_forward',after_order=(a['tx_order'],a['ix']) if a else None))
                    self.custody.inherit(j['id'],self.enqueue(j['root'],j['wallet'],j['slot'],j['stamp'],j['depth'],'core_participation'))
                if rows:self.set('derived_frontier_row',rows[-1]['rowid'])
        trades=core/'pump_event_default.sqlite3'
        if trades.exists() and not self.state('core_early_queued'):
            with closing(read_db(meta_path)) as db,self.db:
                for r in self.db.execute('SELECT body FROM roots').fetchall():
                    root=json.loads(r[0]);row=db.execute('SELECT min(first_seen) FROM projection WHERE mint=?',(decode(root['mint']),)).fetchone()
                    if row and row[0] is not None:self.enqueue(root['mint'],root['creator'],None,row[0]*1000,0,'core_early')
                self.set('core_early_queued',True)
        index_path=self.out/'address-index.sqlite3'
        if index_path.exists() and time.time()-self.state('index_refresh_at',0)>60:
            with closing(read_db(index_path)) as db,self.db:
                revision=db.execute('SELECT coalesce(max(id),0) FROM transactions').fetchone()[0]
                materialized=[revision,db.execute('SELECT coalesce(max(id),0) FROM transfers').fetchone()[0],
                    db.execute('SELECT coalesce(max(id),0) FROM activity').fetchone()[0]]
                if materialized!=self.state('index_materialized_revision',[]):
                    self.db.execute("UPDATE frontier SET state='pending' WHERE state='indexed_source_exhausted'")
                for row in db.execute("SELECT id FROM blocks WHERE state='dead_branch'"):
                    self.db.execute("UPDATE edges SET body=json_set(body,'$.dead_branch',1) WHERE json_extract(body,'$.index_block_id')=?",(row[0],))
                self.set('index_revision',revision);self.set('index_materialized_revision',materialized);self.set('index_refresh_at',time.time())

    def core_participation(self,job,scope):
        path=self.base/'evidence/core-trace-v1/pump_event_default.sqlite3'
        if not path.exists():return dict(waiting_for='indexed_trade_table')
        early=job['source']=='core_early';cursor=json.loads(job['cursor']) or 0
        with closing(read_db(path)) as db:
            db.set_progress_handler(lambda:(scope.guard() or 0),100000)
            column='mint' if early else 'trader';key=decode(job['root'] if early else job['wallet'])
            lower=(job['stamp'] or START_MS)//1000;upper=lower+60 if early else END_MS//1000
            # Event time cannot order distinct transactions within a second.
            rows=db.execute('SELECT rowid,* FROM projection WHERE '+column+'=? AND rowid>? AND stamp'+('>=' if early else '>')+'? AND stamp<? ORDER BY rowid LIMIT 200',
                (key,cursor,lower,upper)).fetchall()
        with self.db:
            for r in rows:
                if r['trader'] is None or r['mint'] is None or (early and r['side']!='buy'):continue
                who,mint=encode(r['trader']),encode(r['mint']);stamp=r['stamp']*1000
                self.node(who,'account','dataset_early_buyer' if early else 'dataset_participant');self.node(mint,'token','dataset_participation')
                self.edge(job['root'],who,mint,'dataset_early_buy' if early else 'later_dataset_'+r['side'],r['slot'],stamp,
                    source='core_pump_event_default',source_rowid=r['rowid'],source_is_sampled=True,
                    parse_version=r['parse_version'],outer_ix=r['outer_ix'],inner_ix=r['inner_ix'],ordinal=r['ordinal'],
                    sol_raw=r['sol'],token_raw=r['tokens'],decimals=r['decimals'],distinct_trade_not_established=True,
                    first_seen_window_not_proven_launch=early,signature_in_original_source=True,bundle_membership_proven=False)
                if early:
                    self.enqueue(job['root'],who,r['slot'],stamp,0,'local_forward')
                    self.enqueue(job['root'],who,None,stamp,0,'core_forward')
            self.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',(canonical(rows[-1]['rowid']) if rows else job['cursor'],
                'pending' if len(rows)==200 else 'sampled_activity_source_exhausted',job['id']))
        return dict(job_id=job['id'],sampled_activity_rows=len(rows),source_complete=False)

    def indexed_forward(self,job,scope):
        path=self.out/'address-index.sqlite3'
        if not path.exists():return dict(waiting_for='reconstructed_blocks')
        cursor=json.loads(job['cursor']) or {};cursor=cursor if isinstance(cursor,dict) else {'transfer':cursor}
        floor,after_ms,ordering=order_bound(self.anchor(job),job['slot'],job['stamp'])
        if ordering=='unanchored':
            with self.db:self.db.execute("UPDATE frontier SET state='anchor_required' WHERE id=?",(job['id'],))
            return dict(job_id=job['id'],state='anchor_required',source_complete=False)
        # Paging IDs are independent of the immutable event-time anchor. Late
        # historical backfills are admitted; earlier actions are not.
        time_clause=' AND t.stamp*1000>?' if after_ms is not None else ''
        time_args=(after_ms,) if after_ms is not None else ()
        end=min(time.time_ns()//1000000,END_MS)
        root=self.db.execute('SELECT body FROM roots WHERE mint=?',(job['root'],)).fetchone()
        wallet_root=bool(root and job['root']==job['wallet'] and json.loads(root[0]).get('root_kind')=='wallet')
        with closing(read_db(path)) as db:
            db.set_progress_handler(lambda:(scope.guard() or 0),100000)
            rows=db.execute('''SELECT f.*,s.key AS source_key,d.key AS destination_key,t.slot,t.tx_order,t.stamp,t.signature,b.hash,b.id AS block_id,b.state,b.receipt
                FROM keys s JOIN transfers f ON f.source=s.id JOIN keys d ON d.id=f.destination
                JOIN transactions t ON t.id=f.tx JOIN blocks b ON b.id=t.block
                WHERE s.key=? AND f.id>? AND (t.slot,t.tx_order,f.ix)>(?,?,?) AND b.state!='dead_branch'
                AND (t.stamp IS NULL OR t.stamp*1000<=?)'''+time_clause+' ORDER BY f.id LIMIT 200',
                (decode(job['wallet']),cursor.get('transfer',0),*floor,end,*time_args)).fetchall()
            incoming=[]
            if wallet_root:
                incoming=db.execute('''SELECT f.*,s.key AS source_key,d.key AS destination_key,t.slot,t.tx_order,t.stamp,t.signature,b.hash,b.id AS block_id,b.state,b.receipt
                    FROM keys d JOIN transfers f ON f.destination=d.id JOIN keys s ON s.id=f.source
                    JOIN transactions t ON t.id=f.tx JOIN blocks b ON b.id=t.block
                    WHERE d.key=? AND f.source!=f.destination AND f.id>? AND (t.slot,t.tx_order,f.ix)>(?,?,?) AND b.state!='dead_branch'
                    AND (t.stamp IS NULL OR t.stamp*1000<=?)'''+time_clause+' ORDER BY f.id LIMIT 200',
                    (decode(job['wallet']),cursor.get('incoming_transfer',0),*floor,end,*time_args)).fetchall()
            activity=db.execute('''SELECT a.*,t.slot,t.tx_order,t.stamp,t.signature,b.id AS block_id,b.hash,b.state,b.receipt
                FROM keys k JOIN activity a ON a.wallet=k.id JOIN transactions t ON t.id=a.tx JOIN blocks b ON b.id=t.block
                WHERE k.key=? AND a.id>? AND (t.slot,t.tx_order,a.ix)>(?,?,?) AND b.state!='dead_branch'
                AND (t.stamp IS NULL OR t.stamp*1000<=?)'''+time_clause+' ORDER BY a.id LIMIT 200',
                (decode(job['wallet']),cursor.get('activity',0),*floor,end,*time_args)).fetchall()
            owners={tid:[dict(e) for e in db.execute('SELECT account,phase,owner,mint,amount,decimals FROM token_owners WHERE tx=?',(tid,))]
                    for tid in {r['tx'] for r in [*rows,*incoming] if r['mint']!='SOL'}}
            commit_evidence={}
            for tid in {r['tx'] for r in [*rows,*incoming,*activity]}:
                record=db.execute('SELECT body FROM tx_commit_evidence WHERE tx=? ORDER BY version DESC LIMIT 1',(tid,)).fetchone()
                commit_evidence[tid]=json.loads(record[0]) if record else None
        omitted=emitted_transfers=emitted_incoming=emitted_activity=0
        with self.db:
            for r in [*rows,*incoming]:
                if transfer_commit_status(r['ix'],commit_evidence[r['tx']])!='committed':
                    omitted+=1;continue
                if int(r['amount'])<=0:continue
                emitted_transfers+=1
                sender=encode(r['source_key'])
                who=encode(r['destination_key']);stamp=r['stamp']*1000 if r['stamp'] is not None else None
                emitted_incoming+=sender!=job['wallet'] and who==job['wallet']
                self.node(sender,'account','indexed_transfer_sender')
                self.node(who,'account','indexed_transfer_recipient')
                owner={}
                if r['mint']!='SOL':
                    owner=dict(source_owner_evidence=owner_evidence(owners[r['tx']],sender,r['mint']),
                               destination_owner_evidence=owner_evidence(owners[r['tx']],who,r['mint']))
                self.edge(job['root'],sender,who,'indexed_transfer',r['slot'],stamp,signature=encode(r['signature']),
                    source=json.loads(r['receipt']).get('source','reconstructed_public_block'),source_sha256=r['hash'],index_block_id=r['block_id'],finality=r['state'],ix=r['ix'],amount=r['amount'],symbol=r['mint'],
                    transfer_kind=r['kind'],same_owner_proven=False,token_account_not_owner=r['mint']!='SOL',independent_consensus_verified=False,
                    precision_version=1,tx_order=r['tx_order'],ordering=ordering,anchor=list(floor),path_funds_continuity_proven=False,
                    instruction_commit_status='committed',instruction_commit_policy=1,
                    same_slot_fork_consistency_verified=False,**owner)
                if sender==job['wallet'] and who!=job['wallet']:
                    self.enqueue(job['root'],who,r['slot'],stamp,job['depth']+1,'local_forward',after_order=(r['tx_order'],r['ix']))
            for r in activity:
                if transfer_commit_status(r['ix'],commit_evidence[r['tx']])!='committed':
                    omitted+=1;continue
                emitted_activity+=1
                self.node(r['mint'],'token','later_observed_token')
                self.edge(job['root'],job['wallet'],r['mint'],'later_'+r['action'],r['slot'],r['stamp']*1000 if r['stamp'] is not None else None,
                    signature=encode(r['signature']),source=json.loads(r['receipt']).get('source','reconstructed_public_block'),index_block_id=r['block_id'],
                    source_sha256=r['hash'],finality=r['state'],ix=r['ix'],bundle_membership_proven=False,independent_consensus_verified=False,
                    precision_version=1,tx_order=r['tx_order'],ordering=ordering,anchor=list(floor),path_funds_continuity_proven=False,
                    instruction_commit_status='committed',instruction_commit_policy=1)
            if rows:cursor['transfer']=rows[-1]['id']
            if incoming:cursor['incoming_transfer']=incoming[-1]['id']
            if activity:cursor['activity']=activity[-1]['id']
            self.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',(canonical(cursor),
                'pending' if len(rows)==200 or len(incoming)==200 or len(activity)==200 else 'indexed_source_exhausted',job['id']))
        return dict(job_id=job['id'],indexed_transfers=emitted_transfers,indexed_incoming=emitted_incoming,indexed_activity=emitted_activity,
                    unverified_instruction_records_omitted=omitted,source_complete=False,
                    ordering=ordering,same_slot_after_anchor_supported=ordering=='recorded_instruction_order')

    def recover_launch(self,job,scope):
        """Recover actual create/early buy instructions by public sparse CAR reads.

        A new small projection is isolated from the original capped ledger.
        Requests reserve the same 12 GiB extension before dispatch. At least
        the remaining compressed core-table payload is left available.
        """
        from .cli import proto
        from .archive import parse_block
        import struct
        _,meta_type=proto('/srv/economic-machine/collector')
        search_index=AddressIndex(self.out/'address-index.sqlite3')
        sparse_root=self.out/'recovered';s=Store(sparse_root)
        reader=ScopedArchive(self,scope,s,meta_type,search_index,wait=False)
        try:
            cursor=json.loads(job['cursor']) or {}
            if 'start' not in cursor:
                # Reuse a previous completed timestamp search without touching
                # its failed job or resetting its network reservations.
                with closing(read_db(self.base/'data/search.sqlite3')) as old:
                    original=old.execute('SELECT args FROM jobs WHERE id=?',('launch:'+job['root'],)).fetchone()
                args=json.loads(original[0]) if original else {}
                provider_anchor=self.state('launch_anchor:'+job['root'])
                if provider_anchor:
                    # Timestamp-to-slot is only a lookup hint. Recovery below
                    # still requires the root mint in the actual CAR create.
                    start=provider_anchor['start'];end=provider_anchor['end']
                elif 'resolved_start' in args:
                    start=args['resolved_start'];end=args['resolved_end']
                else:
                    search=cursor.get('time_search',dict(low=240000000,high=456000000,samples=[]))
                    target=job['stamp']//1000
                    while search['high']-search['low']>4:
                        mid=(search['low']+search['high'])//2;found=None
                        for step in range(4):
                            cached=search_index.db.execute("SELECT slot,stamp FROM blocks WHERE slot=? AND state='decoded' AND stamp IS NOT NULL LIMIT 1",(mid+step,)).fetchone()
                            if cached:found=dict(slot=cached['slot'],time=cached['stamp'])
                            else:found,_=reader.read_slot(mid+step)
                            if found:break
                        if not found:
                            # A run of absent provider entries is not absent history.
                            probe=reader.next_indexed_slot(mid,min(search['high'],mid+4095))
                            if probe is None:
                                probe=reader.next_indexed_slot(max(search['low'],mid-4095),mid)
                            if probe is None:raise ValueError('MAP_TIME_SEARCH_MISSING_RANGE')
                            found,_=reader.read_slot(probe)
                            search.setdefault('index_gap_probes',[]).append(dict(mid=mid,found=probe,provider_absence_not_consensus=True))
                        if not found or found['time'] is None:raise ValueError('MAP_TIME_SEARCH_MISSING_TIMESTAMP')
                        search['samples'].append([found['slot'],found['time']])
                        if found['time']<target:search['low']=found['slot']+1
                        else:search['high']=min(mid,found['slot'])
                        cursor['time_search']=search
                        with self.db:self.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical(cursor),job['id']))
                    start=search['low']-8;end=search['low']+8
                cursor=dict(start=start,end=end,next=start)
                with self.db:self.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical(cursor),job['id']))
            # A page is four blocks, allowing the model to interleave roots.
            count=0
            while cursor['next']<=cursor['end'] and count<4:
                scope.guard(32*1024**2,force=True)
                slot=cursor['next'];reader.ingest_slot(slot);cursor['next']+=1;count+=1
                with self.db:self.db.execute('UPDATE frontier SET cursor=? WHERE id=?',(canonical(cursor),job['id']))
            if cursor['next']>cursor['end']:
                launch=s.db.execute("SELECT l.*,o.signature,o.tx_order,o.finality,o.evidence FROM launches l JOIN observations o ON o.id=l.observation WHERE l.mint=? ORDER BY l.slot,o.tx_order,l.ix LIMIT 1",(job['root'],)).fetchone()
                with self.db:
                    if launch:
                        who=launch['creator'] if address(launch['creator']) else launch['payer']
                        self.node(who,'account','observed_creator' if address(launch['creator']) else 'observed_launch_payer')
                        self.edge(job['root'],who,job['root'],'create' if address(launch['creator']) else 'launch_payer',launch['slot'],None,signature=launch['signature'],ix=launch['ix'],finality=launch['finality'],evidence=launch['evidence'],source='sparse_archive',timestamp_unknown=True,creator_field_present=address(launch['creator']))
                        for who in set((launch['creator'],launch['payer'])):
                            self.node(who,'account','launch_participant');self.enqueue(job['root'],who,launch['slot'],job['stamp'],0,after_order=(launch['tx_order'],launch['ix']))
                            self.enqueue(job['root'],who,None,job['stamp'],0,'core_forward')
                        early=s.db.execute("SELECT a.*,o.signature,o.tx_order,o.finality,o.evidence FROM activity a JOIN observations o ON o.id=a.observation WHERE a.mint=? AND a.action='buy' AND (a.slot,o.tx_order,a.ix)>(?,?,?) AND a.slot<? AND o.finality!='dead_branch' ORDER BY a.slot,o.tx_order,a.ix",(job['root'],launch['slot'],launch['tx_order'],launch['ix'],launch['slot']+150)).fetchall()
                        for r in early:
                            self.node(r['wallet'],'account','observed_early_buyer')
                            self.edge(job['root'],r['wallet'],job['root'],'early_buy',r['slot'],None,signature=r['signature'],ix=r['ix'],finality=r['finality'],evidence=r['evidence'],source='sparse_archive',timestamp_unknown=True,bundle_membership_proven=False)
                            self.enqueue(job['root'],r['wallet'],r['slot'],job['stamp'],0,after_order=(r['tx_order'],r['ix']))
                            self.enqueue(job['root'],r['wallet'],None,job['stamp'],0,'core_forward')
                        self.set('missing_launch:'+job['root'],False)
                    self.db.execute('UPDATE frontier SET state=? WHERE id=?',('launch_recovered' if launch else 'launch_not_found_in_window',job['id']))
            return dict(job_id=job['id'],blocks_read=count,source_complete=False)
        finally:
            search_index.close()
            if s._originals:s._originals.close()
            s.db.close()

    def core_forward(self,job,scope):
        core=self.base/'evidence/core-trace-v1';path=core/'arkm_funding.sqlite3'
        if not path.exists():return dict(job_id=job['id'],waiting_for='core_funding')
        after=(job['stamp'] or START_MS)//1000
        now=min(int(time.time()),END_MS//1000)
        cursor=json.loads(job['cursor'])
        with closing(read_db(path)) as db:
            db.set_progress_handler(lambda:(scope.guard() or 0),100000)
            # Source records may use seconds or milliseconds; normalize while
            # retaining raw row identity. Paging uses rowid, not an inferred time.
            rows=db.execute('''SELECT rowid,* FROM projection WHERE source=? AND rowid>?
                AND ((stamp>? AND stamp<=?) OR (stamp>? AND stamp<=?)) ORDER BY rowid LIMIT 200''',
                (decode(job['wallet']),cursor or 0,after,now,after*1000+999,now*1000+999)).fetchall()
        with self.db:
            for r in rows:
                stamp=r['stamp'] if r['stamp']>=10**12 else r['stamp']*1000
                who=encode(r['destination']);self.node(who,'account','dataset_forward_recipient')
                self.edge(job['root'],job['wallet'],who,'funding_record',r['slot'],stamp,signature=r['signature'],
                    amount=r['amount'],symbol=r['symbol'],source_rowid=r['rowid'],source='core_arkm_funding',
                    third_party_record=True,same_owner_proven=False,destination_labeled=bool(r['destination_labeled']))
                if who!=job['wallet']:
                    self.enqueue(job['root'],who,None,stamp,job['depth']+1,'core_forward')
            self.db.execute('UPDATE frontier SET cursor=?,state=? WHERE id=?',(canonical(rows[-1]['rowid']) if rows else job['cursor'],
                'pending' if len(rows)==200 else 'core_source_exhausted',job['id']))
        # Any later launches by this account are supported metadata associations.
        with closing(read_db(core/'token_metadata.sqlite3')) as meta, self.db:
            for r in meta.execute('SELECT rowid,* FROM projection WHERE creator=? AND first_seen>? AND first_seen<=?',(decode(job['wallet']),after,now)):
                mint=encode(r['mint']);self.node(mint,'token','later_dataset_launch')
                self.edge(job['root'],job['wallet'],mint,'later_dataset_create',None,r['first_seen']*1000,
                    source='core_token_metadata',source_rowid=r['rowid'],independently_chain_verified=False)
        return dict(job_id=job['id'],observed_funding_records=len(rows),source_complete=False)

    def view(self):
        with assessment_snapshot(self,maximum_seconds=10) as snapshot:
            return self._view(snapshot)

    def _view(self,snapshot):
        nodes=[dict(id=r['id'],kind=r['kind'],roles=json.loads(r['roles'])) for r in self.db.execute('SELECT * FROM nodes')]
        # Browser is a bounded projection. The durable graph and pending pages
        # retain everything; this is never advertised as an all-history map.
        edges=[];quarantined={}
        custody_annotations=self.custody.annotations()
        identity_path=self.out/'source-identities.json'
        identities=json.loads(identity_path.read_bytes()).get('identities',{}) if identity_path.exists() else {}
        for root in self.db.execute('SELECT mint FROM roots ORDER BY rowid').fetchall():
            if len(edges)>=12000:break
            unique={}
            rows=self.db.execute("SELECT * FROM edges WHERE root=? AND coalesce(json_extract(body,'$.dead_branch'),0)=0 AND coalesce(json_extract(body,'$.invalid_address'),0)=0 ORDER BY (kind LIKE 'later_%'),coalesce(json_extract(body,'$.precision_version'),0) DESC,rowid LIMIT 4000",(root[0],)).fetchall()
            assessments={}
            for offset in range(0,len(rows),400):
                assessments.update(latest_assessments(self,[r['id'] for r in rows[offset:offset+400]],revision=snapshot))
            for r in rows:
                assessment=assessments[r['id']]
                if edge_requires_commit(r) and assessment['status']!='committed':
                    status=assessment['status'];quarantined[status]=quarantined.get(status,0)+1;continue
                e=dict(id=r['id'],root=r['root'],source=r['source'],target=r['target'],kind=r['kind'],slot=r['slot'],stamp=r['stamp'],evidence=json.loads(r['body']))
                if edge_requires_commit(r):
                    e['evidence']['instruction_commit_status']=assessment['status']
                    if assessment.get('transfer_kind'):e['evidence']['transfer_kind']=assessment['transfer_kind']
                annotate_source_identity(e,identities)
                if e['id'] in custody_annotations:
                    e['evidence']['custody_gaps']=custody_annotations[e['id']]
                    e['evidence']['path_funds_continuity_proven']=False
                unique.setdefault(event_key(e),e)
            edges.extend(list(unique.values())[:min(300,max(0,12000-len(edges)))])
        relevant={v for e in edges for v in (e['source'],e['target'])}
        root_fields=('mint','name','symbol','creator','provider_rank','created_ms','cohort_id','cohort_kind','root_kind','usd_market_cap','ranking_verified')
        roots=[{k:v for k,v in json.loads(r[0]).items() if k in root_fields} for r in self.db.execute('SELECT body FROM roots')]
        return dict(schema='economic-machine.forward-wallet-map.v1',updated_ms=time.time_ns()//1000000,
            roots=roots,nodes=[n for n in nodes if n['id'] in relevant],edges=edges,
            source_window=dict(from_ms=START_MS,to_ms=min(time.time_ns()//1000000,END_MS)),
            counts=dict(nodes=len(nodes),edges=self.db.execute('SELECT count(*) FROM edges').fetchone()[0]),
            quarantined_raw_records=quarantined,
            frontier_states=dict(self.db.execute('SELECT state,count(*) FROM frontier GROUP BY state')),
            view_truncated=self.db.execute('SELECT count(*) FROM edges').fetchone()[0]>len(edges),
            projection_policy='Up to 300 distinct events per root and 12000 in this overview; full observations retained in the search index.',
            complete_to_today=False,ranking_verified=False,current_balances_verified=False,bundle_membership_verified=False,
            profitability_proven=False,orders_submitted=0,
            gaps=['2024–2026 all-account transaction history is not available in the present local index.',
                  'Core trade histories are sampled. Core funding records are not a complete account ledger.',
                  'Locally recovered historical instructions have slot evidence; unknown block times remain undated.',
                  'Token-account transfers and owner observations do not establish common ownership or same-funds continuity.',
                  'Raw instructions without a current successful-execution assessment are retained but excluded from this map.'])


def prepare_jobs(graph,scope,excluded_ids=()):
    menu=build_menu(graph,excluded_ids=excluded_ids)
    jobs=menu['jobs']
    if not jobs:return None
    mapping={r['id']:r for r in jobs}
    context=dict(task='Investigate public Solana wallets, both incoming and outgoing records; follow launch participants forward into later transfers, launches and buys.',
        requested_from='genesis for explicit wallet searches; 2024-01-19 for Pump launch research',requested_through='2026-10-15',
        jobs=[dict(id=j['id'],depth=j['depth'],source=j['source'],root_id=digest(j['root'])[:12],wallet_id=digest(j['wallet'])[:12],page_cursor=compact_cursor(j['cursor'])) for j in jobs],
        graph_counts=dict(graph.db.execute('SELECT kind,count(*) FROM edges GROUP BY kind')),
        address_index_revision=graph.state('index_revision',0),
        response_retry=graph.state('model_retry_generation',0),
        recent_observations=[dict(kind=r['kind'],from_id=digest(r['source'])[:12],to_id=digest(r['target'])[:12],slot=r['slot'],stamp=r['stamp'],
            amount=json.loads(r['body']).get('amount_lamports',json.loads(r['body']).get('amount')),
            source=json.loads(r['body']).get('source'),root_id=digest(r['root'])[:12]) for r in graph.db.execute('SELECT * FROM edges ORDER BY rowid DESC LIMIT 12')],
        complete_wallet_histories=False,ownership_known=False,bundle_membership_known=False)
    quality_path=graph.out/'quality.json'
    if quality_path.exists():
        quality=json.loads(quality_path.read_bytes())
        context['quality_audit']=dict(at_ms=quality['at_ms'],totals=quality['totals'],
            roots=[dict(root_id=digest(r['mint'])[:12],launch_observed=bool(r['observed_launch_events']),
                early_buyers_observed=r['observed_early_buyers'],first_seen_sample_buyers=r['sampled_first_seen_buyers'],
                distinct_observation_records=r.get('distinct_observation_records',r.get('distinct_events',0)),complete_wallet_history=False) for r in quality['roots'] if r['mint'] in menu['admitted_roots']])
        context['dataset_trade_warning']='Rows may be alternative parser versions of the same signature/event. Counts of rows are not independent trades. Recover original signatures before deduplicating or scoring profitability.'
    custody=graph.custody.snapshot()
    context['custody_gaps']=dict(summary=custody['summary'],scope=custody['scope'],
        candidate_job_ids=[j['id'] for j in jobs if graph.db.execute('SELECT 1 FROM custody_jobs WHERE job=? LIMIT 1',(j['id'],)).fetchone()],
        rule='Follow CEX-to-CEX and Solana return candidates without discarding custody gaps. Same exchange label, amount, time or asset cannot prove the same customer or funds. Public bridge leads do not recover exchange customer ledgers.')
    context['crosschain']=dict(candidate_job_ids=[j['id'] for j in jobs if j['source']=='wormhole_forward'],
        scope='Wormholescan public index queries. VAA byte matching is not guardian verification, canonical chain verification, or private-ledger recovery.')
    req=dict(model=MODEL,max_tokens=4096,temperature=.1,reasoning={'effort':'minimal','exclude':True},provider={'allow_fallbacks':False,'require_parameters':True,'max_price':{'prompt':.1,'completion':.2}},
        messages=[dict(role='system',content='You are the investigator controlling Economic Machine public-wallet research. Choose 1 to 8 supplied job IDs. Explicit wallet searches investigate incoming and outgoing public records across available history; launch-participant branches retain their original forward-time anchors. Prioritize gsfa_history jobs when the supplied menu contains them: their index candidates must be checked against signed raw transactions. Include at least one custody_gaps candidate and one crosschain candidate when each list is nonempty; these can be the same job. The owner prioritized CEX transit and cross-chain return investigation. Use remaining selections for broad coverage of admitted original, recent, annual and user-requested wallet cohorts. Prioritize verified evidence over provider claims. Never invent complete history. Public bridge and custody followups are unattributed leads, never a recovered private ledger. Describe what evidence each selected job could recover. No shell commands, URLs, purchases, invented edges, owner attribution, bundle attribution, or profitability claims. Source exhaustion never proves complete history. Return the required JSON only.'),
                  dict(role='user',content=canonical(context))],
        response_format={'type':'json_schema','json_schema':{'name':'wallet_investigation','strict':True,'schema':{
            'type':'object','additionalProperties':False,'properties':{
                'job_ids':{'type':'array','items':{'type':'string','enum':list(mapping)}},
                'reason':{'type':'string'},'remaining_gaps':{'type':'array','items':{'type':'string'}}},
            'required':['job_ids','reason','remaining_gaps']}}})
    context['admitted_cohorts']=menu['cohort_groups']
    context['planner_lane']=graph.state('planner_lane',0)
    context['portal_scope']='Public address-filtered index leads; only CAR-matched signatures and account membership become raw evidence. Empty responses do not prove complete history.'
    context['raw_history_candidate_ids']=[j['id'] for j in jobs if j['source']=='archive_transfer']
    req['messages'][1]['content']=canonical(context)
    if len(canonical(req).encode())>16000:
        context['recent_observations']=[]
        context.pop('quality_audit',None)
        for item in context['jobs']:
            item['page_cursor']=compact_cursor(mapping[item['id']]['cursor'],max_chars=120)
        req['messages'][1]['content']=canonical(context)
    if len(canonical(req).encode())>16000:raise ValueError('MAP_MODEL_CONTEXT_BOUND')
    return dict(req=req,mapping=mapping,context=context,groups=menu['groups'])


def finish_jobs(graph,budget,prepared,call,raw):
    req=prepared['req'];mapping=prepared['mapping'];context=prepared['context']
    if isinstance(raw,bytes):raw=raw.decode('utf-8')
    accepted=False;value=None
    with (graph.out/f'muse-response-{call:06}.raw').open('xb') as handle:
        handle.write(raw.encode());handle.flush();os.fsync(handle.fileno())
    try:
        response=json.loads(raw,object_pairs_hook=unique)
        atomic(graph.out/f'muse-response-{call:06}.json',response)
        if not isinstance(response,dict):raise ValueError('MAP_MODEL_ENVELOPE')
        if response.get('model')!=MODEL:raise ValueError('MAP_MODEL_IDENTITY')
        choices=response.get('choices')
        if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict) or not isinstance(choices[0].get('message'),dict):raise ValueError('MAP_MODEL_CHOICES')
        content=response['choices'][0]['message']['content']
        if not isinstance(content,str) or not content.strip():raise ValueError('MAP_MODEL_EMPTY_RESPONSE')
        value=json.loads(content,object_pairs_hook=unique)
        if not isinstance(value,dict):raise ValueError('MAP_MODEL_KEYS')
        if set(value)!= {'job_ids','reason','remaining_gaps'}:raise ValueError('MAP_MODEL_KEYS')
        ids=value['job_ids']
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or len(set(ids))!=len(ids) or any(i not in mapping for i in ids):raise ValueError('MAP_MODEL_JOBS')
        if not isinstance(value['reason'],str) or len(value['reason'])>3000:raise ValueError('MAP_MODEL_REASON')
        if not isinstance(value['remaining_gaps'],list) or len(value['remaining_gaps'])>20 or any(not isinstance(x,str) or len(x)>1000 for x in value['remaining_gaps']):raise ValueError('MAP_MODEL_GAPS')
        accepted=True
    finally:budget.finish(call,raw,accepted)
    coverage=supplement_selection(value['job_ids'],mapping,prepared['groups'])
    scheduled=coverage['scheduled_job_ids'];reserved=coverage['engine_reserved']
    record=dict(value,scheduled_job_ids=scheduled,engine_reserved=reserved,
        engine_reserved_raw_history_job=reserved.get('raw_history'),
        engine_reserved_custody_job=reserved.get('custody'),
        engine_reserved_crosschain_job=reserved.get('crosschain'),
        engine_reserved_portal_job=reserved.get('portal'),
        admitted_cohorts=prepared['context']['admitted_cohorts'])
    with graph.db:graph.db.execute('INSERT INTO investigations(call_id,body,at) VALUES(?,?,?)',(call,canonical(record),time.time_ns()//1000000))
    return [mapping[i] for i in scheduled]



def select_jobs(graph,budget,key_file,scope):
    prepared=prepare_jobs(graph,scope)
    if prepared is None:return []
    req=prepared['req'];call=budget.reserve(req,time.time_ns()//1000000)
    atomic(graph.out/f'muse-request-{call:06}.json',req)
    try:raw=provider_call(req,key_file)
    except ValueError as e:
        if str(e) in ('provider outcome unknown','provider HTTP 429','provider HTTP 502','provider HTTP 503','provider HTTP 504'):
            raise ValueError('MAP_MODEL_PROVIDER_TRANSIENT') from None
        raise
    return finish_jobs(graph,budget,prepared,call,raw)


def select_parallel(graph,budget,key_file,scope,workers=10,admission_open=None):
    # Only model HTTP calls run concurrently. SQLite, cursors, admission and
    # evidence writes stay on the coordinator thread, with disjoint menus.
    # The caller validates the pinned operating grant. This is capacity, not
    # authority to replace a ten-worker grant or extend its original deadline.
    if type(workers) is not int or not 1<=workers<=100:raise ValueError('MAP_WORKERS_BOUND')
    if graph.db.in_transaction:raise ValueError('MAP_MODEL_PARALLEL_OPEN_TRANSACTION')
    if admission_open is not None and not callable(admission_open):raise ValueError('MAP_MODEL_ADMISSION_CALLBACK')
    scope.guard()
    # Capture immutable clock bounds on the coordinator. Worker threads must
    # not touch the scope's SQLite guards or the model-accounting connection.
    continued=getattr(scope,'continuation',None)
    if continued is not None:
        if getattr(budget,'continuation',None) is None or budget.continuation.hash!=continued.hash:
            raise ValueError('MAP_MODEL_THREAD_CONTINUATION_BOUND')
        provider_deadline=continued.effective_deadline_ms
    else:
        deadlines=[budget.scope.get('deadline_ms')]
        if isinstance(getattr(scope,'grant',None),dict):deadlines.append(scope.grant.get('deadline_ms'))
        if any(type(deadline) is not int or deadline<=0 for deadline in deadlines):
            raise ValueError('MAP_MODEL_THREAD_DEADLINE_BOUND')
        provider_deadline=min(deadlines)
    graph.db.executescript('''
        CREATE TABLE IF NOT EXISTS parallel_model_lanes(batch INTEGER,lane INTEGER,call_id INTEGER UNIQUE,
            state TEXT,body TEXT,PRIMARY KEY(batch,lane));
        CREATE TABLE IF NOT EXISTS parallel_model_events(id INTEGER PRIMARY KEY,batch INTEGER,lane INTEGER,
            call_id INTEGER,event TEXT,at_ms INTEGER,body TEXT);
    ''')
    # A crash does not make an old reservation free, and cannot authorize a
    # blind replay of that call ID. Pending source jobs can be selected again
    # in a later batch; disjoint menus are a within-batch guarantee only.
    interrupted=graph.db.execute("SELECT * FROM parallel_model_lanes WHERE state IN ('reserved','submitted','thinking') ORDER BY batch,lane LIMIT 101").fetchall()
    if len(interrupted)>100:raise ValueError('MAP_MODEL_PARALLEL_RECOVERY_BOUND')
    with graph.db:
        for old in interrupted:
            prior=json.loads(old['body'])
            planned=graph.db.execute('SELECT 1 FROM investigations WHERE call_id=?',(old['call_id'],)).fetchone()
            call=budget.db.execute('SELECT status,response_hash FROM calls WHERE id=?',(old['call_id'],)).fetchone()
            state=('planned_recovered' if planned else 'interrupted_response_recorded' if call and call[1]
                else 'interrupted_not_dispatched' if old['state']=='reserved' else 'interrupted_outcome_unknown')
            prior.update(state=state,recovered_at_ms=time.time_ns()//1000000,original_call_preserved=True,
                automatically_replayed=False,model_budget_status=call[0] if call else 'missing')
            graph.db.execute('UPDATE parallel_model_lanes SET state=?,body=? WHERE batch=? AND lane=?',
                (state,canonical(prior),old['batch'],old['lane']))
            graph.db.execute('INSERT INTO parallel_model_events(batch,lane,call_id,event,at_ms,body) VALUES(?,?,?,?,?,?)',
                (old['batch'],old['lane'],old['call_id'],'restart_reconciled',prior['recovered_at_ms'],canonical(prior)))
    pending={};excluded=set();results=[];errors=[];admission_error=None;ready=[]
    states=[];batch=time.time_ns()//1000000
    previous=graph.db.execute('SELECT coalesce(max(batch),0) FROM parallel_model_lanes').fetchone()[0]
    batch=max(batch,previous+1)
    lock=threading.Lock();metrics=dict(active=0,peak=0,started=0,completed=0,prevented_before_provider=0)
    def event(state,kind,at=None):
        with graph.db:
            graph.db.execute('INSERT INTO parallel_model_lanes VALUES(?,?,?,?,?) ON CONFLICT(batch,lane) DO UPDATE SET state=excluded.state,body=excluded.body',
                (batch,state['lane'],state['call_id'],state['state'],canonical(state)))
            graph.db.execute('INSERT INTO parallel_model_events(batch,lane,call_id,event,at_ms,body) VALUES(?,?,?,?,?,?)',
                (batch,state['lane'],state['call_id'],kind,at or time.time_ns()//1000000,canonical(state)))
    def call_model(req):
        with lock:
            started=time.time_ns()//1000000
            if provider_deadline is not None and started>=provider_deadline:
                metrics['prevented_before_provider']+=1
                return dict(not_dispatched=True,reason='deadline_before_provider_start',
                    checked_ms=started,deadline_ms=provider_deadline)
            monotonic=time.monotonic_ns()
            metrics['active']+=1;metrics['started']+=1;metrics['peak']=max(metrics['peak'],metrics['active'])
        try:
            try:return dict(raw=provider_call(req,key_file),started_ms=started,finished_ms=time.time_ns()//1000000,
                elapsed_ns=time.monotonic_ns()-monotonic)
            except Exception as e:return dict(error=e,started_ms=started,finished_ms=time.time_ns()//1000000,
                elapsed_ns=time.monotonic_ns()-monotonic)
        finally:
            with lock:metrics['active']-=1;metrics['completed']+=1
    def publish():
        with lock:current=dict(metrics)
        atomic(graph.out/'workers.json',dict(schema='economic-machine.research-workers.v1',
            configured=workers,active=current['active'],max_inflight=workers,
            observed_peak_concurrent=current['peak'],provider_calls_started=current['started'],
            provider_calls_completed=current['completed'],interrupted_lanes_preserved=len(interrupted),
            not_dispatched_before_provider=current['prevented_before_provider'],
            workers=states,batch_ms=batch,updated_ms=time.time_ns()//1000000,
            source_fetch_parallelism=1,evidence_writer_parallelism=1,
            execution='Parallel model planning; serialized verified evidence writes'))
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='muse-investigator') as pool:
        for lane in range(workers):
            state=None
            try:
                if admission_open is not None and not admission_open():break
                scope.guard()
                with graph.db:graph.set('planner_lane',dict(batch_ms=batch,lane=lane))
                prepared=prepare_jobs(graph,scope,excluded)
                if prepared is None:break
                if admission_open is not None and not admission_open():break
                excluded.update(prepared['mapping'])
                req=prepared['req'];call=budget.reserve(req,time.time_ns()//1000000)
                state=dict(lane=lane,call_id=call,state='reserved',admitted_jobs=len(prepared['mapping']),
                    request_sha256=digest(req),model=MODEL)
                states.append(state);event(state,'reserved')
                atomic(graph.out/f'muse-request-{call:06}.json',req)
                if workers>10:
                    # Large cohorts prepare bounded disjoint menus before the
                    # network burst; slow SQLite admission cannot serialize
                    # fast model replies. Recheck authority before dispatch.
                    ready.append((prepared,call,state));publish();continue
                # The submission event is durable before the network thread
                # starts. A crash in this boundary remains outcome-unknown.
                state['state']='thinking';event(state,'dispatch_submitted');publish()
                future=pool.submit(call_model,req)
                pending[future]=(prepared,call,state)
            except Exception as e:
                if state is not None:
                    state.update(state='admission_error',error=type(e).__name__)
                    try:event(state,'admission_failed')
                    except Exception:pass  # The original reservation remains.
                admission_error=e;break
        for prepared,call,state in ready:
            try:
                if admission_error is not None or (admission_open is not None and not admission_open()):
                    state.update(state='not_dispatched',dispatch_attempted=False,
                        reservation_preserved=True,provider_charge_claimed=False)
                    event(state,'not_dispatched');continue
                scope.guard()
                state['state']='thinking';event(state,'dispatch_submitted')
                future=pool.submit(call_model,prepared['req'])
                pending[future]=(prepared,call,state)
            except Exception as e:
                state.update(state='admission_error',error=type(e).__name__)
                try:event(state,'admission_failed')
                except Exception:pass
                admission_error=e
        publish()
        while pending:
            done,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
            # Already-sent calls must be journaled even if new admission has
            # expired. This reconciles responses; it dispatches no new work.
            for future in done:
                prepared,call,state=pending.pop(future)
                try:
                    outcome=future.result()
                    if outcome.get('not_dispatched'):
                        state.update(state='not_dispatched',reason=outcome['reason'],checked_ms=outcome['checked_ms'],
                            deadline_ms=outcome['deadline_ms'],dispatch_attempted=False,
                            reservation_preserved=True,provider_charge_claimed=False)
                        event(state,'not_dispatched',outcome['checked_ms']);publish();continue
                    state.update(started_ms=outcome['started_ms'],finished_ms=outcome['finished_ms'],
                        latency_ms=outcome['elapsed_ns']/1_000_000)
                    event(state,'provider_completed',outcome['finished_ms'])
                    if 'error' in outcome:
                        # Keep the unknown reservation, but route a wholly
                        # transient batch to the coordinator's slower retry.
                        # These are local classifications, not Retry-After
                        # values from the provider (the client omits headers).
                        state['provider_transient']=isinstance(outcome['error'],ValueError) and str(outcome['error']) in (
                            'provider outcome unknown','provider HTTP 429','provider HTTP 500',
                            'provider HTTP 502','provider HTTP 503','provider HTTP 504','provider HTTP 529')
                        raise outcome['error']
                    raw=outcome['raw']
                    selected=finish_jobs(graph,budget,prepared,call,raw)
                    results.extend(selected);state.update(state='planned',selected_jobs=len(selected))
                except Exception as e:
                    state.update(state='error',error=type(e).__name__)
                    errors.append(dict(call_id=call,error=str(e)[:120],provider_transient=state.get('provider_transient',False)))
                    with graph.db:graph.set('model_retry_generation',graph.state('model_retry_generation',0)+1)
                event(state,'result_journaled')
                publish()
    with lock:actual=dict(metrics)
    interactive={row['mint'] for row in graph.db.execute('SELECT mint,body FROM roots') if json.loads(row['body']).get('root_kind')=='wallet'}
    # HTTP completion order must not bury an interactive wallet lookup behind
    # cohort archive work. Preserve every model selection; order execution only.
    results.sort(key=lambda job:(job['root'] not in interactive,job['source']!='indexed_forward'))
    record=dict(batch_ms=batch,configured=workers,max_inflight=workers,dispatched=actual['started'],
        reservations=len(states),provider_calls_completed=actual['completed'],selected_jobs=len(results),errors=errors,
        observed_peak_concurrent=actual['peak'],workers=states,interrupted_lanes_preserved=len(interrupted),
        not_dispatched_before_provider=actual['prevented_before_provider'],
        source_fetch_parallelism=1,evidence_writer_parallelism=1,
        menus_disjoint_within_batch=True,source_jobs_exactly_once=False,old_call_ids_automatically_replayed=False,
        execution_order_job_ids=[job['id'] for job in results])
    publish()
    atomic(graph.out/f'muse-batch-{batch}.json',record)
    with graph.db:graph.set('parallel_last_batch',record)
    if admission_error:raise admission_error
    if actual['started'] and len(errors)==actual['started']:
        if all(row['provider_transient'] for row in errors):raise ValueError('MAP_MODEL_PROVIDER_TRANSIENT')
        raise ValueError('MAP_MODEL_PARALLEL_ALL_FAILED')
    return results


def admit_cohorts(graph,scope):
    from . import cohort,annual_cohort
    if not graph.state('original_research_roots_v1'):
        with graph.db:graph.set('original_research_roots_v1',[r[0] for r in graph.db.execute('SELECT mint FROM roots ORDER BY rowid')])
    for name,module in (('recent50-v1',cohort),('annual-year-v1',annual_cohort)):
        folder=graph.base/'evidence'/name
        if graph.state('cohort_admitted:'+name) or not (folder/'grant.sha256').exists():continue
        scope.guard()
        raw=(folder/'grant.json').read_bytes();grant=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()!=(folder/'grant.sha256').read_text().strip()
            or grant.get('parent_grant_sha256')!=scope.hash):raise ValueError('MAP_COHORT_GRANT_HASH')
        snapshot=json.loads((folder/'snapshot.json').read_bytes())
        result=module.activate(graph,snapshot,grant,time.time_ns()//1000000,scope.grant['deadline_ms'])
        with graph.db:graph.set('cohort_admitted:'+name,result)
        atomic(graph.out/(name+'-activation.json'),dict(result,at_ms=time.time_ns()//1000000))


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--key-file');p.add_argument('--authorize',action='store_true');p.add_argument('--once',action='store_true');a=p.parse_args()
    base=Path(a.base);g=Graph(base);path=g.out/'model-scope.json'
    core_scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    if a.authorize:
        if path.exists():raise ValueError('MAP_SCOPE_ALREADY_EXISTS')
        scope=dict(authorization='owner_unlimited_muse_top10_forward_wallet_research',maximum_usd=None,max_calls=None,model=MODEL,
            deadline_ms=core_scope.grant['deadline_ms'],trade_authority=False,approved_ms=time.time_ns()//1000000,
            old_12_call_ledger_preserved=True,io_and_storage_grant_sha256=core_scope.hash)
        atomic(path,scope);g.seed();atomic(g.out/'map.json',g.view());g.close();core_scope.close();return
    lock=(g.out/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    g.seed()
    internal=g.out/'internal-grant.json';parallel=1;workspace=None
    if internal.exists():
        raw=internal.read_bytes();grant=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()!=(g.out/'internal-grant.sha256').read_text().strip()
            or grant.get('authorization')!='owner_private_solana_research_ten_muse_workers'
            or grant.get('parent_grant_sha256')!=core_scope.hash
            or grant.get('deadline_ms')!=core_scope.grant['deadline_ms']
            or grant.get('parallel_models')!=10 or grant.get('trading') is not False
            or grant.get('public_deployment') is not False or grant.get('paid_data_api') is not False):
            raise ValueError('MAP_INTERNAL_AUTHORITY')
        pinned=g.state('internal_authority_sha256')
        if pinned and pinned!=hashlib.sha256(raw).hexdigest():raise ValueError('MAP_INTERNAL_AUTHORITY_CHANGED')
        with g.db:g.set('internal_authority_sha256',hashlib.sha256(raw).hexdigest())
        parallel=10;workspace=Workspace(base)
    if (g.out/'parallel-grant.json').exists():
        if not workspace or not core_scope.expansion:raise ValueError('MAP_PARALLEL_EXPANSION_REQUIRED')
        from .research_parallel import load_parallel_authority
        parallel_authority=load_parallel_authority(g,core_scope)
        with g.db:
            g.set('parallel_authority_sha256',parallel_authority['grant_sha256'])
            g.set('history_expansion_sha256',core_scope.expansion.hash)
        parallel=parallel_authority['parallel_models']
    continuation=getattr(core_scope,'continuation',None)
    if continuation:
        pinned=g.state('history_continuation_sha256')
        if pinned is not None and pinned!=continuation.hash:raise ValueError('MAP_CONTINUATION_CHANGED')
        with g.db:g.set('history_continuation_sha256',continuation.hash)
    budget=UncappedMuse(g.out/'model.sqlite3',json.loads(path.read_bytes()),continuation);stopping=False;model_active=False
    external_pool=None;pool_adapter=None;pool_guard=None
    external_pool_root=os.environ.get('EM_RESEARCH_POOL_ROOT')
    native_scope=core_scope
    def stop(*_):
        nonlocal stopping
        stopping=True
        if model_active:return  # Drain and journal every already-dispatched reply.
        raise InterruptedError('MAP_STOP_SIGNAL_PRESERVE_CURSOR')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    status=dict(state='STARTING',new_calls_unlimited=budget.spend is None,maximum_usd=None,max_calls=None,orders_submitted=0,
        monthly_research_limit_krw=budget.spend.grant['monthly_limit'] if budget.spend else None,
        configured_model_workers=50 if external_pool_root else parallel,history_expansion_sha256=core_scope.expansion.hash if core_scope.expansion else None,
        history_continuation_sha256=continuation.hash if continuation else None,
        deadline_ms=core_scope.effective_deadline_ms,original_deadline_ms=core_scope.grant['deadline_ms'])
    def stage(name):
        if not external_pool_root:return
        now=time.time_ns()//1000000
        previous=status.get('native_source_stage')
        started=status.get('native_stage_started_ms')
        if previous and type(started) is int:
            status['native_previous_stage']=dict(name=previous,elapsed_ms=max(0,now-started))
        status.update(native_source_stage=name,native_stage_started_ms=now,updated_ms=now)
        atomic(g.out/'status.json',status)
    current_index=None
    try:
        status.update(state='STARTING_NATIVE_RECOVERY',updated_ms=time.time_ns()//1000000,
                      legacy_effective_deadline_ms=core_scope.effective_deadline_ms)
        atomic(g.out/'status.json',status)
        if external_pool_root:
            from ..research_pool.queue import Queue
            from . import research_pool_adapter as pool_adapter
            external_pool=Queue(Path(external_pool_root))
            pool_guard=pool_adapter.NativePoolGuard(external_pool,base)
            native_scope=pool_adapter.PoolBoundScope(core_scope,pool_guard)
            pool_adapter.initialize_consumer(g)
            status.update(deadline_ms=native_scope.operating_deadline_ms,
                research_pool_policy_sha256=pool_guard.digest,native_source_stage='startup_integrity_checks')
            atomic(g.out/'status.json',status)
        native_scope.guard()
        stage('startup_integrity_checks')
        g.migrate_precision()
        g.reconcile_launch_evidence(native_scope)
        g.recover_original_anchors(native_scope)
        # Keep WAL sidecars available while the private read-only web process
        # serves reports, including between model batches and source retries.
        current_index=AddressIndex(g.out/'address-index.sqlite3')
        stage('restore_coverage_projection')
        sync_retained(g,native_scope,limit=300)
        while not stopping:
            native_scope.guard()
            stage('seed_native_frontier')
            if workspace:admit_cohorts(g,native_scope)
            native_scope.guard(32*1024**2,force=True);g.retry_source_errors(time.time_ns()//1000000);g.add_core(native_scope)
            g.custody.backfill(native_scope.guard)
            g.crosschain.seed(native_scope.guard)
            g.raw_history.seed(native_scope.guard)
            g.portal.seed(native_scope)
            if workspace:g.launch_resolver.seed(native_scope)
            if workspace:workspace.admit(g,native_scope)
            if workspace:seed_owner_accounts(g,native_scope,limit=100)
            stage('seed_gsfa_frontier')
            if workspace:g.gsfa.seed(native_scope)
            try:
                model_active=True
                if external_pool:
                    stage('consume_external_decisions')
                    jobs=pool_adapter.drain_ready(external_pool,g,limit=100,guard=pool_guard)
                    status['research_pool']=pool_adapter.pool_status(external_pool)
                    status['legacy_model_journal_preserved']=True
                    if not jobs:
                        status.update(state='WAITING_EXTERNAL_RESEARCH',updated_ms=time.time_ns()//1000000)
                        atomic(g.out/'status.json',status)
                else:
                    jobs=select_parallel(g,budget,a.key_file,core_scope,parallel,admission_open=lambda:not stopping) if parallel>1 else select_jobs(g,budget,a.key_file,core_scope)
            except (ValueError,TypeError,KeyError) as e:
                if not (str(e).startswith('MAP_MODEL_') or isinstance(e,(json.JSONDecodeError,TypeError,KeyError))):raise
                failures=g.state('consecutive_model_invalid',0)+1
                with g.db:
                    g.set('consecutive_model_invalid',failures);g.set('model_retry_generation',g.state('model_retry_generation',0)+1)
                if failures>=3:raise ValueError('MAP_MODEL_INVALID_THREE_CONSECUTIVE') from e
                status.update(state='MODEL_RESPONSE_RETRY',updated_ms=time.time_ns()//1000000,budget=budget.totals(),error=str(e)[:120])
                atomic(g.out/'status.json',status)
                for _ in range(30 if str(e)=='MAP_MODEL_PROVIDER_TRANSIENT' else 5):
                    native_scope.guard();time.sleep(1)
                continue
            finally:model_active=False
            with g.db:g.set('consecutive_model_invalid',0)
            if external_pool and not jobs:
                # An empty asynchronous result queue is normal. Do not rerun
                # the full map projection/audit merely to wait for a model.
                if a.once:break
                for _ in range(2):
                    if stopping:break
                    native_scope.guard();time.sleep(1)
                continue
            outcomes=[]
            for job in jobs:
                if stopping:break
                native_scope.guard()
                if not g.custody.enter(job['id']):
                    with g.db:g.db.execute("UPDATE frontier SET state='custody_anchor_revoked' WHERE id=?",(job['id'],))
                    g.custody.leave()
                    if external_pool:pool_adapter.finish_job(external_pool,g,job,dict(job_id=job['id'],state='custody_anchor_revoked',executed=False))
                    continue
                status.update(state='INVESTIGATING',current_job_source=job['source'],updated_ms=time.time_ns()//1000000,budget=budget.totals())
                stage('native_source_step:'+job['source'])
                atomic(g.out/'status.json',status)
                try:
                    if job['source']=='local_forward':result=g.local_forward(job)
                    elif job['source']=='recover_launch':result=g.recover_launch(job,native_scope)
                    elif job['source']=='indexed_forward':result=g.indexed_forward(job,native_scope)
                    elif job['source']=='wormhole_forward':result=g.crosschain.run(job,native_scope)
                    elif job['source']=='archive_transfer':result=g.raw_history.run(job,native_scope)
                    elif job['source'] in ('portal_forward','portal_verify'):result=g.portal.run(job,native_scope)
                    elif job['source']=='portal_resolve_launch':result=g.launch_resolver.run(job,native_scope)
                    elif job['source']=='gsfa_history':result=g.gsfa.run(job,native_scope)
                    elif job['source'] in ('core_early','core_participation'):result=g.core_participation(job,native_scope)
                    else:result=g.core_forward(job,native_scope)
                except (PortalDeferred,ArchiveDeferred) as e:
                    # Shared provider cooldown defers this job, never every
                    # unrelated lookup. No request or failure attempt is added.
                    result=dict(job_id=job['id'],state='waiting_for_source',reason=e.reason,retryable=True,
                        retry_at_ms=e.retry_at_ms,attempts=g.state('source_error:'+job['id'],{}).get('attempts',0),no_request=True)
                    with g.db:
                        g.db.execute("UPDATE frontier SET state='source_blocked' WHERE id=?",(job['id'],))
                        g.set('source_error:'+job['id'],result)
                except (ValueError,urllib.error.URLError,TimeoutError) as e:
                    if str(e).startswith(('CORE_','MAP_IO_','SOL_POOL_')):raise
                    # Preserve the failed source/cursor and continue other roots.
                    # No absence/ownership conclusions are derived from a failure.
                    result=dict(job_id=job['id'],state='source_blocked',error_type=type(e).__name__,error=str(e)[:120])
                    retryable=isinstance(e,TimeoutError) or (isinstance(e,urllib.error.HTTPError) and e.code in (429,502,503,504,529)) or (isinstance(e,urllib.error.URLError) and not isinstance(e,urllib.error.HTTPError))
                    attempts=g.state('source_error:'+job['id'],{}).get('attempts',0)+1
                    if retryable:
                        delay=min(1800,60*2**min(attempts-1,5))
                        retry_after=e.headers.get('Retry-After','') if isinstance(e,urllib.error.HTTPError) and e.headers else ''
                        if retry_after.isdigit():delay=max(delay,int(retry_after))
                        result.update(retryable=True,attempts=attempts,retry_at_ms=time.time_ns()//1000000+delay*1000)
                    with g.db:
                        g.db.execute("UPDATE frontier SET state='source_blocked' WHERE id=?",(job['id'],))
                        g.set('source_error:'+job['id'],result)
                finally:g.custody.leave()
                if external_pool:pool_adapter.finish_job(external_pool,g,job,result)
                outcomes.append(result)
            stage('review_retained_integrity')
            review=review_retained(g,native_scope)
            stage('project_retained_coverage')
            projection=sync_retained(g,native_scope,limit=300)
            status['evidence_review']=dict(state=review['state'],graph=review.get('graph'),coverage=projection)
            stage('render_private_overview')
            view=g.view();atomic(g.out/'map.json',view)
            stage('publish_source_snapshots')
            atomic(g.out/'custody.json',g.custody.snapshot())
            atomic(g.out/'crosschain.json',g.crosschain.snapshot())
            atomic(g.out/'raw-history.json',g.raw_history.snapshot())
            atomic(g.out/'portal-history.json',g.portal.snapshot())
            atomic(g.out/'gsfa-history.json',g.gsfa.snapshot())
            if time.time()-g.state('quality_audit_at',0)>60:
                stage('quality_audit')
                atomic(g.out/'quality.json',audit(base,native_scope.guard))
                with g.db:g.set('quality_audit_at',time.time())
            status.update(state='INVESTIGATING' if jobs else ('WAITING_EXTERNAL_RESEARCH' if external_pool else 'WAITING_FOR_SOURCE_DATA'),updated_ms=time.time_ns()//1000000,
                budget=budget.totals(),counts=view['counts'],frontier_states=view['frontier_states'],last_jobs=outcomes,
                complete_to_today=False,deadline_ms=native_scope.operating_deadline_ms if external_pool else native_scope.effective_deadline_ms)
            atomic(g.out/'status.json',status)
            stage('native_batch_complete')
            if a.once:break
            for _ in range((1 if jobs else 2) if external_pool else (10 if jobs else 30)):
                if stopping:break
                time.sleep(1)
        if stopping:status['state']='STOPPED'
    except BaseException as e:
        expired=str(e)=='SOL_POOL_DEADLINE'
        status.update(state='DEADLINE' if expired else ('STOPPED' if stopping else 'STOPPED_ERROR'),error_type=type(e).__name__,error=str(e)[:160])
        if not stopping and not expired:raise
    finally:
        status.update(updated_ms=time.time_ns()//1000000,budget=budget.totals());atomic(g.out/'status.json',status)
        if workspace:workspace.close()
        if external_pool:external_pool.close()
        budget.db.close();g.close()
        if budget.spend:budget.spend.close()
        if current_index:current_index.close()
        core_scope.close();lock.close()


if __name__=='__main__':main()
