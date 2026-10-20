"""Private read-only wallet lookup across sealed snapshot-derived shards.

Coverage is explicit. A registered shard proves a reproducible extraction, not
complete history, current balance, common ownership, or consensus finality.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import time
from functools import lru_cache
from .freezer_cloud_index import resolve
from .graph import _address
from economic_machine.pump_research.store import canonical

def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for raw in iter(lambda:f.read(1024**2),b''):h.update(raw)
    return h.hexdigest()

def file_identity(path):
    s=Path(path).lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise ValueError('SEARCH_SEALED_FILE_TYPE')
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)

@lru_cache(maxsize=256)
def _verified_identity(path,expected,identity):
    if file_sha(path)!=expected or file_identity(path)!=identity:raise ValueError('SEARCH_SEALED_SHARD_CHANGED')
    return identity

def sealed(path,expected):
    return _verified_identity(str(path),expected,file_identity(path))

def connect(path):
    db=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
    return db

def register(base,folder):
    base=Path(base).resolve();folder=Path(folder).resolve()
    if not folder.is_relative_to(base/'evidence'):raise ValueError('SEARCH_SHARD_LOCATION')
    report=json.loads((folder/'verification.json').read_bytes());chain=report['graph']['chain_id']
    if chain not in (42161,4663) or report.get('state')!='VERIFIED_SEARCHABLE_SHARD' or report.get('source_blob_readback') is not True:
        raise ValueError('SEARCH_UNVERIFIED_SHARD')
    path=folder/'index.sqlite3'
    if Path(str(path)+'-wal').exists() and Path(str(path)+'-wal').stat().st_size:raise ValueError('SEARCH_UNSEALED_SHARD')
    sha=file_sha(path);db=connect(path)
    try:
        identity=json.loads(db.execute("select value from metadata where key='identity'").fetchone()[0])
        first,last,count=db.execute('select min(number),max(number),count(*) from blocks').fetchone()
        if identity['chain_id']!=chain or count!=report['blocks'] or last-first+1!=count:raise ValueError('SEARCH_COVERAGE_MISMATCH')
        for row in db.execute('select source_sha256,cloud_ref from pages'):
            proof=json.loads(row['cloud_ref'])
            if proof['raw_sha256']!=row['source_sha256'] or not proof['complete_cloud_readback'] or proof['chain_id']!=chain:
                raise ValueError('SEARCH_SOURCE_PROOF')
        # Include conflicting prior senders as investigation hits, never edges.
        addresses={r[0] for r in db.execute('select address from addresses')}
        discrepancies=json.loads((folder/'source-discrepancies.json').read_bytes()).get('records',[])
        addresses.update(_address(d['prior_sender']) for d in discrepancies)
    finally:db.close()
    catalog=base/'evidence/freezer-search-v1';catalog.mkdir(exist_ok=True)
    out=sqlite3.connect(catalog/'catalogue.sqlite3')
    out.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS shards(id TEXT PRIMARY KEY,chain INTEGER,path TEXT UNIQUE,first INTEGER,last INTEGER,body TEXT);
      CREATE TABLE IF NOT EXISTS wallet_shards(chain INTEGER,wallet TEXT,shard TEXT,PRIMARY KEY(chain,wallet,shard));''')
    record=dict(index_sha256=sha,report_sha256=file_sha(folder/'verification.json'),discrepancy_sha256=file_sha(folder/'source-discrepancies.json'),
        first=first,last=last,blocks=count,trace_available=False,registered_at_ms=time.time_ns()//1000000)
    try:
        old=out.execute('select id from shards where path=?',(str(path),)).fetchone()
        if old and old[0]!=sha:raise ValueError('SEARCH_REGISTERED_SHARD_CHANGED')
        # Check window boundaries and any repeated blocks before search admission.
        out.row_factory=sqlite3.Row
        neighbors=out.execute('select * from shards where chain=? and first<=? and last>=?',(chain,last+1,first-1)).fetchall()
        current=connect(path)
        try:
            for row in neighbors:
                digest,lo,hi=row['id'],row['first'],row['last']
                if digest==sha:continue
                with resolve(base,row) as neighbor_path:
                    if file_sha(neighbor_path)!=digest:raise ValueError('SEARCH_NEIGHBOR_CHANGED')
                    neighbor=connect(neighbor_path)
                    try:
                        if hi+1==first:
                            if neighbor.execute('select hash from blocks where number=?',(hi,)).fetchone()[0]!=current.execute('select parent_hash from blocks where number=?',(first,)).fetchone()[0]:raise ValueError('SEARCH_PARENT_BOUNDARY')
                        elif last+1==lo:
                            if current.execute('select hash from blocks where number=?',(last,)).fetchone()[0]!=neighbor.execute('select parent_hash from blocks where number=?',(lo,)).fetchone()[0]:raise ValueError('SEARCH_PARENT_BOUNDARY')
                        else:
                            args=(max(first,lo),min(last,hi))
                            a=[tuple(r) for r in current.execute('select number,hash from blocks where number between ? and ? order by number',args)]
                            b=[tuple(r) for r in neighbor.execute('select number,hash from blocks where number between ? and ? order by number',args)]
                            if a!=b:raise ValueError('SEARCH_OVERLAPPING_HEADERS')
                    finally:neighbor.close()
        finally:current.close()
        with out:
            out.execute('insert or ignore into shards values(?,?,?,?,?,?)',(sha,chain,str(path),first,last,canonical(record)))
            out.executemany('insert or ignore into wallet_shards values(?,?,?)',((chain,a,sha) for a in addresses))
    finally:out.close()
    return record

def _top_k_skip(edges,interactions,limit,row,record,report):
    """Prove this shard cannot affect either ordered graph result.

    Strict block inequalities preserve all id/shard tie breaking and overlapping
    observations. More than limit rows in both streams also preserves limited.
    Legacy metadata without authenticated coverage bounds uses the full query.
    """
    if type(report) is not dict:return None
    graph=report.get('graph',{})
    first,last=row['first'],row['last']
    if (type(graph) is not dict or type(first) is not int or type(last) is not int
            or first<0 or last<first or report.get('state')!='VERIFIED_SEARCHABLE_SHARD'
            or report.get('source_blob_readback') is not True or graph.get('chain_id')!=row['chain']
            or graph.get('covered_first_block')!=first or graph.get('covered_last_block')!=last
            or type(graph.get('counts')) is not dict or graph['counts'].get('blocks')!=last-first+1 or report.get('blocks')!=last-first+1
            or record.get('first')!=first or record.get('last')!=last or record.get('blocks')!=last-first+1
            or len(edges)<=limit or len(interactions)<=limit):
        return None
    edge_block=sorted((r['block'] for r in edges),reverse=True)[limit-1]
    interaction_block=sorted((r['block'] for r in interactions),reverse=True)[limit-1]
    if edge_block>last and interaction_block>last:
        return dict(edges_kth_block=edge_block,interactions_kth_block=interaction_block,shard_last_block=last)
    return None


def wallet(base,chain,address,limit=100):
    address=_address(address)
    if chain not in (42161,4663) or type(limit) is not int or not 1<=limit<=1000:raise ValueError('SEARCH_QUERY')
    catalog=connect(Path(base)/'evidence/freezer-search-v1/catalogue.sqlite3')
    try:
        # A bounded continuation is explicit rather than silently discarding shards.
        rows=catalog.execute('select s.* from shards s join wallet_shards w on w.shard=s.id where w.chain=? and w.wallet=? order by s.last desc limit 65',(chain,address)).fetchall()
        coverage=[dict(r) for r in catalog.execute('select first,last,id from shards where chain=? order by first',(chain,))]
    finally:catalog.close()
    edges=[];interactions=[];conflicts=[];sources=[]
    for row in rows[:64]:
        original=Path(row['path']);record=json.loads(row['body'])
        report_path=original.parent/'verification.json';discrepancy_path=original.parent/'source-discrepancies.json'
        identities={p:sealed(p,digest) for p,digest in [(report_path,record['report_sha256']),(discrepancy_path,record['discrepancy_sha256'])]}
        report=json.loads(report_path.read_bytes())
        discrepancies=json.loads(discrepancy_path.read_bytes())['records']
        skip=_top_k_skip(edges,interactions,limit,row,record,report)
        if skip is None:
            with resolve(base,row) as path:
                graph_identity=sealed(path,row['id'])
                db=connect(path)
                try:
                    for table,target in [('edges',edges),('interactions',interactions)]:
                        target.extend(dict(r,shard=row['id']) for r in db.execute(f'select * from {table} where sender=? or recipient=? order by block desc,id limit ?',(address,address,limit+1)))
                finally:db.close()
                if file_identity(path)!=graph_identity:raise ValueError('SEARCH_SEALED_SHARD_CHANGED')
        conflicts.extend(dict(d,shard=row['id']) for d in discrepancies if address in (d['prior_sender'],d['snapshot_sender']))
        if any(file_identity(p)!=value for p,value in identities.items()):raise ValueError('SEARCH_SEALED_SHARD_CHANGED')
        sources.append(dict(shard=row['id'],first=row['first'],last=row['last'],index_queried=skip is None,
            index_skip_reason=None if skip is None else 'strict_top_k_block_bound',top_k_block_bounds=skip,
            verification_metadata_verified=True,source_conflicts_collected=True))
    def ordered(items):return sorted(items,key=lambda r:(-r['block'],r.get('id',''),r['shard']))[:limit]
    return dict(chain_id=chain,address=address,edges=ordered(edges),interactions=ordered(interactions),source_conflicts=ordered(conflicts),sources=sources,
        coverage=coverage,scope='registered_snapshot_shards',limited=len(rows)>64 or len(edges)>limit or len(interactions)>limit or len(conflicts)>limit,
        trace_available=False,whole_history_complete=False,current_balance_available=False,owner_inferred=False,independent_consensus_verified=False)
