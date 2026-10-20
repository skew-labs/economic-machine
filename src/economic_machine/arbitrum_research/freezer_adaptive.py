"""Byte-bounded native input using pinned freezer index sizes before body fetch."""
import base64,hashlib,json,os,struct,time
from pathlib import Path
from .freezer_source import bounds,metadata,TABLES,DEADLINE
from .freezer_source_pipeline import PipelinedSnapshot
from .freezer_prepared import read_prepared
from .storage import Storage
from economic_machine.pump_research.store import atomic,canonical
MAX_ENVELOPE=60*1024**2
MAX_PAGE=16384
FIELDS=(("header","headers"),("hash","hashes"),("body","bodies"),("receipts","receipts"))

def record_sizes(snapshot,name,start,count):
 if name not in TABLES or type(start) is not int or start<0 or type(count) is not int or not 1<=count<=MAX_PAGE:
  raise ValueError("ADAPTIVE_RECORD_SCOPE")
 ext=TABLES[name];index=name+'.'+ext+'idx';entry=snapshot.files.get(snapshot.prefix+'/'+index)
 if not entry:raise ValueError("FREEZER_MEMBER_PENDING:"+index)
 _,removed=struct.unpack('>HI',snapshot.member(index,0,6))
 m=snapshot.files.get(snapshot.prefix+'/'+name+'.meta')
 if not m or m['bytes']>64:raise ValueError("FREEZER_META_MISSING")
 meta=metadata(snapshot.member(name+'.meta',0,m['bytes']))
 stop=(start-removed+count+1)*6
 if start<max(removed,meta['tail']) or entry['bytes']%6 or stop>entry['bytes'] or meta['flush'] is not None and stop>meta['flush']:
  raise ValueError("FREEZER_HISTORY_OUTSIDE")
 positions=bounds(snapshot.member(index,(start-removed)*6,(count+1)*6),first_item=start==removed)
 return [v[2] for v in positions]

def count_for_sizes(chain,start,sizes,limit=MAX_ENVELOPE):
 if chain!=42161 or type(start) is not int or start<0 or type(limit) is not int or not 0<limit<=MAX_ENVELOPE:
  raise ValueError("ADAPTIVE_ENVELOPE_SCOPE")
 if set(sizes)!={v for _,v in FIELDS}:raise ValueError("ADAPTIVE_TABLES")
 lengths={len(v) for v in sizes.values()}
 if len(lengths)!=1 or not 1<=next(iter(lengths))<=MAX_PAGE:raise ValueError("ADAPTIVE_COUNTS")
 total=len((canonical(dict(chain=chain,blocks=[]))+'\n').encode())
 selected=0
 for i in range(next(iter(lengths))):
  record=dict(number=start+i,**{k:"" for k,_ in FIELDS})
  extra=len(canonical(record).encode())+(1 if selected else 0)
  for _,name in FIELDS:
   n=sizes[name][i]
   if type(n) is not int or not 0<=n<=16*1024**2:raise ValueError("ADAPTIVE_RECORD_BOUND")
   extra+=4*((n+2)//3)
  if total+extra>limit:break
  total+=extra;selected+=1
 if not selected:raise ValueError("ADAPTIVE_SINGLE_BLOCK_TOO_LARGE")
 return selected,total

def produce(config,q,slots,stop):
 # Import the original bounded queue helpers, preserving their stop semantics.
 from freezer_pipeline import publish,acquire_slot,acquire_tables
 storage=None;snapshot=None;out=Path(config['out']);policy=config['policy'];cursor=config['next_block']
 try:
  if policy.get('page_mode')!='byte_bounded_v1' or policy.get('max_native_input_bytes')!=MAX_ENVELOPE:
   raise ValueError("ADAPTIVE_POLICY")
  storage=Storage(out/'acquisition',Path('/srv/economic-machine/solana'),chain_id=policy['chain'])
  snapshot=PipelinedSnapshot(Path(config['base'])/'evidence/bulk-snapshot-v1',policy['source_cache'],storage.cloud,
    read_ahead=True,source_authorization=policy.get('source_cap_authorization'))
  original_guard=snapshot.guard
  def guard(n=0):
   if stop.is_set():raise ValueError("PIPELINE_STOP_REQUESTED")
   original_guard(n)
  snapshot.guard=guard
  snapshot.db.execute('create table if not exists prepared_pages(id TEXT PRIMARY KEY,body TEXT)');snapshot.db.commit()
  while cursor<=policy['end']:
   if not acquire_slot(slots,stop):return
   n=cursor;job=hashlib.sha256((config['policy_sha256']+':'+str(n)).encode()).hexdigest()
   committed=snapshot.db.execute('select body from prepared_pages where id=?',(job,)).fetchone()
   begin=time.monotonic();before_calls=snapshot.calls;before_bytes=snapshot.network_bytes;snapshot.last_refs=[]
   if committed:
    ready=json.loads(committed[0]);count=ready['count']
    if type(count) is not int or not 1<=count<=min(MAX_PAGE,policy['end']-n+1):raise ValueError("ADAPTIVE_RESUME_COUNT")
   else:
    maximum=min(MAX_PAGE,policy['end']-n+1)
    sizes={name:record_sizes(snapshot,name,n,maximum) for _,name in FIELDS}
    count,predicted=count_for_sizes(policy['chain'],n,sizes)
   folder=out/f'window-{n}-{n+count-1}';folder.mkdir(exist_ok=True)
   if committed and not (folder/'acquired.json').exists():atomic(folder/'acquired.json',ready)
   if (folder/'acquired.json').exists():
    _,ready=read_prepared(folder,policy['source_cache'],storage.cloud,chain=policy['chain'],start=n,count=count,
      binary_sha256=policy['binary_sha256'],source_authorization=policy.get('source_cap_authorization'))
    if ready['job_id']!=job:raise ValueError("PIPELINE_HANDOFF_JOB_CHANGED")
   else:
    tables=acquire_tables(snapshot,n,count)
    request=dict(chain=policy['chain'],blocks=[dict(number=n+i,**{k:base64.b64encode(tables[v][i]).decode() for k,v in FIELDS}) for i in range(count)])
    raw=(canonical(request)+'\n').encode()
    if len(raw)!=predicted or not 0<len(raw)<=MAX_ENVELOPE:raise ValueError("ADAPTIVE_SIZE_MISMATCH")
    storage.cloud.guard_local(len(raw)*2);path=folder/'candidate-records.json'
    if path.exists():
     if path.is_symlink() or path.read_bytes()!=raw:raise ValueError("PIPELINE_PARTIAL_CHANGED")
    else:
     with path.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    ready=dict(job_id=job,chain=policy['chain'],start=n,count=count,folder=str(folder.resolve()),binary_sha256=policy['binary_sha256'],deadline_ms=DEADLINE,
      source_identity=snapshot.identity,source_ranges=snapshot.last_refs,raw_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest(),
      source_seconds=time.monotonic()-begin,network_requests=snapshot.calls-before_calls,network_bytes=snapshot.network_bytes-before_bytes,
      acquired_at_ms=time.time_ns()//1000000,adaptive_selection=dict(maximum_blocks=maximum,selected_blocks=count,predicted_bytes=predicted,limit_bytes=MAX_ENVELOPE))
    if policy.get('source_cap_authorization'):ready['source_cap_authorization']=policy['source_cap_authorization']
    with snapshot.db:snapshot.db.execute('insert into prepared_pages values(?,?)',(job,canonical(ready)))
    atomic(folder/'acquired.json',ready)
    del tables,request,raw
   atomic(out/'acquisition-status.json',dict(state='ACQUIRED',start=n,count=count,at_ms=time.time_ns()//1000000,search_registered=False,page_mode='byte_bounded_v1'))
   if not publish(q,dict(start=n,count=count,folder=str(folder)),stop):return
   cursor=n+count
  publish(q,dict(done=True),stop)
 except Exception as e:
  atomic(out/'acquisition-error.json',dict(at_ms=time.time_ns()//1000000,error_type=type(e).__name__,error=str(e)[:200]))
  publish(q,dict(error_type=type(e).__name__),stop)
 finally:
  if snapshot:snapshot.close()
  if storage:storage.close()
