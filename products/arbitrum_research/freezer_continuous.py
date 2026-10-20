"""Retained, bounded Arbitrum snapshot acquisition through registered search.

One child owns the original source ledger. Each child covers at most 262144
blocks and the durable supervisor only advances across registered pages. No
finished historical run is reopened. Pauses count in the wall-clock rate.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from economic_machine.arbitrum_research.freezer_authority import source_authority
from economic_machine.arbitrum_research.freezer_source import DEADLINE
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.store import atomic, canonical

BATCH = 262144
# Leave room for the current bounded source/graph batch and three archive
# restores above the original host free-space floor. This is an admission
# buffer, not a larger storage grant or a reservation shared with other jobs.
BATCH_HEADROOM_BYTES = 8 * 1024**3
STOP = False


def guard_batch_headroom(cloud, base=None):
    try:
        cloud.guard_local(BATCH_HEADROOM_BYTES)
    except ValueError as error:
        if str(error) != 'RESEARCH_CLOUD_LOCAL_FREE_SPACE' or base is None:
            raise
        from economic_machine.arbitrum_research.freezer_cache_pressure import evict_for_headroom
        result = evict_for_headroom(base, cloud, BATCH_HEADROOM_BYTES)
        # Eviction never grants admission. Reapply the original inventory and
        # fresh free-space checks, including the unchanged eight-GiB buffer.
        cloud.guard_local(BATCH_HEADROOM_BYTES)
        return result


def guard_host_admission(base, setting=None):
    setting = os.environ.get('EM_FREEZER_HOST_ADMISSION', '0') if setting is None else setting
    if setting not in ('0', '1'):
        raise ValueError('CONTINUOUS_HOST_ADMISSION_SETTING')
    if setting == '0':
        return None
    from economic_machine.arbitrum_research.freezer_host_admission import check
    path = Path(base) / 'evidence/acceleration-72h/evm-focus-v1/amsterdam-primary-v1/resources/batch-admission-state.json'
    return check(base, path)


def execution_amendment(path, digest, original_policy_sha, base):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 16384 or sha(path) != digest:
        raise ValueError('CONTINUOUS_EXECUTION_HASH')
    value = document(path)
    expected = dict(schema='economic-machine.freezer-execution-amendment.v1',
        original_policy_sha256=original_policy_sha, deadline_ms=DEADLINE,
        monthly_budget_krw=1000000, minimum_host_free_bytes=128*1024**3,
        batch_headroom_bytes=BATCH_HEADROOM_BYTES, source_writers=1, consumers=3,
        max_inflight=4, max_native_input_bytes=60*1024**2,
        reset_cursor=False, preserve_existing_policies=True)
    if any(value.get(k) != v for k,v in expected.items()):
        raise ValueError('CONTINUOUS_EXECUTION_SCOPE')
    if (type(value.get('first_batch')) is not int or value['first_batch'] < 11277952
            or type(value.get('authorized_at_ms')) is not int
            or value['authorized_at_ms'] > time.time_ns()//1000000):
        raise ValueError('CONTINUOUS_EXECUTION_TIME')
    flags = value.get('flags', {})
    required = {'EM_FREEZER_ENVELOPE_OVERLAP':'1', 'EM_FREEZER_DECODED_MEMORY':'1',
                'EM_FREEZER_HOST_ADMISSION':'1', 'EM_FREEZER_GRAPH_MEMORY_BUILD':'1',
                'EM_FREEZER_GRAPH_CACHE_MIB':'512'}
    if flags != required or any(os.environ.get(k) != v for k,v in flags.items()):
        raise ValueError('CONTINUOUS_EXECUTION_FLAGS')
    files = value.get('files', {})
    expected_files = {'products/arbitrum_research/freezer_continuous.py',
        'products/arbitrum_research/freezer_probe.py',
        'src/economic_machine/arbitrum_research/freezer_envelope_overlap.py',
        'src/economic_machine/arbitrum_research/freezer_decoded_memory.py',
        'src/economic_machine/arbitrum_research/freezer_host_admission.py',
        'src/economic_machine/arbitrum_research/freezer_cloud_index.py',
        'src/economic_machine/arbitrum_research/freezer_graph_options.py',
        'src/economic_machine/arbitrum_research/graph.py'}
    if value.get('decoder_routing') is not None:
        if value['decoder_routing'] != 'typed_v1' or value.get('decoder_amendment') != {
                'path':os.environ.get('EM_FREEZER_DECODER_AMENDMENT'),
                'sha256':os.environ.get('EM_FREEZER_DECODER_AMENDMENT_SHA')}:
            raise ValueError('CONTINUOUS_EXECUTION_DECODER')
        expected_files.add('src/economic_machine/arbitrum_research/freezer_decoder.py')
    elif os.environ.get('EM_FREEZER_DECODER_AMENDMENT') or os.environ.get('EM_FREEZER_DECODER_AMENDMENT_SHA'):
        raise ValueError('CONTINUOUS_EXECUTION_DECODER_REQUIRED')
    if set(files) != expected_files:
        raise ValueError('CONTINUOUS_EXECUTION_FILES')
    for relative, expected_sha in files.items():
        target = Path(base)/'source'/relative
        if target.is_symlink() or sha(target) != expected_sha:
            raise ValueError('CONTINUOUS_EXECUTION_IMPLEMENTATION_CHANGED')
    # Optional additive dependency pin; old immutable amendments keep their
    # exact implementation set and semantics. No arbitrary support paths.
    if 'support_files' in value:
        support = value['support_files']
        allowed = {'src/economic_machine/arbitrum_research/freezer_ram_cache_v1.py'}
        if type(support) is not dict or set(support) != allowed:
            raise ValueError('CONTINUOUS_EXECUTION_SUPPORT_FILES')
        for relative, expected_sha in support.items():
            if (type(expected_sha) is not str or len(expected_sha) != 64
                    or any(c not in '0123456789abcdef' for c in expected_sha)):
                raise ValueError('CONTINUOUS_EXECUTION_SUPPORT_HASH')
            target = Path(base)/'source'/relative
            if not target.is_file() or target.is_symlink() or sha(target) != expected_sha:
                raise ValueError('CONTINUOUS_EXECUTION_SUPPORT_CHANGED')
    return value


def execution_required():
    return (any(os.environ.get(k,'0') != '0' for k in
                ('EM_FREEZER_ENVELOPE_OVERLAP','EM_FREEZER_DECODED_MEMORY','EM_FREEZER_HOST_ADMISSION',
                 'EM_FREEZER_GRAPH_MEMORY_BUILD','EM_FREEZER_GRAPH_CACHE_MIB'))
            or bool(os.environ.get('EM_FREEZER_DECODER_AMENDMENT'))
            or bool(os.environ.get('EM_FREEZER_DECODER_AMENDMENT_SHA')))


def document(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def immutable(path, value):
    path = Path(path)
    if path.exists():
        if document(path) != value:
            raise ValueError('CONTINUOUS_POLICY_CHANGED')
    else:
        atomic(path, value)


def implementation_amendment(path,digest,original_policy_sha,pipeline_sha):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>8192 or sha(path)!=digest:
        raise ValueError('CONTINUOUS_AMENDMENT_HASH')
    value=document(path)
    if (value.get('schema')!='economic-machine.freezer-implementation-amendment.v1'
            or value.get('original_policy_sha256')!=original_policy_sha
            or value.get('pipeline_sha256')!=pipeline_sha or value.get('consumers')!=3
            or value.get('max_inflight')!=4 or value.get('deadline_ms')!=DEADLINE
            or value.get('monthly_budget_krw')!=1000000 or value.get('reset_cursor') is not False
            or type(value.get('first_batch')) is not int or value['first_batch']<11277952
            or type(value.get('authorized_at_ms')) is not int or value['authorized_at_ms']>time.time_ns()//1000000):
        raise ValueError('CONTINUOUS_AMENDMENT_SCOPE')
    return value


def adaptive_amendment(path,digest,original_policy_sha,baseline_pipeline_sha,adaptive_pipeline_sha):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>8192 or sha(path)!=digest:
        raise ValueError('CONTINUOUS_ADAPTIVE_HASH')
    value=document(path)
    expected=dict(schema='economic-machine.freezer-adaptive-amendment.v1',
        original_policy_sha256=original_policy_sha,baseline_pipeline_sha256=baseline_pipeline_sha,
        adaptive_pipeline_sha256=adaptive_pipeline_sha,deadline_ms=DEADLINE,monthly_budget_krw=1000000,
        source_writers=1,consumers=3,max_inflight=4,max_page_blocks=16384,max_native_input_bytes=60*1024**2,
        batch_blocks=65536,reset_cursor=False,preserve_existing_policies=True)
    if any(value.get(k)!=v for k,v in expected.items()) or type(value.get('first_batch')) is not int or value['first_batch']<11277952:
        raise ValueError('CONTINUOUS_ADAPTIVE_SCOPE')
    if type(value.get('authorized_at_ms')) is not int or value['authorized_at_ms']>time.time_ns()//1000000:
        raise ValueError('CONTINUOUS_ADAPTIVE_TIME')
    return value


def next_batch(cursor, end):
    if type(cursor) is not int or type(end) is not int or cursor < 0:
        raise ValueError('CONTINUOUS_RANGE')
    return None if cursor > end else (cursor, min(cursor + BATCH - 1, end))


def plan_avoiding_registered(cursor,end,records):
    """Do not reacquire independently verified probe shards inside this run."""
    planned=next_batch(cursor,end)
    if planned is None:return None
    lo,hi=planned
    candidates=sorted((r for r in records if r['last']>=lo and r['first']<=hi),key=lambda r:(r['first'],r['last']))
    if not candidates:return dict(kind='acquire',first=lo,last=hi)
    row=candidates[0]
    if row['first']<=lo:return dict(kind='skip_registered',first=lo,last=min(end,row['last']),proof=row)
    return dict(kind='acquire',first=lo,last=row['first']-1)


def registered_exclusions(base,out,registry,cursor,end):
    from economic_machine.arbitrum_research.freezer_cloud_index import resolve
    hi=min(end,cursor+BATCH-1)
    registry.row_factory=sqlite3.Row
    try:rows=registry.execute('select * from shards where chain=42161 and first<=? and last>=?',(hi,cursor)).fetchall()
    finally:registry.row_factory=None
    records=[]
    for row in rows:
        folder=Path(row['path']).parent
        if folder.is_relative_to(out):continue
        body=json.loads(row['body'])
        if sha(folder/'verification.json')!=body['report_sha256']:raise ValueError('CONTINUOUS_EXTERNAL_REPORT_CHANGED')
        with resolve(base,row) as path:
            if sha(path)!=row['id']:raise ValueError('CONTINUOUS_EXTERNAL_SHARD_CHANGED')
        report=document(folder/'verification.json')
        if (report['state']!='VERIFIED_SEARCHABLE_SHARD' or report['source_blob_readback'] is not True
                or report['blocks']!=row['last']-row['first']+1 or report['graph']['chain_id']!=42161):
            raise ValueError('CONTINUOUS_EXTERNAL_UNVERIFIED')
        records.append(dict(first=row['first'],last=row['last'],index_sha256=row['id'],report_sha256=body['report_sha256'],path=row['path']))
    return records


def import_windows(db, registry, folder, child, start, end):
    """Only a verified contiguous prefix can become supervisor progress."""
    expected = start
    for w in child.get('windows', []):
        if (w['start'] != expected or w['end'] != w['start'] + w['blocks'] - 1
                or not 0 < w['blocks'] <= 16384 or w['end'] > end):
            raise ValueError('CONTINUOUS_PAGE_GAP')
        path = str(Path(folder) / f"window-{w['start']}-{w['end']}" / 'index.sqlite3')
        row = registry.execute('select id,first,last from shards where chain=42161 and path=?', (path,)).fetchone()
        if row != (w['index_sha256'], w['start'], w['end']):
            raise ValueError('CONTINUOUS_UNREGISTERED_PAGE')
        previous = db.execute('select body from pages where first=?', (w['start'],)).fetchone()
        body = canonical(dict(w, batch=str(folder)))
        if previous and previous[0] != body:
            raise ValueError('CONTINUOUS_PAGE_CHANGED')
        with db:
            db.execute('insert or ignore into pages values(?,?,?,?)', (w['start'], w['end'], w['blocks'], body))
        expected = w['end'] + 1
    if child.get('next_block') != expected:
        raise ValueError('CONTINUOUS_CURSOR_WITHOUT_PROOF')
    if child.get('state') == 'COMPLETE_BOUNDED_WINDOW' and expected != end + 1:
        raise ValueError('CONTINUOUS_INCOMPLETE_BATCH')
    return expected


def retryable(folder):
    """Retry only classified transport failures; retained proofs stay untouched."""
    path = Path(folder) / 'acquisition-error.json'
    if not path.exists():
        return False
    e = document(path)
    kind, message = e.get('error_type'), e.get('error', '')
    return (kind in ('TimeoutError', 'URLError', 'EOFError', 'ArchiveDeferred')
            or kind == 'HTTPError' and any(f'HTTP Error {n}:' in message for n in (429, 500, 502, 503, 504, 529)))


def sample(db, started, now):
    blocks = db.execute('select coalesce(sum(blocks),0) from pages').fetchone()[0]
    raw = [json.loads(row[0]) for row in db.execute('select body from pages')]
    elapsed = max(.001, (now - started) / 1000)
    return dict(indexed_blocks=blocks, elapsed_seconds=elapsed, blocks_per_second=blocks / elapsed,
                source_network_bytes=sum(w['network_bytes'] for w in raw),
                source_network_requests=sum(w['network_requests'] for w in raw),
                last_registered_at_ms=max((w['registered_at_ms'] for w in raw), default=None))


def measure(db, started, now):
    last = db.execute('select body from measurements order by id desc limit 1').fetchone()
    previous = json.loads(last[0]) if last else dict(ended_at_ms=started, total_blocks=0, total_source_bytes=0)
    if now - previous['ended_at_ms'] < 600000:
        return
    total = sample(db, started, now)
    rows = db.execute('select first,last,body from pages order by first').fetchall()
    prefix = previous['total_blocks']
    selected = []
    count = 0
    for row in rows:
        w = json.loads(row[2]); count += w['blocks']
        if count > prefix:
            selected.append(w)
    duration = (now - previous['ended_at_ms']) / 1000
    record = dict(started_at_ms=previous['ended_at_ms'], ended_at_ms=now, seconds=duration,
                  blocks=total['indexed_blocks'] - prefix, blocks_per_second=(total['indexed_blocks'] - prefix) / duration,
                  first=selected[0]['start'] if selected else None, last=selected[-1]['end'] if selected else None,
                  source_network_bytes=total['source_network_bytes'] - previous['total_source_bytes'],
                  total_blocks=total['indexed_blocks'], total_source_bytes=total['source_network_bytes'],
                  search_committed=True, nonoverlapping=True, includes_waits=True,
                  fully_cold=False, aligned_metadata_and_readahead_cache_reused=True,
                  all_pages_have_new_source_bytes=bool(selected) and all(w['network_bytes'] > 0 for w in selected),
                  whole_history_complete=False)
    with db:
        db.execute('insert into measurements(body) values(?)', (canonical(record),))


def main():
    parser = argparse.ArgumentParser()
    for key in ('base', 'out', 'binary', 'source-cache', 'source-authorization', 'source-authorization-sha', 'coverage-proof'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    parser.add_argument('--runtime-amendment')
    parser.add_argument('--runtime-amendment-sha')
    a = parser.parse_args()
    if a.start < 11277952 or a.start > a.end or time.time_ns() // 1000000 >= DEADLINE:
        raise ValueError('CONTINUOUS_SCOPE_OR_DEADLINE')
    base, out = Path(a.base).resolve(), Path(a.out).resolve()
    if not out.is_relative_to(base / 'evidence'):
        raise ValueError('CONTINUOUS_LOCATION')
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'supervisor.lock').open('a+b')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    authority = dict(path=str(Path(a.source_authorization).resolve()), sha256=a.source_authorization_sha)
    source = sqlite3.connect('file:' + str(Path(a.source_cache) / 'source.sqlite3') + '?mode=ro', uri=True)
    identity = source.execute('select body from identity').fetchone()[0]
    source_authority(authority, a.source_cache, identity)
    coverage = document(a.coverage_proof)
    if (coverage['source_identity_sha256'] != hashlib.sha256(identity.encode()).hexdigest()
            or a.start < coverage['common_first'] or a.end > coverage['common_last']):
        raise ValueError('CONTINUOUS_SNAPSHOT_SCOPE')
    pipeline = Path(__file__).with_name('freezer_pipeline_parallel.py')
    policy = dict(schema='economic-machine.arbitrum-continuous.v1', chain_id=42161, start=a.start, end=a.end,
                  deadline_ms=DEADLINE, monthly_budget_krw=1000000, batch_blocks=BATCH, source_writers=1,
                  consumers=2, inflight_pages=4, source_cap_bytes=None, source_cap_authorization=authority,
                  source_cache=str(Path(a.source_cache).resolve()), source_identity_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                  binary_sha256=sha(a.binary), pipeline_sha256=sha(pipeline), coverage_proof_sha256=sha(a.coverage_proof),
                  full_mapping_complete=False)
    amended=None
    if a.runtime_amendment or a.runtime_amendment_sha:
        if not a.runtime_amendment or not a.runtime_amendment_sha or not (out/'policy.json').exists():
            raise ValueError('CONTINUOUS_AMENDMENT_ARGUMENT')
        amended=implementation_amendment(a.runtime_amendment,a.runtime_amendment_sha,sha(out/'policy.json'),sha(pipeline))
        policy['pipeline_sha256']=document(out/'policy.json')['pipeline_sha256']
    immutable(out / 'policy.json', policy)
    adaptive=None
    adaptive_path=os.environ.get('EM_FREEZER_ADAPTIVE_AMENDMENT')
    adaptive_sha=os.environ.get('EM_FREEZER_ADAPTIVE_AMENDMENT_SHA')
    adaptive_pipeline=Path(__file__).with_name('freezer_pipeline_adaptive.py')
    if adaptive_path or adaptive_sha:
        if not adaptive_path or not adaptive_sha or not amended:
            raise ValueError('CONTINUOUS_ADAPTIVE_ARGUMENT')
        adaptive=adaptive_amendment(adaptive_path,adaptive_sha,sha(out/'policy.json'),sha(pipeline),sha(adaptive_pipeline))
    execution = None
    execution_path = os.environ.get('EM_FREEZER_EXECUTION_AMENDMENT')
    execution_sha = os.environ.get('EM_FREEZER_EXECUTION_AMENDMENT_SHA')
    if execution_path or execution_sha:
        if not execution_path or not execution_sha or not adaptive:
            raise ValueError('CONTINUOUS_EXECUTION_ARGUMENT')
        execution = execution_amendment(execution_path, execution_sha, sha(out/'policy.json'), base)
    elif execution_required():
        raise ValueError('CONTINUOUS_EXECUTION_AMENDMENT_REQUIRED')
    from economic_machine.arbitrum_research.freezer_decoder import configured as decoder_configured, select_batch
    decoder_route = decoder_configured(base)
    db = sqlite3.connect(out / 'runtime.sqlite3')
    db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS identity(body TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS pages(first INTEGER PRIMARY KEY,last INTEGER,blocks INTEGER,body TEXT);
      CREATE TABLE IF NOT EXISTS batches(first INTEGER PRIMARY KEY,last INTEGER,body TEXT);
      CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,body TEXT);
      CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY,body TEXT);
      CREATE TABLE IF NOT EXISTS registered_skips(first INTEGER PRIMARY KEY,last INTEGER,body TEXT);''')
    old = db.execute('select body from identity').fetchone()
    if old:
        initial = json.loads(old[0])
        if initial['policy_sha256'] != sha(out / 'policy.json'):
            raise ValueError('CONTINUOUS_IDENTITY_CHANGED')
    else:
        initial = dict(run_started_at_ms=time.time_ns() // 1000000, policy_sha256=sha(out / 'policy.json'))
        with db: db.execute('insert into identity values(?)', (canonical(initial),))
    started = initial['run_started_at_ms']
    cloud = Cloud(Path('/srv/economic-machine/solana'))
    registry = sqlite3.connect('file:' + str(base / 'evidence/freezer-search-v1/catalogue.sqlite3') + '?mode=ro', uri=True)
    child = None
    current_start = a.start
    row = db.execute('select max(last) from batches').fetchone()[0]
    if row is not None: current_start = row + 1
    skipped=db.execute('select max(last) from registered_skips').fetchone()[0]
    if skipped is not None:current_start=max(current_start,skipped+1)
    if execution and current_start < execution['first_batch']:
        raise ValueError('CONTINUOUS_EXECUTION_CURSOR')
    state = dict(state='RUNNING', run_started_at_ms=started, next_batch_start=current_start,
                 deadline_ms=DEADLINE, full_mapping_complete=False, trace_available=False,
                 independent_consensus_verified=False, next_batch_headroom_bytes=BATCH_HEADROOM_BYTES)
    if amended:state['implementation_amendment']=dict(path=a.runtime_amendment,sha256=a.runtime_amendment_sha,**amended)
    if adaptive:state['adaptive_amendment']=dict(path=adaptive_path,sha256=adaptive_sha,**adaptive)
    if execution:state['execution_amendment']=dict(path=execution_path,sha256=execution_sha,**execution)
    def publish():
        now = time.time_ns() // 1000000
        state.update(sample(db, started, now), at_ms=now, budget=cloud.spend.totals(),
                     measurements=[json.loads(r[0]) for r in db.execute('select body from measurements order by id')],
                     registered_external_skips=[json.loads(r[0]) for r in db.execute('select body from registered_skips order by first')])
        atomic(out / 'status.json', state)
    def stopping(signum, frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stopping); signal.signal(signal.SIGINT, stopping)
    try:
        while current_start <= a.end and not STOP:
            if time.time_ns() // 1000000 >= DEADLINE:
                state['state'] = 'DEADLINE_COMPLETE_PARTIAL'; break
            if execution:
                execution_amendment(execution_path, execution_sha, sha(out/'policy.json'), base)
            if sha(a.binary) != policy['binary_sha256'] or sha(pipeline) != (amended['pipeline_sha256'] if amended else policy['pipeline_sha256']):
                raise ValueError('CONTINUOUS_IMPLEMENTATION_CHANGED')
            try:
                pressure = guard_batch_headroom(cloud, base)
                if pressure:
                    state['cache_pressure'] = pressure
            except ValueError as e:
                if str(e) not in ('RESEARCH_CLOUD_LOCAL_FREE_SPACE', 'RESEARCH_CLOUD_LOCAL_STORAGE_LIMIT'):
                    raise
                state.update(state='RESOURCE_PAUSED', pause_reason=str(e)); publish(); time.sleep(30); continue
            if cloud.spend.totals()['remaining_krw'] <= 0:
                state['state'] = 'BUDGET_COMPLETE_PARTIAL'; break
            host_admission = guard_host_admission(base)
            if host_admission is not None:
                state['host_admission'] = host_admission
                if host_admission.get('allow_new_work') is not True:
                    state.update(state='RESOURCE_PAUSED', pause_reason='HOST_ADMISSION_BACKOFF')
                    publish(); time.sleep(30); continue
            adaptive_active=adaptive is not None and current_start>=adaptive['first_batch']
            if adaptive_active and sha(adaptive_pipeline)!=adaptive['adaptive_pipeline_sha256']:
                raise ValueError('CONTINUOUS_ADAPTIVE_IMPLEMENTATION_CHANGED')
            effective_end=min(a.end,current_start+adaptive['batch_blocks']-1) if adaptive_active else a.end
            plan=plan_avoiding_registered(current_start,effective_end,registered_exclusions(base,out,registry,current_start,effective_end))
            if plan['kind']=='skip_registered':
                with db:db.execute('insert or ignore into registered_skips values(?,?,?)',(plan['first'],plan['last'],canonical(plan)))
                current_start=plan['last']+1
                state.update(next_block=current_start,next_batch_start=current_start);publish();continue
            lo,hi=plan['first'],plan['last']
            folder = out / f'batch-{lo}-{hi}'
            decoder_route = decoder_configured(base)
            selected_binary = select_batch(base, folder, a.binary, lo, decoder_route)
            state.update(state='RUNNING', batch_start=lo, batch_end=hi, next_batch_start=lo)
            consumers=3 if amended and lo>=amended['first_batch'] else 2
            state['effective_consumers']=consumers
            state['selected_decoder_sha256']=sha(selected_binary)
            state['selected_decoder_path']=str(selected_binary)
            state.pop('pause_reason', None)
            if not (folder / 'status.json').exists() or document(folder / 'status.json')['state'] != 'COMPLETE_BOUNDED_WINDOW':
                selected_pipeline=adaptive_pipeline if adaptive_active else pipeline
                command = [sys.executable, str(selected_pipeline), '--base', str(base), '--out', str(folder),
                           '--binary', str(selected_binary), '--chain', '42161', '--start', str(lo), '--end', str(hi),
                           '--source-cache', a.source_cache, '--source-pipeline', '--source-authorization', authority['path'],
                           '--source-authorization-sha', authority['sha256']]
                if consumers!=2:command.extend(['--consumers',str(consumers)])
                attempt = dict(started_at_ms=time.time_ns() // 1000000, start=lo, end=hi)
                with db:
                    ident = db.execute('insert into attempts(body) values(?)', (canonical(attempt),)).lastrowid
                log = (out / f'attempt-{ident}.log').open('ab')
                child = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
                while child.poll() is None:
                    if (folder / 'status.json').exists():
                        c = document(folder / 'status.json')
                        state['next_block'] = import_windows(db, registry, folder, c, lo, hi)
                        state['batch_state'] = c['state']
                    publish()
                    if time.time_ns() // 1000000 >= DEADLINE:
                        os.killpg(child.pid, signal.SIGTERM); child.wait(timeout=30); break
                    time.sleep(5)
                attempt.update(ended_at_ms=time.time_ns() // 1000000, returncode=child.returncode)
                with db: db.execute('update attempts set body=? where id=?', (canonical(attempt), ident))
                child = None; log.close()
            c = document(folder / 'status.json')
            state['next_block'] = import_windows(db, registry, folder, c, lo, hi)
            state['batch_state'] = c['state']
            if c['state'] != 'COMPLETE_BOUNDED_WINDOW':
                if time.time_ns() // 1000000 >= DEADLINE:
                    state['state'] = 'DEADLINE_COMPLETE_PARTIAL'; break
                if retryable(folder):
                    state.update(state='SOURCE_BACKOFF', retained_batch=str(folder)); publish(); time.sleep(30); continue
                raise ValueError('CONTINUOUS_CHILD_REVIEW_REQUIRED')
            with db: db.execute('insert or ignore into batches values(?,?,?)', (lo, hi, canonical(c)))
            measure(db, started, time.time_ns() // 1000000)
            current_start = hi + 1
            state.update(next_batch_start=current_start, next_block=current_start)
            publish()
        else:
            state['state'] = 'STOPPED_PRESERVED' if STOP else 'SNAPSHOT_RANGE_COMPLETE'
        publish()
    except Exception as e:
        state.update(state='STOPPED_REVIEW_REQUIRED', error_type=type(e).__name__, error=str(e)[:200]); publish(); raise
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM); child.wait(timeout=30)
        registry.close(); source.close(); cloud.close(); db.close(); lock.close()


if __name__ == '__main__':
    main()
