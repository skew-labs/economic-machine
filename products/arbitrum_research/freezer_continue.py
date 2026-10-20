"""Finite extraction continuation with original source ledger and sealed search."""
import argparse,fcntl,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from economic_machine.arbitrum_research.freezer_source import DEADLINE
from economic_machine.arbitrum_research.freezer_search import register
from economic_machine.pump_research.store import atomic

def main():
 p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--out',required=True);p.add_argument('--chain',type=int,required=True);p.add_argument('--start',type=int,required=True);p.add_argument('--end',type=int,required=True);p.add_argument('--binary',required=True);p.add_argument('--source-cache',required=True);a=p.parse_args()
 if a.chain not in (42161,4663) or a.start<0 or a.end<a.start or a.end-a.start>=131072:raise ValueError('CONTINUATION_SCOPE')
 out=Path(a.out);out.mkdir(parents=True,exist_ok=True);lock=(out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 policy=dict(chain=a.chain,start=a.start,end=a.end,deadline_ms=DEADLINE,page_blocks=16384,source_cache=str(Path(a.source_cache).resolve()),source_cap_bytes=1024**3,
  binary_sha256=hashlib.sha256(Path(a.binary).read_bytes()).hexdigest(),original_source_journal_preserved=True)
 if (out/'policy.json').exists():
  if json.loads((out/'policy.json').read_bytes())!=policy:raise ValueError('CONTINUATION_POLICY_CHANGED')
 else:atomic(out/'policy.json',policy)
 status=dict(state='RUNNING',at_ms=time.time_ns()//1000000,next_block=a.start,indexed_blocks=0,windows=[])
 if (out/'status.json').exists():status=json.loads((out/'status.json').read_bytes())
 while status['next_block']<=a.end:
  if time.time_ns()//1000000>=DEADLINE:status['state']='DEADLINE';break
  n=status['next_block'];count=min(16384,a.end-n+1);folder=out/f'window-{n}-{n+count-1}'
  try:
   if not (folder/'verification.json').exists():
    command=[sys.executable,str(Path(__file__).with_name('freezer_probe.py')),'--base',a.base,'--out',str(folder),'--chain',str(a.chain),'--start',str(n),'--count',str(count),'--binary',a.binary,'--source-cache',a.source_cache,'--read-ahead']
    run=subprocess.run(command,capture_output=True,timeout=900)
    atomic(out/f'attempt-{n}-{time.time_ns()//1000000}.json',dict(returncode=run.returncode,stderr_tail=run.stderr.decode(errors='replace')[-1500:]))
    if run.returncode:raise ValueError('CONTINUATION_WINDOW_FAILED')
   # Reconciliation of an interrupted registration never repeats source work.
   registration=register(a.base,folder)
   proof=json.loads((folder/'verification.json').read_bytes())
   if registration['first']!=n or registration['blocks']!=count:raise ValueError('CONTINUATION_WINDOW_IDENTITY')
   status['windows'].append(dict(start=n,end=n+count-1,blocks=count,elapsed_seconds=proof['elapsed_seconds'],source_seconds=proof['source_seconds'],
       network_bytes=proof['network_bytes'],network_requests=proof['network_requests'],index_sha256=registration['index_sha256']))
   status.update(state='RUNNING',next_block=n+count,indexed_blocks=status['indexed_blocks']+count,at_ms=time.time_ns()//1000000)
   atomic(out/'status.json',status)
  except Exception as e:
   status.update(state='STOPPED_REVIEW_REQUIRED',error_type=type(e).__name__,at_ms=time.time_ns()//1000000);atomic(out/'status.json',status);raise
 else:status['state']='COMPLETE_BOUNDED_WINDOW'
 status['at_ms']=time.time_ns()//1000000;status['full_mapping_complete']=False;atomic(out/'status.json',status)

if __name__=='__main__':main()
