"""Resumable genesis-to-head ingestion; model proposals never mutate evidence."""
from contextlib import closing
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.error

from economic_machine.pump_research.store import atomic, canonical
from .authority import authority
from .storage import Storage

UNIT='economic-machine-arbitrum-research.service'
GIB=1024**3


def cpu_sample():
    row=Path('/proc/stat').read_text().splitlines()[0].split()[1:]
    values=list(map(int,row));return sum(values[:8]),values[3]+values[4]


def used_bytes(base):
    total=0
    for parent,dirs,files in os.walk(base):
        dirs[:]=[x for x in dirs if not Path(parent,x).is_symlink()]
        for name in files:
            p=Path(parent,name)
            if not p.is_symlink():
                try:total+=p.stat().st_size
                except FileNotFoundError:pass
    return total


def working_cache_bytes(base):
    """This collector's mutable working set, not retained evidence/other jobs.

    Immutable journals, proofs, and separate snapshot workers still consume real
    disk and remain covered by the host free-space floor. They cannot be evicted
    by rotating this collector's active graph.
    """
    base=Path(base)
    return sum(used_bytes(base/name) for name in ('index','staging'))


def metrics(base,spend,backlog,backoff=0,unit=UNIT):
    first=cpu_sample();time.sleep(.2);second=cpu_sample()
    elapsed=second[0]-first[0]
    busy=100*(1-(second[1]-first[1])/elapsed) if elapsed>0 else 100
    mem={a:int(b)*1024 for a,b,*_ in (r.split() for r in Path('/proc/meminfo').read_text().splitlines())}
    io=Path('/proc/pressure/io').read_text().splitlines()[0].split()
    pressure=float(next(x.split('=')[1] for x in io if x.startswith('avg60=')))
    group=Path('/sys/fs/cgroup/system.slice')/unit/'memory.current'
    current=int(group.read_text()) if group.exists() else 0
    return dict(observed_ms=time.time_ns()//1_000_000,logical_cpus=os.cpu_count(),memory_current_bytes=current,
        cpu_busy_pct=max(0,min(100,busy)),io_pressure_pct=pressure,
        mem_available_bytes=mem['MemAvailable:'],mem_total_bytes=mem['MemTotal:'],
        free_disk_bytes=shutil.disk_usage(base).free,protected_free_bytes=128*GIB,
        arb_current_bytes=used_bytes(base),cache_current_bytes=working_cache_bytes(base),
        monthly_remaining_krw=spend.totals()['remaining_krw'],
        source_backoff_seconds=math.ceil(max(0,backoff)),backlog_blocks=max(0,backlog))


def apply_limits(policy,enabled,unit=UNIT):
    """Only this research unit is eligible; model text is never shell code."""
    if unit not in (UNIT,'economic-machine-robinhood-research.service'):raise ValueError('RESOURCE_UNIT')
    if not enabled:return {'applied':False,'reason':'validation_run'}
    quota=policy['cpu_quota_percent'];memory=policy['memory_max_bytes'];weight=policy['io_weight']
    if (not isinstance(quota,(int,float)) or not 1<=quota<=800
            or type(memory) is not int or not 64*1024**2<=memory<=32*GIB
            or type(weight) is not int or not 1<=weight<=500):
        raise ValueError('ARBITRUM_RESOURCE_APPLY_BOUND')
    call=subprocess.run(['systemctl','set-property','--runtime',unit,
        f'CPUQuota={quota}%',f'MemoryMax={memory}',f'IOWeight={weight}'],capture_output=True,timeout=20)
    if call.returncode:raise ValueError('ARBITRUM_RESOURCE_APPLY_FAILED')
    observed=subprocess.check_output(['systemctl','show',unit,
        '--property=CPUQuotaPerSecUSec,MemoryMax,IOWeight'],text=True,timeout=10).splitlines()
    return dict(applied=True,unit=unit,observed=observed)


def investigation_jobs(page,snapshot):
    """Thirty distinct audit questions over actual retained source records."""
    tasks=[('coverage','missing receipt status'),('coverage','transaction index continuity'),
        ('coverage','trace availability by transaction'),('coverage','log and transaction attribution'),
        ('coverage','parent hash continuity'),('coverage','Classic special transaction shapes'),
        ('graph','native value routes'),('graph','contract deployment funding'),
        ('graph','reverted call exclusion'),('graph','selfdestruct balance transfers'),
        ('graph','repeated sender participation'),('graph','high fanout recipients'),
        ('protocol','ERC20 transfer events'),('protocol','ERC721 token identifiers'),
        ('protocol','ERC1155 batch transfer sizes'),('protocol','unknown event topics'),
        ('protocol','contract method selectors'),('protocol','router and aggregator call nesting'),
        ('protocol','liquidity and swap event candidates'),('protocol','lending and repayment candidates'),
        ('protocol','derivative protocol interaction candidates'),('protocol','mint and burn semantics'),
        ('custody','exchange-like consolidation candidates without ownership attribution'),
        ('custody','deposit-like fanin candidates without identity claims'),
        ('custody','withdrawal-like fanout candidates without same-funds claims'),
        ('custody','private exchange ledger gaps'),('bridge','L1 ingress candidates'),
        ('bridge','L2 egress candidates'),('bridge','retryable and system transaction candidates'),
        ('bridge','counterpart chain evidence requirements')]
    blocks=page['blocks'];summaries=[]
    # Small real sample, with exact source object ID. Full material remains in Blob.
    records=[]
    for block in blocks:
        header=block.get('header',{})
        for table in ('transactions','logs','traces'):
            for row in block.get(table,[])[:3]:
                item=dict(block=header.get('number'),table=table,record=row)
                if len(canonical(item))<1800:records.append(item)
        if len(records)>=18:break
    sample=records[:18]
    for i,(kind,question) in enumerate(tasks):
        summaries.append(dict(job_id=f"{page['first_block']}-{page['last_block']}:{i:02d}",
            source_ids=[page['sha256']],kind=kind,
            records=dict(question=question,range=[page['first_block'],page['last_block']],
                indexed=snapshot,sample=sample[i%max(1,len(sample))::6][:3],
                evidence_level='provider_records_not_independent_consensus',
                all_protocols_requested=True,provider_table_completeness_verified=False,
                private_exchange_ledgers=False)))
    return summaries


class Runtime:
    chain_id=42161
    source_name="SQD public provider"
    def __init__(self,base,shared_base,key_path,*,resource_apply=False):
        from .source import PublicSource
        from .muse import MuseFleet
        self.base=Path(base);self.shared=Path(shared_base)
        loader=authority
        if self.chain_id==4663:
            from economic_machine.robinhood_research.authority import authority as loader
        self.grant,self.grant_hash=loader(base,shared_base)
        self.out=self.base/'evidence';self.resource_apply=resource_apply;self.stop=False
        self.resource_reapply_pending=resource_apply
        self.lock=(self.out/'worker.lock').open('a+b');fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.db=sqlite3.connect(self.out/'runtime.sqlite3');self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS identity(hash TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(start INTEGER PRIMARY KEY,end INTEGER NOT NULL,
              requested_end INTEGER NOT NULL,sha TEXT NOT NULL,proof TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,start INTEGER,end INTEGER,
              state TEXT,at_ms INTEGER,reason TEXT);
            CREATE TABLE IF NOT EXISTS pipeline_metrics(attempt INTEGER PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shards(start INTEGER PRIMARY KEY,end INTEGER,tip_hash TEXT,
              state TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS investigation_checks(id INTEGER PRIMARY KEY,batch_id TEXT,
              job_id TEXT,kind TEXT,status TEXT,body TEXT,UNIQUE(batch_id,job_id,kind));
            CREATE TABLE IF NOT EXISTS audits(source_sha TEXT PRIMARY KEY,outcome TEXT,at_ms INTEGER,proof TEXT);
            CREATE TABLE IF NOT EXISTS coverage_gaps(block INTEGER,tx_hash TEXT,kind TEXT,
              source_sha TEXT,status TEXT,PRIMARY KEY(block,tx_hash,kind));''')
        with self.db:
            old=self.db.execute('SELECT hash FROM identity').fetchall()
            if old and [r[0] for r in old]!=[self.grant_hash]:raise ValueError('ARBITRUM_JOURNAL_AUTHORITY')
            self.db.execute('INSERT OR IGNORE INTO identity VALUES(?)',(self.grant_hash,))
            self.db.execute('INSERT OR IGNORE INTO state VALUES(1,?)',(canonical(dict(
                next_block=0,shard_start=0,previous_shard_hash=None,head=None,
                backoff_until=0,last_manager_ms=0,last_investigation_ms=0,page_count=0)),))
        self.state=json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        if self.chain_id==4663:
            from economic_machine.robinhood_research.source import PublicSource
        self.source=PublicSource()
        self.storage=Storage(base,shared_base,chain_id=self.chain_id) if self.chain_id!=42161 else Storage(base,shared_base)
        from .parallel import load_plan,CoordinatedSource
        self.parallel_plan=load_plan(base) if self.chain_id==42161 else None
        if self.parallel_plan:
            self.source=CoordinatedSource(self.source,self.out/'parallel-source.sqlite3')
        self.fleet=(MuseFleet(base,shared_base,key_path,chain_id=self.chain_id) if self.chain_id!=42161 else MuseFleet(base,shared_base,key_path));self.graph=None
        self.recover_shard();self.open_graph()

    def save(self):
        with self.db:self.db.execute('UPDATE state SET body=? WHERE id=1',(canonical(self.state),))

    def open_graph(self):
        from .graph import Graph
        (self.base/'index').mkdir(exist_ok=True)
        self.graph_path=self.base/'index'/f"{self.state['shard_start']:012d}.sqlite3"
        self.graph=Graph(self.graph_path,initial_start=self.state['shard_start'],
            previous_block_hash=self.state['previous_shard_hash'],chain_id=self.chain_id)

    def recover_shard(self):
        pending=self.db.execute("SELECT * FROM shards WHERE state='sealing'").fetchone()
        if pending is None:
            # Recover a crash after sealing was journaled but before switching the active shard.
            sealed=self.db.execute("SELECT * FROM shards WHERE state='sealed' AND start=?",
                (self.state['shard_start'],)).fetchone()
            if sealed:
                self.state.update(shard_start=sealed['end']+1,previous_shard_hash=sealed['tip_hash'])
                self.save()
            return
        body=json.loads(pending['body']);path=self.base/'index'/body['filename']
        if path.exists():result=self.storage.archive_index(path,body['snapshot'])
        else:
            target=self.out/'shards'/(body['sha256']+'.json')
            result=json.loads(target.read_bytes())
            actual=json.loads(self.storage.read(result['cloud_proof']))
            if actual!=result['manifest']:raise ValueError('ARBITRUM_SHARD_RECOVERY_PROOF')
        if result['manifest']['sha256']!=body['sha256']:raise ValueError('ARBITRUM_SHARD_RECOVERY_HASH')
        with self.db:self.db.execute("UPDATE shards SET state='sealed',body=? WHERE start=?",
            (canonical(result),pending['start']))
        self.state.update(shard_start=pending['end']+1,previous_shard_hash=pending['tip_hash'])
        self.save()

    def seal(self):
        snapshot=self.graph.snapshot()
        if snapshot.get('covered_last_block') is None:return
        self.graph.close();self.graph=None
        # Closing the last SQLite connection checkpoints WAL. Verify no live WAL remains.
        wal=Path(str(self.graph_path)+'-wal')
        if wal.exists() and wal.stat().st_size:raise ValueError('ARBITRUM_SHARD_WAL_NOT_CHECKPOINTED')
        with self.graph_path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        with self.db:self.db.execute('INSERT INTO shards VALUES(?,?,?,?,?)',
            (self.state['shard_start'],snapshot['covered_last_block'],snapshot['tip_hash'],'sealing',
             canonical(dict(filename=self.graph_path.name,sha256=digest,snapshot=snapshot))))
        self.recover_shard();self.open_graph()

    def publish(self,state,**extra):
        totals=self.storage.cloud.spend.totals()
        pages=self.db.execute("SELECT count(*),coalesce(sum(end-start+1),0) FROM pages WHERE state='indexed'").fetchone()
        active=self.graph.snapshot() if self.graph else None
        value=dict(schema='economic-machine.arbitrum-live-status.v1',at_ms=time.time_ns()//1_000_000,
            state=state,chain_id=self.chain_id,grant_sha256=self.grant_hash,start_block=0,
            next_block=self.state['next_block'],indexed_through=self.state['next_block']-1,
            provider_finalized_head=self.state.get('head'),pages=pages[0],contiguous_blocks_indexed=pages[1],
            active_index=active,sealed_shards=self.db.execute("SELECT count(*) FROM shards WHERE state='sealed'").fetchone()[0],
            original_source=self.source_name,independent_consensus_verified=False,
            whole_history_complete=False,private_exchange_ledger_available=False,
            all_protocols_selected=True,model='meta/muse-spark-1.3-contributor',
            configured_investigators=(0 if os.environ.get('EM_RESEARCH_POOL_ROOT') else (10 if self.chain_id==4663 else 30)),
            configured_resource_managers=(0 if os.environ.get('EM_RESEARCH_POOL_ROOT') or self.chain_id==4663 else 1),
            research_dispatch=('external_shared_50_lane_pool' if os.environ.get('EM_RESEARCH_POOL_ROOT') else 'inline'),
            shared_research_lane_limit=(50 if os.environ.get('EM_RESEARCH_POOL_ROOT') else None),budget=totals,
            assigned_range=(self.parallel_plan['ranges'][0] if self.parallel_plan else None),
            policy=self.state.get('policy'),resource_application=self.state.get('resource_application'),**extra)
        atomic(self.out/'live-status.json',value)
        return value

    def manage(self,force=False):
        from .governor import govern,conservative_proposal
        now=time.time_ns()//1_000_000
        if not force and now-self.state['last_manager_ms']<300_000:return
        measured=metrics(self.base,self.storage.cloud.spend,
            (self.state.get('head') or 0)-self.state['next_block']+1,self.state['backoff_until']-time.time())
        # The external pool admits research independently of source-page latency.
        # Resource admission still runs here with fresh native measurements and
        # the last retained proposal (or the deterministic fallback).
        result=(dict(status='external_research_pool',proposal=None)
                if os.environ.get('EM_RESEARCH_POOL_ROOT') else self.fleet.manage(measured))
        # Muse returns declarative policy only; native admission applies current capacity.
        proposal=result.get('proposal')
        fallback=proposal is None
        if time.time_ns()//1_000_000-measured['observed_ms']>120000:
            measured=metrics(self.base,self.storage.cloud.spend,
                (self.state.get('head') or 0)-self.state['next_block']+1,self.state['backoff_until']-time.time())
            fallback=True
        if fallback:
            if self.state.get('manager_proposal'):
                from .governor import metric_source_id
                proposal=dict(self.state['manager_proposal'])
                proposal['source_ids']=[metric_source_id(measured)]
            else:proposal=conservative_proposal(measured)
        policy=govern(proposal,measured)
        # One durable writer consumes prefetched pages in block order.
        policy['effective_download_concurrency']=1
        policy['manager_response_applied']=not fallback
        self.state.update(policy=policy,applied_policy=policy,manager_proposal=proposal,last_manager_ms=now,
            resource_application=apply_limits(policy,self.resource_apply))
        self.resource_reapply_pending=False
        self.save();atomic(self.out/'resource-manager.json',dict(at_ms=now,metrics=measured,model=result,applied=policy,
            resource_application=self.state['resource_application']))

    def reclamp(self):
        """Recheck fresh host capacity without claiming the model saw new data."""
        from .governor import govern,validate_metrics,metric_source_id
        if 'manager_proposal' not in self.state:return
        current=validate_metrics(metrics(self.base,self.storage.cloud.spend,
            (self.state.get('head') or 0)-self.state['next_block']+1,self.state['backoff_until']-time.time()))
        original=self.state['manager_proposal'];proposal=dict(original)
        proposal['source_ids']=[metric_source_id(current)]
        policy=govern(proposal,current)
        if self.state.get('source_batch_cap'):
            policy['download_batch_blocks']=min(policy['download_batch_blocks'],self.state['source_batch_cap'])
        policy['schedule_delay_seconds']=max(policy['schedule_delay_seconds'],self.state.get('source_min_delay',1))
        policy.update(native_recheck=True,original_model_source_ids=original['source_ids'],
            effective_download_concurrency=1,
            manager_response_applied=self.state.get('policy',{}).get('manager_response_applied',False))
        old=self.state.get('applied_policy',self.state.get('policy',{}))
        changed=policy['io_weight']!=old.get('io_weight') or self.resource_reapply_pending
        for key in ('cpu_quota_percent','memory_max_bytes'):
            prior=old.get(key,0);value=policy[key]
            changed=changed or prior==0 or value<prior*.9 or value>prior*1.25
        if changed:
            self.state['resource_application']=apply_limits(policy,self.resource_apply)
            self.state['applied_policy']=policy
            self.resource_reapply_pending=False
        self.state['policy']=policy;self.save()

    def accelerate(self):
        from .acceleration import accelerate
        p=self.state.get('policy',{})
        if not p:return
        # These pressure reasons were computed from the current native sampler.
        reasons=set(p.get('reasons',[]))
        measured=dict(cpu_busy_pct=85 if 'HOST_CPU_PRESSURE' in reasons else 0,
            io_pressure_pct=20 if 'HOST_IO_PRESSURE' in reasons else 0,
            source_backoff_seconds=max(0,self.state.get('backoff_until',0)-time.time()),
            monthly_remaining_krw=self.storage.cloud.spend.totals()['remaining_krw'])
        self.state['policy']=accelerate(p,measured,self.state,chain_id=self.chain_id,
            enabled=os.environ.get('EM_HISTORY_FAST')=='1')
        self.save()

    def guard(self,allow_seal=True):
        remaining=self.storage.cloud.spend.totals()['remaining_krw']
        if remaining<=0:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
        if shutil.disk_usage(self.base).free<128*GIB+256*1024**2:
            raise ValueError('ARBITRUM_HOST_FREE_RESERVE')
        policy=self.state.get('policy') or {}
        cap=policy.get('cache_limit_bytes',0)
        if cap and working_cache_bytes(self.base)>cap:
            if allow_seal and self.graph_path.stat().st_size>8*1024**2:self.seal()
            if working_cache_bytes(self.base)>cap:raise ValueError('ARBITRUM_DYNAMIC_CACHE_PRESSURE')

    def commit(self,row):
        from .source import parse_page
        if self.chain_id==4663:
            from economic_machine.robinhood_research.source import parse_page
        proof=json.loads(row['proof']);raw=self.storage.read(proof)
        page=parse_page(raw,row['start'],row['requested_end'])
        if page['sha256']!=row['sha'] or page['last_block']!=row['end']:
            raise ValueError('ARBITRUM_PENDING_PAGE_IDENTITY')
        self.graph.ingest(page['blocks'],page['sha256'],row['start'],row['end'],proof)
        with self.db:
            for block in page['blocks']:
                traced={t.get('transactionIndex') for t in block['traces']}
                for tx in block['transactions']:
                    for missing in (["RECEIPT_STATUS"] if tx.get('status') is None else [])+(
                            ["TRACE_ROWS"] if tx['transactionIndex'] not in traced else []):
                        self.db.execute('INSERT OR IGNORE INTO coverage_gaps VALUES(?,?,?,?,?)',
                            (block['header']['number'],tx['hash'],missing,page['sha256'],'UNRESOLVED'))
            self.db.execute("UPDATE pages SET state='indexed' WHERE start=?",(row['start'],))
            self.state['next_block']=row['end']+1
            if self.chain_id==4663 and page.get('retry_after_seconds'):
                self.state['backoff_until']=max(self.state['backoff_until'],time.time()+page['retry_after_seconds'])
            self.state['page_count']=self.db.execute("SELECT count(*) FROM pages WHERE state='indexed'").fetchone()[0]
            self.db.execute('UPDATE state SET body=? WHERE id=1',(canonical(self.state),))
        return page

    def one(self):
        if self.state.get('integrity_halt'):raise ValueError(self.state['integrity_halt'])
        pending=self.db.execute("SELECT * FROM pages WHERE state='archived' ORDER BY start LIMIT 1").fetchone()
        if pending:
            # Finish the same shard before pressure handling can rotate it.
            if self.storage.cloud.spend.totals()['remaining_krw']<=0:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
            return self.commit(pending)
        if self.state.get('head') is None or time.time()-self.state.get('head_checked',0)>300:
            self.state.update(head=self.source.head(),head_checked=time.time());self.save()
        if self.storage.cloud.spend.totals()['remaining_krw']<=0:raise ValueError('RESEARCH_MONTHLY_BUDGET_LIMIT')
        self.manage(force='policy' not in self.state)
        self.reclamp()
        self.accelerate()
        self.guard()
        policy=self.state['policy']
        if policy.get('state') not in ('RUN','RUNNING','ADMITTED','ACTIVE'):
            self.publish('RESOURCE_PAUSED',reason=policy.get('state'));self.sleep(30);return None
        start=self.state['next_block'];head=self.state['head']
        if self.parallel_plan:
            head=min(head,self.parallel_plan['ranges'][0]['end'])
            if start>head:
                self.publish('ASSIGNED_RANGE_COMPLETE');self.sleep(30);return None
        if start>head:self.publish('AT_PROVIDER_HEAD');self.sleep(15);return None
        end=min(head,start+policy['download_batch_blocks']-1)
        with self.db:attempt=self.db.execute('INSERT INTO attempts(start,end,state,at_ms) VALUES(?,?,?,?)',
            (start,end,'reserved',time.time_ns()//1_000_000)).lastrowid
        self.publish('FETCHING',requested_start=start,requested_end=end)
        began=time.monotonic()
        try:page=self.source.fetch(start,end)
        except BaseException as e:
            reason=dict(error_type=type(e).__name__,status=getattr(e,'status',None),
                retry_after_seconds=getattr(e,'retry_after_seconds',None))
            with self.db:self.db.execute('UPDATE attempts SET state=?,reason=? WHERE id=?',('failed',canonical(reason),attempt))
            raise
        fetched=time.monotonic()
        proof=self.storage.put(page['raw'],'source_page',dict(provider=self.source_name,
            requested_start=start,requested_end=end,first_block=page['first_block'],last_block=page['last_block']))
        archived=time.monotonic()
        with self.db:
            self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)',
                (start,page['last_block'],end,page['sha256'],canonical(proof),'archived'))
            self.db.execute("UPDATE attempts SET state='archived' WHERE id=?",(attempt,))
        row=self.db.execute('SELECT * FROM pages WHERE start=?',(start,)).fetchone()
        committed=self.commit(row)
        timing=dict(at_ms=time.time_ns()//1000000,source_sha256=page['sha256'],first_block=start,
            last_block=page['last_block'],fetch_parse_seconds=fetched-began,
            archive_seconds=archived-fetched,index_commit_seconds=time.monotonic()-archived,
            source_transport=page.get('response'),source_raw_bytes=len(page['raw']),
            indexed_after_blob_verification=True)
        with self.db:self.db.execute('INSERT INTO pipeline_metrics VALUES(?,?)',(attempt,canonical(timing)))
        atomic(self.out/'latest-pipeline.json',timing)
        return committed

    def investigate(self,page):
        if os.environ.get('EM_RESEARCH_POOL_ROOT'):
            return
        now=time.time_ns()//1_000_000
        if now-self.state['last_investigation_ms']<600_000:return
        cap=10 if self.chain_id==4663 else 30
        workers=min(cap,self.state.get('policy',{}).get('model_research_workers',cap))
        if workers==0:return
        jobs=investigation_jobs(page,self.graph.snapshot())
        if self.chain_id==4663:
            jobs=[jobs[i] for i in (0,2,6,12,16,21,23,26,28,29)]
            for job in jobs:
                job['records']['chain_id']=4663
                job['records']['crosschain']=self.bridge.snapshot()
                job['records']['stock_token_scaled_units']='raw amounts only; corporate action multiplier not applied'
        result=self.fleet.investigate(jobs,max_workers=workers)
        with self.db:
            for answer in result['results']:
                proposal=answer.get('proposal')
                if answer.get('status')!='valid' or not proposal:continue
                for check in proposal['next_checks']:
                    status=('SOURCE_BYTES_RECHECKED' if check=='verify_source' else
                        'PROVIDER_RANGE_RETAINED' if check=='fetch_blocks' else
                        'PROVIDER_TRACES_RETAINED_COMPLETENESS_UNVERIFIED' if check=='fetch_traces' else
                        'WAITING_INDEPENDENT_RECEIPTS' if check=='fetch_receipts' else
                        'TOKEN_STANDARDS_INDEXED_UNKNOWN_PROTOCOL_ABI_PENDING' if check=='decode_protocol' else
                        'GENESIS_SCAN_PENDING' if check=='follow_address' else
                        'WAITING_COUNTERPART_CHAIN' if check=='verify_bridge' else 'GAP_RETAINED')
                    self.db.execute('INSERT OR IGNORE INTO investigation_checks(batch_id,job_id,kind,status,body) VALUES(?,?,?,?,?)',
                        (result['batch_id'],answer['job_id'],check,status,canonical(proposal)))
        atomic(self.out/'latest-investigation.json',result)
        self.state['last_investigation_ms']=now;self.save()

    def audit(self,page):
        from .audit import audit_page
        now=time.time()
        if now<self.state.get('audit_not_before',0) or now-self.state.get('last_audit_at',0)<600:return
        result=audit_page(page,max_transactions=1,
            probe_traces=now>=self.state.get('trace_probe_not_before',0))
        proof=self.storage.put(result['raw'],'official_rpc_audit',dict(source_sha256=page['sha256']))
        with self.db:self.db.execute('INSERT OR REPLACE INTO audits VALUES(?,?,?,?)',
            (page['sha256'],result['outcome'],result['at_ms'],canonical(proof)))
        for record in result['records']:
            if (record['request']['method']=='debug_traceTransaction'
                    and isinstance(record.get('rpc_error'),dict) and record['rpc_error'].get('code')==-32601):
                self.state['trace_probe_not_before']=now+86400
        self.state.update(last_audit_at=now,audit_not_before=now+result.get('retry_after_seconds',0))
        self.save()
        atomic(self.out/'latest-audit.json',dict(outcome=result['outcome'],block_checks=result['block_checks'],
            transaction_checks=result['transaction_checks'],gap_count=len(result['original_gaps']),
            at_ms=result['at_ms'],proof=proof,independent_consensus_verified=False))
        if result['outcome']=='MISMATCH':
            self.state['integrity_halt']='ARBITRUM_INDEPENDENT_PROVIDER_MISMATCH';self.save()
            raise ValueError(self.state['integrity_halt'])

    def sleep(self,seconds):
        end=time.monotonic()+min(3600,max(0,seconds))
        while not self.stop and time.monotonic()<end:time.sleep(min(1,max(0,end-time.monotonic())))

    def scheduled_audit(self,page):
        from .source import SourceBackoff,SourceHTTPError
        if time.time()<self.state.get('audit_not_before',0):return
        try:self.audit(page)
        except (SourceBackoff,SourceHTTPError,urllib.error.URLError,TimeoutError) as error:
            self.state['audit_not_before']=time.time()+max(60,getattr(error,'retry_after_seconds',60))
            self.save()
            atomic(self.out/'audit-retry.json',dict(at_ms=time.time_ns()//1000000,
                state='INDEPENDENT_AUDIT_PENDING',source_sha256=page['sha256'],
                retry_at=self.state['audit_not_before'],error_type=type(error).__name__,
                ingestion_source_backoff_unchanged=True))

    def run(self,max_pages=None):
        from .source import SourceBackoff,SourceLimit
        done=0
        while not self.stop:
            try:
                if self.state['backoff_until']>time.time():
                    self.publish('SOURCE_BACKOFF',retry_at_ms=int(self.state['backoff_until']*1000))
                    self.sleep(min(30,self.state['backoff_until']-time.time()));continue
                page=self.one()
                if page is None:continue
                self.investigate(page)
                self.scheduled_audit(page)
                self.state['successful_pages_since_backoff']=self.state.get('successful_pages_since_backoff',0)+1
                if self.state['successful_pages_since_backoff']>=(2 if os.environ.get('EM_HISTORY_FAST')=='1' else 8):
                    self.state['source_min_delay']=max(1,self.state.get('source_min_delay',1)//2)
                    self.state['successful_pages_since_backoff']=0
                self.save()
                self.publish('INDEXING')
                if self.graph_path.stat().st_size>128*1024**2:self.seal()
                done+=1
                if max_pages is not None and done>=max_pages:break
                self.sleep(max(1,self.state.get('policy',{}).get('schedule_delay_seconds',1)))
            except SourceBackoff as e:
                self.state['backoff_until']=time.time()+max(1,e.retry_after_seconds);self.save()
                if e.status!=-1:
                    self.state['source_min_delay']=min(30,max(2,self.state.get('source_min_delay',1)*2))
                    self.state['successful_pages_since_backoff']=0
                    self.save()
                self.publish('SOURCE_BACKOFF',reason=type(e).__name__)
            except SourceLimit:
                p=self.state.get('policy',{})
                if p.get('download_batch_blocks',1)<=1:raise
                self.state['source_batch_cap']=max(1,p['download_batch_blocks']//2)
                p['download_batch_blocks']=self.state['source_batch_cap'];self.save()
            except (urllib.error.URLError,TimeoutError) as e:
                self.publish('NETWORK_RETRY',reason=type(e).__name__);self.sleep(60)
            except Exception as e:
                if isinstance(e,ValueError) and str(e) in (
                        'RESEARCH_MONTHLY_BUDGET_LIMIT','RESEARCH_CLOUD_CAPACITY_LIMIT',
                        'ARBITRUM_HOST_FREE_RESERVE','ARBITRUM_DYNAMIC_CACHE_PRESSURE',
                        'RESEARCH_CLOUD_LOCAL_STORAGE_LIMIT','RESEARCH_CLOUD_LOCAL_FREE_SPACE'):
                    self.publish('RESOURCE_PAUSED',reason=str(e));self.sleep(60);continue
                self.publish('STOPPED',reason=str(e)[:180],error_type=type(e).__name__);raise
        self.publish('STOPPED_BY_SIGNAL' if self.stop else 'BOUNDED_RUN_COMPLETE')

    def close(self):
        if self.graph:self.graph.close()
        close=getattr(self.fleet,'close',None)
        if close:close()
        self.storage.close();self.db.close();self.lock.close()
