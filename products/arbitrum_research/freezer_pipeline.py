"""One source writer, one verified search consumer, one window of lookahead.

The run clock starts before either stage. Source bytes and failed reservations
stay in the original cumulative ledger. Completed windows are never re-fetched.
"""
import argparse,base64,fcntl,hashlib,json,multiprocessing,os,queue,sqlite3,subprocess,sys,time
from pathlib import Path
from economic_machine.arbitrum_research.freezer_source import Snapshot,DEADLINE
from economic_machine.arbitrum_research.freezer_prepared import read_prepared
from economic_machine.arbitrum_research.freezer_search import register
from economic_machine.arbitrum_research.storage import Storage
from economic_machine.pump_research.store import atomic,canonical

def pages(start,end):
 if type(start) is not int or type(end) is not int or start<0 or end<start or end-start>=262144:raise ValueError('PIPELINE_SCOPE')
 return [(n,min(16384,end-n+1)) for n in range(start,end+1,16384)]

def publish(q,item,stop):
 while not stop.is_set():
  try:q.put(item,timeout=1);return True
  except queue.Full:pass
 return False

def acquire_slot(slots,stop):
 while not stop.is_set():
  if slots.acquire(timeout=1):return True
 return False

def acquire_tables(snapshot,n,count):
 tables={name:snapshot.table(name,n,count) for name in ('headers','hashes','bodies','receipts')}
 # No prepared handoff may be constructed until every source range has its
 # durable, fully read-back Blob proof, including asynchronous implementations.
 if hasattr(snapshot,'drain'):snapshot.drain()
 return tables

def produce(config,q,slots,stop):
 storage=None;snapshot=None;out=Path(config['out']);policy=config['policy']
 try:
  storage=Storage(out/'acquisition',Path('/srv/economic-machine/solana'),chain_id=policy['chain'])
  snapshot=Snapshot(Path(config['base'])/'evidence/bulk-snapshot-v1',Path(policy['source_cache']),storage.cloud,read_ahead=True)
  original_guard=snapshot.guard
  def guard(n=0):
   if stop.is_set():raise ValueError('PIPELINE_STOP_REQUESTED')
   original_guard(n)
  snapshot.guard=guard
  snapshot.db.execute('create table if not exists prepared_pages(id TEXT PRIMARY KEY,body TEXT)');snapshot.db.commit()
  for n,count in pages(policy['start'],policy['end']):
   if n<config['next_block']:continue
   if not acquire_slot(slots,stop):return
   folder=out/f'window-{n}-{n+count-1}';folder.mkdir(exist_ok=True)
   job=hashlib.sha256((config['policy_sha256']+':'+str(n)).encode()).hexdigest()
   committed=snapshot.db.execute('select body from prepared_pages where id=?',(job,)).fetchone()
   if committed and not (folder/'acquired.json').exists():atomic(folder/'acquired.json',json.loads(committed[0]))
   if (folder/'acquired.json').exists():
    _,ready=read_prepared(folder,policy['source_cache'],storage.cloud,chain=policy['chain'],start=n,count=count,binary_sha256=policy['binary_sha256'],source_authorization=policy.get('source_cap_authorization'))
    if ready['job_id']!=job:raise ValueError('PIPELINE_HANDOFF_JOB_CHANGED')
   else:
    begin=time.monotonic();snapshot.last_refs=[];before_calls=snapshot.calls;before_bytes=snapshot.network_bytes
    tables=acquire_tables(snapshot,n,count)
    request=dict(chain=policy['chain'],blocks=[dict(number=n+i,**{k:base64.b64encode(tables[v][i]).decode() for k,v in [('header','headers'),('hash','hashes'),('body','bodies'),('receipts','receipts')]}) for i in range(count)])
    raw=(canonical(request)+'\n').encode()
    if not 0<len(raw)<=60*1024**2:raise ValueError('PIPELINE_ENVELOPE_BOUND')
    storage.cloud.guard_local(len(raw)*2)
    path=folder/'candidate-records.json'
    if path.exists():
     if path.is_symlink() or path.read_bytes()!=raw:raise ValueError('PIPELINE_PARTIAL_CHANGED')
    else:
     with path.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    # Every range has already passed the full Blob readback in Snapshot.read.
    ready=dict(job_id=job,chain=policy['chain'],start=n,count=count,folder=str(folder.resolve()),binary_sha256=policy['binary_sha256'],deadline_ms=DEADLINE,
      source_identity=snapshot.identity,source_ranges=snapshot.last_refs,raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest(),
      source_seconds=time.monotonic()-begin,network_requests=snapshot.calls-before_calls,network_bytes=snapshot.network_bytes-before_bytes,acquired_at_ms=time.time_ns()//1000000)
    if policy.get('source_cap_authorization'):ready['source_cap_authorization']=policy['source_cap_authorization']
    with snapshot.db:snapshot.db.execute('insert into prepared_pages values(?,?)',(job,canonical(ready)))
    atomic(folder/'acquired.json',ready)
    del tables,request,raw
   atomic(out/'acquisition-status.json',dict(state='ACQUIRED',start=n,count=count,at_ms=time.time_ns()//1000000,search_registered=False))
   if not publish(q,dict(start=n,count=count,folder=str(folder)),stop):return
  publish(q,dict(done=True),stop)
 except Exception as e:
  atomic(out/'acquisition-error.json',dict(at_ms=time.time_ns()//1000000,error_type=type(e).__name__,error=str(e)[:200]))
  publish(q,dict(error_type=type(e).__name__),stop)
 finally:
  if snapshot:snapshot.close()
  if storage:storage.close()

def main():
 p=argparse.ArgumentParser()
 for key in ('base','out','binary','source-cache'):p.add_argument('--'+key,required=True)
 for key in ('chain','start','end'):p.add_argument('--'+key,type=int,required=True)
 a=p.parse_args();pages(a.start,a.end)
 if a.chain not in (42161,4663) or time.time_ns()//1000000>=DEADLINE:raise ValueError('PIPELINE_CHAIN_OR_DEADLINE')
 out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
 lock=(out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 policy=dict(version=1,chain=a.chain,start=a.start,end=a.end,page_blocks=16384,deadline_ms=DEADLINE,source_cache=str(Path(a.source_cache).resolve()),source_cap_bytes=1024**3,
  binary_sha256=hashlib.sha256(Path(a.binary).read_bytes()).hexdigest(),max_inflight_windows=2,source_writers=1,search_consumers=1)
 if (out/'policy.json').exists():
  if json.loads((out/'policy.json').read_bytes())!=policy:raise ValueError('PIPELINE_POLICY_CHANGED')
 else:atomic(out/'policy.json',policy)
 sha=hashlib.sha256(canonical(policy).encode()).hexdigest();begin=time.monotonic();started=time.time_ns()//1000000
 status=dict(state='RUNNING',started_at_ms=started,next_block=a.start,indexed_blocks=0,windows=[],resumed=False,full_mapping_complete=False)
 if (out/'status.json').exists():
  status=json.loads((out/'status.json').read_bytes())
  if status['state']=='COMPLETE_BOUNDED_WINDOW':raise ValueError('PIPELINE_ALREADY_COMPLETE')
  status['resumed']=True
 # Resume may reconcile a fully written page; it is never a fresh-source benchmark.
 atomic(out/'status.json',status)
 ctx=multiprocessing.get_context('spawn');q=ctx.Queue(maxsize=1);slots=ctx.BoundedSemaphore(2);stop=ctx.Event()
 config=dict(base=a.base,out=str(out),policy=policy,policy_sha256=sha,next_block=status['next_block'])
 producer=ctx.Process(target=produce,args=(config,q,slots,stop));producer.start()
 try:
  while status['next_block']<=a.end:
   if time.time_ns()//1000000>=DEADLINE:raise ValueError('PIPELINE_DEADLINE')
   try:item=q.get(timeout=1)
   except queue.Empty:
    if not producer.is_alive():raise ValueError('PIPELINE_PRODUCER_EXITED')
    continue
   if 'error_type' in item or item.get('done'):raise ValueError('PIPELINE_INCOMPLETE_SOURCE')
   n=status['next_block'];count=min(16384,a.end-n+1);folder=out/f'window-{n}-{n+count-1}'
   if item!=dict(start=n,count=count,folder=str(folder)):raise ValueError('PIPELINE_OUT_OF_ORDER')
   consumed=time.monotonic()
   if not (folder/'verification.json').exists():
    command=[sys.executable,str(Path(__file__).with_name('freezer_probe.py')),'--base',a.base,'--out',str(folder),'--chain',str(a.chain),'--start',str(n),'--count',str(count),'--binary',a.binary,'--source-cache',a.source_cache,'--read-ahead','--prepared']
    run=subprocess.run(command,capture_output=True,timeout=900)
    atomic(folder/f'consumer-attempt-{time.time_ns()//1000000}.json',dict(returncode=run.returncode,stderr_tail=run.stderr.decode(errors='replace')[-1200:]))
    if run.returncode:raise ValueError('PIPELINE_CONSUMER_FAILED')
   registration=register(a.base,folder);proof=json.loads((folder/'verification.json').read_bytes())
   if registration['first']!=n or registration['blocks']!=count:raise ValueError('PIPELINE_REGISTRATION_SCOPE')
   status['windows'].append(dict(start=n,end=n+count-1,blocks=count,consumer_seconds=time.monotonic()-consumed,source_seconds=proof['source_seconds'],network_bytes=proof['network_bytes'],network_requests=proof['network_requests'],index_sha256=registration['index_sha256'],registered_at_ms=time.time_ns()//1000000))
   status.update(next_block=n+count,indexed_blocks=status['indexed_blocks']+count,at_ms=time.time_ns()//1000000,elapsed_seconds=time.monotonic()-begin)
   status['blocks_per_second']=status['indexed_blocks']/((status['at_ms']-status['started_at_ms'])/1000 if status['resumed'] else status['elapsed_seconds'])
   atomic(out/'status.json',status);slots.release()
  producer.join(timeout=10)
  if producer.is_alive() or producer.exitcode!=0:raise ValueError('PIPELINE_SOURCE_DRAIN_FAILED')
  status.update(state='COMPLETE_BOUNDED_WINDOW',at_ms=time.time_ns()//1000000,elapsed_seconds=time.monotonic()-begin)
  status['blocks_per_second']=status['indexed_blocks']/((status['at_ms']-status['started_at_ms'])/1000 if status['resumed'] else status['elapsed_seconds'])
  status['measurement_scope']='source_acquisition_through_verified_blob_graph_registered_search_including_startup_and_drain'
  atomic(out/'status.json',status)
 except Exception as e:
  status.update(state='STOPPED_REVIEW_REQUIRED',at_ms=time.time_ns()//1000000,error_type=type(e).__name__,error=str(e)[:200]);atomic(out/'status.json',status);raise
 finally:
  stop.set();producer.join(timeout=30)
  if producer.is_alive():producer.terminate();producer.join(timeout=10)
  q.close()

if __name__=='__main__':main()
