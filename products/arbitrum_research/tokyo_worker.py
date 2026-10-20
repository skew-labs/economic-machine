"""Forced-command public-data worker. No model, Blob or financial credentials."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from economic_machine.arbitrum_research.source import PublicSource,SourceBackoff,parse_page
from economic_machine.arbitrum_research.graph import Graph

BASE=Path('/srv/economic-machine/history-worker/data')
MIB=1024**2


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def request(value):
    if (not isinstance(value,dict) or set(value)!= {'id','start','end'}
        or not isinstance(value['id'],str) or not re.fullmatch('[0-9a-f]{32}',value['id'])
        or type(value['start']) is not int or type(value['end']) is not int
        or not 22207818<=value['start']<=value['end']<2**53
        or value['end']-value['start']>=10000):
        raise ValueError('WORKER_REQUEST')
    return value


def pack(path):
    target=path.with_suffix(path.suffix+'.gz')
    if target.exists():
        with gzip.open(target,'rb') as f:
            h=hashlib.file_digest(f,'sha256').hexdigest()
        if h!=digest(path):raise ValueError('WORKER_COMPRESSED_CHANGED')
        return dict(name=target.name,bytes=target.stat().st_size,sha256=digest(target),
            raw_bytes=path.stat().st_size,raw_sha256=digest(path))
    temporary=target.with_suffix(target.suffix+'.next')
    if temporary.exists():
        temporary.rename(temporary.with_suffix('.partial-'+str(time.time_ns())))
    with path.open('rb') as source,temporary.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw,mode='wb',compresslevel=3,mtime=0) as output:
            shutil.copyfileobj(source,output,MIB)
        raw.flush();os.fsync(raw.fileno())
    os.replace(temporary,target)
    return dict(name=target.name,bytes=target.stat().st_size,sha256=digest(target),
        raw_bytes=path.stat().st_size,raw_sha256=digest(path))


def prepare(job,base=BASE,source=None,on_source=None):
    started=time.monotonic()
    job=request(job);base.mkdir(exist_ok=True)
    folder=base/job['id'];folder.mkdir(exist_ok=True)
    receipt=folder/'manifest.json'
    if receipt.exists():
        result=json.loads(receipt.read_text())
        if result['job']!=job:raise ValueError('WORKER_JOB_CHANGED')
        for p in result['parts']:
            path=folder/p['name']
            if path.is_symlink() or path.stat().st_size!=p['bytes'] or digest(path)!=p['sha256']:
                raise ValueError('WORKER_RETAINED_CHANGED')
        if on_source:on_source(dict(event='SOURCE_RETAINED',job_id=job['id'],
            source_sha256=result['source_sha256'],first_block=result['first_block'],last_block=result['last_block']))
        return result
    retained=sum(p.stat().st_size for p in base.glob('*/*') if p.is_file())
    if retained>2*1024**3 or shutil.disk_usage(base).free<8*1024**3:
        raise ValueError('WORKER_CACHE_PRESSURE')
    raw=folder/'source.ndjson'
    if raw.exists():page=parse_page(raw.read_bytes(),job['start'],job['end'])
    else:
        page=(source or PublicSource(max_response=16*MIB)).fetch(job['start'],job['end'])
        temporary=raw.with_suffix('.next')
        if temporary.exists():temporary.rename(temporary.with_suffix('.partial-'+str(time.time_ns())))
        with temporary.open('xb') as out:out.write(page['raw']);out.flush();os.fsync(out.fileno())
        os.replace(temporary,raw)
    fetched=time.monotonic()
    if on_source:on_source(dict(event='SOURCE_RETAINED',job_id=job['id'],source_sha256=page['sha256'],
        first_block=page['first_block'],last_block=page['last_block']))
    index=folder/'index.sqlite3'
    graph=Graph(index,initial_start=job['start'],previous_block_hash=page['blocks'][0]['header']['parentHash'])
    try:
        graph.ingest(page['blocks'],page['sha256'],page['first_block'],page['last_block'],
            {'state':'PENDING_CENTRAL_BLOB_READBACK','job_id':job['id']})
        snapshot=graph.snapshot()
    finally:graph.close()
    indexed=time.monotonic()
    if index.stat().st_size>256*MIB:raise ValueError('WORKER_INDEX_BOUND')
    parts=[pack(raw),pack(index)]
    if sum(p['bytes'] for p in parts)>62*MIB:raise ValueError('WORKER_PACKAGE_BOUND')
    result=dict(schema='economic-machine.tokyo-public-shard.v1',job=job,
        first_block=page['first_block'],last_block=page['last_block'],source_sha256=page['sha256'],
        parts=parts,snapshot=snapshot,model_calls=0,credentials_used='dedicated_transport_only',
        timings=dict(source_seconds=fetched-started,index_seconds=indexed-fetched,
            package_seconds=time.monotonic()-indexed),source_transport=page.get('response'))
    temp=receipt.with_suffix('.next');temp.write_text(json.dumps(result,sort_keys=True)+'\n')
    os.replace(temp,receipt)
    return result


def main():
    line=sys.stdin.buffer.readline(4097)
    if not line.endswith(b'\n') or len(line)>4096:raise ValueError('WORKER_HEADER_BOUND')
    job=request(json.loads(line))
    try:result=prepare(job,on_source=lambda value:print(json.dumps(value,sort_keys=True),flush=True))
    except SourceBackoff as e:
        print(json.dumps(dict(error='SOURCE_BACKOFF',retry_after_seconds=e.retry_after_seconds,status=e.status)),flush=True)
        return
    print(json.dumps(result,sort_keys=True),flush=True)
    folder=BASE/job['id']
    for part in result['parts']:
        with (folder/part['name']).open('rb') as f:
            shutil.copyfileobj(f,sys.stdout.buffer,MIB)
    sys.stdout.buffer.flush()
    # The controller acknowledges only after source, index and catalog readback.
    ack=sys.stdin.buffer.readline(4097)
    if len(ack)>4096 or not ack.endswith(b'\n'):return
    value=json.loads(ack)
    if value!={'id':job['id'],'blob_verified':True,'source_sha256':result['source_sha256']}:return
    for name in ('source.ndjson','index.sqlite3','source.ndjson.gz','index.sqlite3.gz'):
        (folder/name).unlink()
    (folder/'ack.json').write_text(json.dumps(dict(at_ms=time.time_ns()//1000000,**value))+'\n')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':'WORKER_FAILED','reason':type(error).__name__+':'+str(error)[:160]}),flush=True)
        raise SystemExit(1)
