"""Resumable provider-index cardinality ledger; no transaction-body acquisition."""
import json
from pathlib import Path
import sqlite3
import time
from .store import atomic,canonical


class Census:
    def __init__(self,out,policy):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.out/'runtime.sqlite3',timeout=15)
        self.db.row_factory=sqlite3.Row;self.policy=policy
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS jobs(epoch INTEGER PRIMARY KEY,state TEXT,attempts INTEGER,
            not_before_ms INTEGER,body TEXT);
          CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,epoch INTEGER,kind TEXT,
            reserved_bytes INTEGER,received_bytes INTEGER,state TEXT,at_ms INTEGER,body TEXT);
          CREATE TABLE IF NOT EXISTS counts(epoch INTEGER PRIMARY KEY,entries INTEGER,body TEXT);''')
        old=self.db.execute('SELECT body FROM identity').fetchall();body=canonical(policy)
        if old and (len(old)!=1 or old[0][0]!=body):
            self.db.close();raise ValueError('SIGCENSUS_POLICY_CHANGED')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(body,))
            for epoch in policy['epochs']:
                self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?)',(epoch,'QUEUED',0,0,'{}'))
            # A process crash changes neither completed evidence nor HTTP reservations.
            self.db.execute("UPDATE jobs SET state='QUEUED' WHERE state='RUNNING'")

    def close(self):self.db.close()

    def next_epoch(self,now_ms=None):
        now_ms=time.time_ns()//1000000 if now_ms is None else now_ms
        row=self.db.execute("""SELECT epoch FROM jobs WHERE state IN ('QUEUED','SOURCE_BACKOFF','PROVIDER_UNAVAILABLE')
            AND not_before_ms<=? ORDER BY attempts,CASE WHEN epoch IN (1009,1010,1008,1007,909) THEN 0 ELSE 1 END,epoch DESC LIMIT 1""",(now_ms,)).fetchone()
        return row[0] if row else None

    def begin(self,epoch):
        with self.db:
            row=self.db.execute('SELECT state FROM jobs WHERE epoch=?',(epoch,)).fetchone()
            if not row or row[0] not in ('QUEUED','SOURCE_BACKOFF','PROVIDER_UNAVAILABLE'):raise ValueError('SIGCENSUS_JOB_STATE')
            self.db.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1 WHERE epoch=?",(epoch,))

    def defer(self,epoch,state,retry_ms,body):
        if state not in ('SOURCE_BACKOFF','PROVIDER_UNAVAILABLE','FAILED_VALIDATION'):raise ValueError('SIGCENSUS_DEFER_STATE')
        with self.db:self.db.execute('UPDATE jobs SET state=?,not_before_ms=?,body=? WHERE epoch=?',
                                    (state,retry_ms,canonical(body),epoch))

    def finish(self,epoch,result):
        if result.get('epoch')!=epoch or result.get('complete_canonical_history_proven') is not False:
            raise ValueError('SIGCENSUS_COUNT_SCOPE')
        value=result.get('provider_signature_entries')
        if type(value) is not int or value<0:raise ValueError('SIGCENSUS_COUNT_VALUE')
        body=canonical(result)
        with self.db:
            old=self.db.execute('SELECT body FROM counts WHERE epoch=?',(epoch,)).fetchone()
            if old and old[0]!=body:raise ValueError('SIGCENSUS_COUNT_CHANGED')
            self.db.execute('INSERT OR IGNORE INTO counts VALUES(?,?,?)',(epoch,value,body))
            self.db.execute("UPDATE jobs SET state='COUNTED_PROVIDER_INDEX',body=? WHERE epoch=?",(body,epoch))

    def snapshot(self,state='RUNNING',**extra):
        now=time.time_ns()//1000000
        states=dict(self.db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
        n,total=self.db.execute('SELECT count(*),coalesce(sum(entries),0) FROM counts').fetchone()
        reserved,received,attempts=self.db.execute('SELECT coalesce(sum(reserved_bytes),0),coalesce(sum(received_bytes),0),count(*) FROM attempts').fetchone()
        result=dict(schema='economic-machine.signature-census.v1',at_ms=now,state=state,
            deadline_ms=self.policy['deadline_ms'],inventory_epochs=len(self.policy['epochs']),
            counted_epochs=n,unknown_epochs=len(self.policy['epochs'])-n,epoch_states=states,
            provider_signature_entries_partial=total,
            provider_index_denominator_complete=n==len(self.policy['epochs']),
            canonical_transaction_denominator_verified=False,
            source_reserved_bytes=reserved,source_received_bytes=received,source_attempts=attempts,
            counts_include_votes=True,transaction_success_unknown=True,
            all_wallet_mapping_complete=False,production_changed=False,
            **extra)
        atomic(self.out/'summary.json',result)
        return result
