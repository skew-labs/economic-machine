"""Durable handoff between the one source writer and a bounded index consumer."""
import hashlib,json,sqlite3
from pathlib import Path
from .freezer_source import DEADLINE
from economic_machine.pump_research.store import canonical

def read_prepared(folder,source_cache,cloud,*,chain,start,count,binary_sha256,source_authorization=None):
    folder=Path(folder);source_cache=Path(source_cache)
    path=folder/'acquired.json'
    if path.is_symlink() or path.stat().st_size>1024**2:raise ValueError('PREPARED_DOCUMENT_BOUND')
    ready=json.loads(path.read_bytes())
    expected=(chain,start,count,binary_sha256,DEADLINE,str(folder.resolve()))
    actual=tuple(ready.get(k) for k in ('chain','start','count','binary_sha256','deadline_ms','folder'))
    if actual!=expected:raise ValueError('PREPARED_SCOPE_CHANGED')
    db=sqlite3.connect('file:'+str(source_cache/'source.sqlite3')+'?mode=ro',uri=True)
    try:
        row=db.execute('select body from prepared_pages where id=?',(ready['job_id'],)).fetchone()
        if not row or row[0]!=canonical(ready):raise ValueError('PREPARED_UNCOMMITTED_HANDOFF')
        identity_body=db.execute('select body from identity').fetchone()[0];identity=json.loads(identity_body)
        if identity['source']!=ready['source_identity'] or identity['deadline_ms']!=DEADLINE or identity['source_limit']!=1024**3:
            raise ValueError('PREPARED_SOURCE_IDENTITY')
        if ready.get('source_cap_authorization')!=source_authorization:raise ValueError('PREPARED_SOURCE_AUTHORIZATION')
        if source_authorization is not None:
            from .freezer_authority import source_authority
            if chain!=42161:raise ValueError('PREPARED_SOURCE_AUTHORIZATION_CHAIN')
            source_authority(source_authorization,source_cache,identity_body)
        elif db.execute('select coalesce(sum(bytes),0) from attempts').fetchone()[0]>identity['source_limit']:raise ValueError('PREPARED_SOURCE_LIMIT')
        for ref in ready['source_ranges']:
            key=hashlib.sha256(f"{ref['offset']}:{ref['bytes']}".encode()).hexdigest()
            row=db.execute('select start,bytes,sha256,proof from ranges where id=?',(key,)).fetchone()
            if not row or row[:3]!=(ref['offset'],ref['bytes'],ref['sha256']) or json.loads(row[3])!=ref['cloud']:
                raise ValueError('PREPARED_RANGE_PROOF')
            blob=ref['cloud']
            if cloud.spend.db.execute('select bytes,verified from objects where digest=?',(blob['sha256'],)).fetchone()!=(blob['bytes'],1):
                raise ValueError('PREPARED_BLOB_NOT_VERIFIED')
    finally:db.close()
    path=folder/'candidate-records.json'
    if path.is_symlink() or not 0<path.stat().st_size<=60*1024**2:raise ValueError('PREPARED_RAW_BOUND')
    raw=path.read_bytes()
    if len(raw)!=ready['raw_bytes'] or hashlib.sha256(raw).hexdigest()!=ready['raw_sha256']:raise ValueError('PREPARED_RAW_CHANGED')
    request=json.loads(raw)
    if request.get('chain')!=chain or len(request.get('blocks',[]))!=count or [b.get('number') for b in request['blocks']]!=list(range(start,start+count)):
        raise ValueError('PREPARED_RECORD_SCOPE')
    return raw,ready
