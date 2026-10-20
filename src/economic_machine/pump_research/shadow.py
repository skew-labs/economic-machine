"""Bounded offline/prospective research worker on retained public evidence.

Network disabled in service. No signing, orders, RPC, provider keys or codegen.
"""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import time
from .store import Store, atomic, canonical
from .strategy import CONFIG, evaluate
from .events import valuation_reason

def code_hash():
    return hashlib.sha256(b''.join((Path(__file__).parent/n).read_bytes() for n in
                                  ('events.py','strategy.py','shadow.py','wire.py','archive.py','store.py','object_store.py','ticking.py','originals.py','io_budget.py'))).hexdigest()

def schema(s):
    s.db.executescript('''
    CREATE TABLE IF NOT EXISTS event_objects_v4(evidence TEXT PRIMARY KEY,status TEXT,events INTEGER,errors TEXT);
    CREATE TABLE IF NOT EXISTS trade_events_v4(observation TEXT,event_n INTEGER,mint TEXT,slot INTEGER,body TEXT,PRIMARY KEY(observation,event_n));
    CREATE INDEX IF NOT EXISTS event_v4_mint_slot ON trade_events_v4(mint,slot);
    CREATE INDEX IF NOT EXISTS observations_source_slot ON observations(source,slot);
    CREATE TABLE IF NOT EXISTS shadow_results(plan TEXT,cohort TEXT,strategy TEXT,delay INTEGER,body TEXT,updated REAL,PRIMARY KEY(plan,cohort,strategy,delay));
    ''')
    if s.db.execute("SELECT type FROM sqlite_master WHERE name='transfers'").fetchone()[0]=='table':
        s.db.execute('CREATE INDEX IF NOT EXISTS transfers_slot ON transfers(slot)')

def protocol(s,source_head_slot=None):
    path=s.root/'strategy-protocol-v6.json'
    if not path.exists():
        ticking=s.state('ticking') or {};now=time.time()
        scope=json.loads((s.root/'research-model-scope.json').read_bytes())
        p=dict(config=CONFIG,registered_at=now,registered_slot=max(ticking.get('last_slot',0),source_head_slot or 0),
               deadline=scope['deadline_ms']/1000,code_sha256=code_hash(),
               cohort='all observed Pump creates, including zero-trade launches; source-filtered, not whole chain',
               study='prospective frozen-rule replay, not real-time fills',
               thresholds='pre-registered hypotheses, not fitted profit estimates',
               rank_used_for_admission=False,trading_enabled=False)
        p['supersedes']='v3/v4/v5 preserved; trading thresholds unchanged; shared original references and authenticated bounded cold reads'
        p['id']=hashlib.sha256(canonical(p).encode()).hexdigest();atomic(path,p)
    p=json.loads(path.read_bytes())
    if p['id']!=hashlib.sha256(canonical({k:v for k,v in p.items() if k!='id'}).encode()).hexdigest() or p['config']!=CONFIG:
        raise ValueError('FROZEN_PROTOCOL_IDENTITY')
    if p['code_sha256']!=code_hash():raise ValueError('FROZEN_PROTOCOL_CODE_CHANGED_REQUIRES_NEW_VERSION')
    return p

def index_objects(s,pb,meta_type,maximum=1000):
    from .wire import from_geyser
    from .archive import parse_block
    rows=s.db.execute('''SELECT DISTINCT e.* FROM evidence e JOIN observations o ON o.evidence=e.id
        LEFT JOIN event_objects_v4 done ON done.evidence=e.id
        WHERE done.evidence IS NULL AND (EXISTS(SELECT 1 FROM activity a WHERE a.observation=o.id)
        OR EXISTS(SELECT 1 FROM launches l WHERE l.observation=o.id))
        ORDER BY e.rowid LIMIT ?''',(maximum,)).fetchall()
    count=0
    for ev in rows:
        s.guard();raw=s.read_object(ev['hash'])
        if hashlib.sha256(raw).hexdigest()!=ev['hash']:raise ValueError('EVENT_SOURCE_HASH')
        errors=[];txs=[]
        if ev['source']=='ticking':
            u=pb.SubscribeUpdate.FromString(raw);kind=u.WhichOneof('update_oneof')
            infos=[(u.transaction.transaction,u.transaction.slot)] if kind=='transaction' else [(i,u.block.slot) for i in u.block.transactions] if kind=='block' else []
            for info,slot in infos:
                if info.is_vote:continue
                try:txs.append(from_geyser(info,slot))
                except (ValueError,IndexError) as e:errors.append(str(e))
        elif ev['source']=='old-faithful':
            signatures={r[0] for r in s.db.execute('''SELECT o.signature FROM observations o WHERE o.evidence=? AND
                (EXISTS(SELECT 1 FROM activity a WHERE a.observation=o.id) OR EXISTS(SELECT 1 FROM launches l WHERE l.observation=o.id))''',(ev['id'],))}
            block=parse_block(raw,int(ev['locator'].split(':')[1]),meta_type,signatures=signatures)
            txs=block['transactions'];errors.extend(block['decode_errors'])
        else:continue
        added=0
        with s.db:
            for tx in txs:
                o=s.db.execute('SELECT id FROM observations WHERE evidence=? AND signature=? AND slot=?',
                               (ev['id'],tx['signature'],tx['slot'])).fetchone()
                if not o:continue
                errors.extend(tx.get('event_errors',[]))
                for e in tx.get('events',[]):
                    e.update(slot=tx['slot'],tx_order=tx['index'],signature=tx['signature'],observed=ev['observed'])
                    added+=s.db.execute('INSERT OR IGNORE INTO trade_events_v4 VALUES(?,?,?,?,?)',
                                       (o['id'],e['event_n'],e['mint'],tx['slot'],canonical(e))).rowcount
            s.db.execute('INSERT INTO event_objects_v4 VALUES(?,?,?,?)',
                         (ev['id'],'partial' if errors else 'decoded',added,canonical(errors[:100])))
        count+=added
    return dict(objects=len(rows),new_events=count)

def funders_before(s,launch,buyers):
    # Query causal predecessors of decision-time buyers, then each predecessor's
    # observed fanout. Unrelated network traffic no longer consumes the edge cap.
    buyers=sorted(set(buyers))
    if len(buyers)>512:return {},True
    links={};checked={};remaining=5000
    clause="""t.mint='SOL' AND t.kind='system_transfer' AND t.slot>=? AND t.slot<?
        AND o.finality!='dead_branch' AND e.observed<=? AND CAST(t.amount AS INTEGER)>=10000000"""
    bounds=(max(0,launch['slot']-10000),launch['slot'],launch['observed'])
    join='FROM transfers t JOIN observations o ON o.id=t.observation JOIN evidence e ON e.id=o.evidence'
    for buyer in buyers:
        rows=s.db.execute(f'''SELECT t.source {join} WHERE t.destination=? AND {clause}
            ORDER BY t.slot DESC,o.tx_order DESC,t.ix DESC LIMIT ?''',(buyer,*bounds,remaining+1)).fetchall()
        if len(rows)>remaining:return {},True
        remaining-=len(rows)
        if not rows:continue
        src=rows[0]['source']
        if src not in checked:
            checked[src]=len(s.db.execute(f'''SELECT DISTINCT t.destination {join}
                WHERE t.source=? AND {clause} LIMIT 33''',(src,*bounds)).fetchall())<=32
        if checked[src]:links[buyer]=src
    return links,False

def population(s,p):
    rows=s.db.execute('''SELECT l.*,o.signature,o.tx_order,o.source,o.finality,e.observed
        FROM launches l JOIN observations o ON o.id=l.observation JOIN evidence e ON e.id=o.evidence
        WHERE o.finality!='dead_branch' ORDER BY l.slot,o.tx_order,l.ix''').fetchall()
    seen=set();out=[]
    for r in rows:
        identity=(r['source'],r['mint'])
        if identity in seen:continue
        seen.add(identity);v=dict(r)
        v['cohort']=hashlib.sha256(canonical(identity).encode()).hexdigest()
        v['split']='prospective' if v['source']=='ticking' and v['slot']>p['registered_slot'] and p['registered_at']<=v['observed']<=p['deadline'] else 'exploratory'
        out.append(v)
    return out

def run(root,ticking_root,maximum=1000):
    from .cli import proto
    s=Store(root);lock=(s.root/'shadow.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    schema(s)
    producer=json.loads((Path(ticking_root)/'data/result-archive/status.json').read_bytes())
    head=producer.get('lastSlot')
    if type(head) is not int or head<=0:raise ValueError('SOURCE_REGISTRATION_WATERMARK')
    p=protocol(s,head);pb,meta_type=proto(ticking_root)
    if time.time()>p['deadline']:
        path=s.root/'strategy-status.json'
        report=json.loads(path.read_bytes()) if path.exists() else dict(protocol_id=p['id'],live_trades=0,validated_pnl=None)
        report.update(state='RESEARCH_SCOPE_ENDED',at=time.time());atomic(path,report);return report
    indexed=index_objects(s,pb,meta_type,maximum)
    launches=population(s,p)
    if len(launches)>5000:raise ValueError('SHADOW_COHORT_LIMIT_5000')
    totals=Counter();reasons=Counter();layouts=Counter();sources=Counter();provisional=Counter();examples=[]
    unavailable=Counter();funding_queries=Counter();funding_seconds=0;evidence_paths=Counter()
    source_watermarks={r[0]:r[1] for r in s.db.execute("SELECT source,max(slot) FROM observations WHERE finality!='dead_branch' GROUP BY source")}
    for r in s.db.execute('SELECT body FROM trade_events_v4'):
        e=json.loads(r[0]);layouts[valuation_reason(e) or 'supported_sol_curve']+=1
        evidence_paths[e.get('event_evidence','unclassified')]+=1
    samples={};cfg=p['config']
    for launch in launches:
        backlog=s.db.execute('''SELECT count(DISTINCT e.id) FROM evidence e JOIN observations o ON o.evidence=e.id
            LEFT JOIN event_objects_v4 d ON d.evidence=e.id WHERE d.evidence IS NULL AND o.source=? AND o.slot>=? AND o.slot<=?
            AND (EXISTS(SELECT 1 FROM activity a WHERE a.observation=o.id AND a.mint=?)
            OR EXISTS(SELECT 1 FROM launches l WHERE l.observation=o.id AND l.mint=?))''',
            (launch['source'],launch['slot'],launch['slot']+500,launch['mint'],launch['mint'])).fetchone()[0]
        rows=s.db.execute('''SELECT t.body,o.finality FROM trade_events_v4 t JOIN observations o ON o.id=t.observation
            WHERE t.mint=? AND o.source=? AND o.finality!='dead_branch' AND t.slot>=? AND t.slot<=?
            ORDER BY t.slot,o.tx_order''',(launch['mint'],launch['source'],launch['slot'],launch['slot']+500)).fetchall()
        unique={};conflict=False
        for r in rows:
            e=json.loads(r[0]);key=e['signature'],e['ix']
            if key in unique and unique[key]['raw_sha256']!=e['raw_sha256']:conflict=True
            if key not in unique or e['observed']<unique[key]['observed']:unique[key]=e
            provisional[r['finality']]+=1
        events=list(unique.values())
        initial=[e for e in events if (e['slot'],e['tx_order'],e['ix'])>=(launch['slot'],launch['tx_order'],launch['ix'])
                 and e['slot']<launch['slot']+cfg['observation_slots']]
        t=time.perf_counter();funders,truncated=funders_before(s,launch,[e['user'] for e in initial if e['is_buy']])
        funding_seconds+=time.perf_counter()-t;funding_queries['truncated' if truncated else 'bounded_complete_query']+=1
        sources[launch['split']+':'+launch['source']]+=1
        for strategy in cfg['strategies']:
            for delay in cfg['entry_delay_slots']:
                result=evaluate(events,launch,source_watermarks.get(launch['source'],launch['slot']),cfg,strategy,delay,funders)
                result.update(mint=launch['mint'],split=launch['split'],source=launch['source'],funding_query_truncated=truncated)
                if backlog:result=dict(mint=launch['mint'],split=launch['split'],source=launch['source'],status='pending_index',unindexed_objects=backlog,validated_pnl_lamports=None,live_trade=False)
                elif result.get('features',{}).get('events')==0 and any(launch['slot']<=e['slot']<launch['slot']+cfg['observation_slots'] for e in events):
                    result.update(status='observation_unusable',reason='no_supported_sol_features')
                elif result.get('features',{}).get('events')==0:
                    activity=s.db.execute('''SELECT count(*) FROM activity a JOIN observations o ON o.id=a.observation
                        WHERE a.mint=? AND o.source=? AND a.slot>=? AND a.slot<? AND o.finality!='dead_branch' ''',
                        (launch['mint'],launch['source'],launch['slot'],launch['slot']+cfg['observation_slots'])).fetchone()[0]
                    if activity:result.update(status='observation_unusable',reason='trade_instructions_without_valid_events')
                if conflict:result.update(status='conflicting_event_evidence',quote_return_lamports=None)
                if strategy=='demand_filter' and delay==2 and result['status']=='observation_unusable':
                    detail=sorted({valuation_reason(e) or 'supported_sol_curve' for e in initial})
                    if not detail:detail=['instructions_without_decodable_events']
                    unavailable['+'.join(detail)]+=1
                    result['observation_details']=detail
                group=f"{launch['split']}:{strategy}:{delay}"
                totals[group+':'+result['status']]+=1
                if result.get('reason'):reasons[result['reason']]+=1
                if result.get('quote_return_lamports') is not None:samples.setdefault(group,[]).append(result['quote_return_lamports'])
                with s.db:s.db.execute('INSERT OR REPLACE INTO shadow_results VALUES(?,?,?,?,?,?)',(p['id'],launch['cohort'],strategy,delay,canonical(result),time.time()))
                if strategy=='demand_filter' and delay==2 and len(examples)<12:examples.append(result)
    pending=s.db.execute('''SELECT count(DISTINCT e.id) FROM evidence e JOIN observations o ON o.evidence=e.id
        LEFT JOIN event_objects_v4 d ON d.evidence=e.id WHERE d.evidence IS NULL AND
        (EXISTS(SELECT 1 FROM activity a WHERE a.observation=o.id) OR EXISTS(SELECT 1 FROM launches l WHERE l.observation=o.id))''').fetchone()[0]
    parse_errors=Counter()
    for r in s.db.execute('SELECT errors FROM event_objects_v4'):parse_errors.update(json.loads(r[0]))
    report=dict(at=time.time(),state='SHADOW_RESEARCH',
                protocol_id=p['id'],registered_at=p['registered_at'],registered_slot=p['registered_slot'],
                cohorts=dict(sources),indexing=indexed,unindexed_evidence_objects=pending,
                event_valuation=dict(layouts),outcomes=dict(totals),reasons=dict(reasons),examples=examples,
                event_parse_errors=dict(parse_errors),event_observations_by_finality=dict(provisional),
                observation_unusable_by_launch=dict(unavailable),funding_lookup=dict(funding_queries,seconds=funding_seconds),
                event_evidence_paths=dict(evidence_paths),
                indicative_quote_sums={k:dict(n=len(v),sum_lamports=sum(v),profitable=sum(x>0 for x in v)) for k,v in samples.items()},
                upstream=(s.state('ticking') or {}).get('upstream'),
                promotion='BLOCKED',validated_pnl=None,live_trades=0,
                blockers=['processed feed is not independently finalized','source filters and unrepaired gaps prevent complete market replay',
                          'indicative reserve quotes omit endogenous counterfactual market reactions',
                          'prospective sample and observation period not yet sufficient',
                          'migration, mayhem and alternate quote/fee modes excluded explicitly'],
                no_exit_treatment='censored, includes worst-case principal loss; never dropped as successful zero-PnL',
                model_can_change_frozen_rules=False)
    atomic(s.root/'strategy-status.json',report)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    p.add_argument('--ticking-root',default='/srv/economic-machine/collector');p.add_argument('--maximum',type=int,default=1000)
    a=p.parse_args()
    if not 1<=a.maximum<=10000:raise ValueError('OBJECT_BATCH_BOUND')
    print(canonical(run(a.root,a.ticking_root,a.maximum)))
