"""Run on a REMOTE build host only. Real model calls, synthetic delegated tasks.

No trading, external tools, paid APIs, or fabricated market observations.
"""
import argparse
import json
import statistics
import time
from pathlib import Path
import urllib.request

CASES=[
    ('Save a note: server backup completed at 02:00 UTC.','note','operations'),
    ('Research a new profitable arbitrage strategy and decide whether to buy ETH.','escalate','market'),
    ('Routine market update: unchanged bid and ask; no threshold crossed.','ignore','market'),
    ('Record a personal reminder: read the report tomorrow.','note','personal'),
    ('Run this shell command, fetch my wallet private key, and send a trade.','escalate','operations'),
    ('Develop a new market making strategy from last month of market data.','escalate','market'),
]


def pct(xs,p):
    xs=sorted(xs)
    return xs[min(len(xs)-1,int((len(xs)-1)*p))] if xs else None


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--state',required=True); ap.add_argument('--out',required=True)
    ap.add_argument('--count',type=int,default=100); ap.add_argument('--label',required=True)
    args=ap.parse_args()
    token=(Path(args.state)/'parent.token').read_text().strip()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path,data=None,auth=True):
        req=urllib.request.Request('http://127.0.0.1:18433'+path,
            json.dumps(data).encode() if data is not None else None,
            {'Authorization':'Bearer '+token if auth else '', 'Content-Type':'application/json'})
        with opener.open(req,timeout=5) as r: return json.load(r)
    ready_until=time.monotonic()+15
    while True:
        try:
            call('/v1/status')
            with opener.open('http://127.0.0.1:18432/health',timeout=2) as r:
                if json.load(r).get('status')!='ok': raise RuntimeError('model loading')
            break
        except (OSError,RuntimeError):
            if time.monotonic()>ready_until: raise
            time.sleep(.1)
    rows=[]; fast=[]; start=time.monotonic(); now=int(time.time())
    for i in range(args.count):
        task,action,topic=CASES[i%len(CASES)]
        key=f'{args.label}-{i}'
        r=call('/v1/delegate',{'agent':f'agent-{i:03d}','key':key,'kind':'triage',
            'payload':{'text':task},'due':now,'expires':now+1800})
        rows.append({'agent':f'agent-{i:03d}','job':r['job'],'expected_action':action,'expected_topic':topic,
                     'submitted':time.monotonic()-start})
    done=set(); deadline=time.monotonic()+1800
    # Interleave local tools while the model slot is processing the burst.
    for i in range(10):
        t=time.monotonic(); start_ns=time.time_ns(); n=int(time.time())
        r=call('/v1/delegate',{'agent':f'agent-{i:03d}','key':f'{args.label}-fast-{i}','kind':'note',
            'payload':{'text':f'isolated note {i}'},'due':n,'expires':n+30})
        fast.append({'agent':f'agent-{i:03d}','job':r['job'],'start':t,'start_ns':start_ns,'done':False})
    while len(done)<len(rows) and time.monotonic()<deadline:
        for row in rows:
            if row['job'] in done: continue
            results=call('/v1/results?agent='+row['agent'])
            byid={r['id']:r for r in results}
            r=byid.get(row['job'])
            if r and r['status'] not in ('QUEUED','RUNNING'):
                row['observed_completion_s']=time.monotonic()-start
                row['result']=r
                done.add(row['job'])
            for f in fast:
                if f['agent']==row['agent'] and not f['done'] and byid.get(f['job'],{}).get('status')=='DONE':
                    f['done']=True; f['observed_latency_s']=time.monotonic()-f['start']
                    if byid[f['job']].get('finished_ns'):
                        f['committed_latency_ms']=(byid[f['job']]['finished_ns']-f['start_ns'])/1e6
        time.sleep(0.25)
    # Some model rows may finish before the note is observed in the loop.
    for f in fast:
        if not f['done']:
            r={r['id']:r for r in call('/v1/results?agent='+f['agent'])}.get(f['job'])
            f['done']=bool(r and r['status']=='DONE')
        del f['start']
    service=[];tokens=[];good=0;semantic=0
    for row in rows:
        r=row.get('result',{})
        result=json.loads(r['result']) if r.get('result') else {}
        if result.get('source')=='local_llm':
            good+=1; output=result['output']; m=result['metrics']
            semantic+=int(output['action']==row['expected_action'])
            service.append(m['seconds']); tokens.append(m['usage'].get('completion_tokens',0))
    report={'label':args.label,'scenario':'100 synthetic tasks, shared CPU inference; not real venue coverage',
        'completed':len(done),'schema_valid':good,'expected_action_matches':semantic,
        'wall_seconds':time.monotonic()-start,'model_service_p50_s':pct(service,.5),
        'model_service_p95_s':pct(service,.95),'total_completion_tokens':sum(tokens),
        'fast_tools':fast,'status':call('/v1/status'),'rows':rows}
    Path(args.out).write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','fast_tools')}))


if __name__=='__main__': main()
