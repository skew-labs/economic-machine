"""Opt-in batch admission only. No daemon, source calls or production mutation."""
import hashlib,json,math,os
from pathlib import Path
import time
GiB=2**30
DEADLINE=1792351694298
ROOT=Path('/srv/economic-machine/collector')
def _load(path):
    p=Path(path)
    if p.is_symlink() or p.stat().st_size>131072:raise ValueError('TELEMETRY_BOUND')
    raw=p.read_bytes()
    return json.loads(raw),hashlib.sha256(raw).hexdigest(),int(p.stat().st_mtime*1000)
def observe(now_ms=None):
    now_ms=time.time_ns()//1000000 if now_ms is None else now_ms
    mem={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        k,v=line.split(':',1);mem[k]=int(v.split()[0])*1024
    psi={}
    for name in ('io','memory','cpu'):
        for line in Path('/proc/pressure',name).read_text().splitlines():
            fields=line.split()
            if fields[0]=='full' or name=='cpu' and fields[0]=='some':
                psi[name]=float(dict(p.split('=') for p in fields[1:])['avg10'])
    native,nh,nt=_load(ROOT/'data/result-archive/status.json')
    chart,ch,ct=_load(ROOT/'data/observed-chart/status.json')
    fs=os.statvfs('/srv/economic-machine')
    return dict(at_ms=now_ms,available_bytes=mem['MemAvailable'],free_bytes=fs.f_bavail*fs.f_frsize,
                io_full=psi['io'],memory_full=psi['memory'],cpu_some=psi['cpu'],
                native_age_ms=now_ms-nt,chart_age_ms=now_ms-ct,
                connected=native.get('connected') is True,
                pending_bytes=native.get('pendingBytes'),backlog_segments=chart.get('backlogSegments'),
                pending_overflow=chart.get('pendingOverflow'),caught_up=chart.get('caughtUpToObservedDurableTail'),
                native_sha256=nh,chart_sha256=ch)
def decide(obs, previous=None, *, now_ms=None):
    previous=previous or {}
    now_ms=obs.get('at_ms') if now_ms is None else now_ms
    if type(now_ms) is not int or now_ms>=DEADLINE:
        return dict(state='DEADLINE',allow_new_work=False,reasons=['original_deadline'],at_ms=now_ms)
    numerical=('at_ms','available_bytes','free_bytes','io_full','memory_full','cpu_some','native_age_ms','chart_age_ms','pending_bytes','backlog_segments','pending_overflow')
    if any(type(obs.get(k)) not in (int,float) or not math.isfinite(obs[k]) or obs[k]<0 for k in numerical):
        return dict(state='HOLD',allow_new_work=False,reasons=['missing_or_invalid_telemetry'],at_ms=now_ms)
    immediate=[]
    if obs['at_ms']>now_ms or now_ms-obs['at_ms']>30000:immediate.append('host_telemetry_stale')
    if obs['native_age_ms']>10000 or obs['chart_age_ms']>10000:immediate.append('production_publication_stale')
    if obs.get('connected') is not True:immediate.append('production_source_not_connected')
    if obs['available_bytes']<20*GiB:immediate.append('ram_reserve_12GiB_plus_max_8GiB_research')
    if obs['free_bytes']<136*GiB:immediate.append('existing_136GiB_source_admission')
    if obs['memory_full']>1:immediate.append('memory_pressure')
    if obs['pending_overflow']>previous.get('pending_overflow',obs['pending_overflow']):immediate.append('new_projection_overflow')
    pressure=[]
    if obs['io_full']>20:pressure.append('sustained_io_pressure')
    if obs['cpu_some']>40:pressure.append('sustained_cpu_pressure')
    if obs['backlog_segments']>0 or obs['pending_bytes']>8*1024**2 or obs.get('caught_up') is not True:pressure.append('sustained_production_backlog')
    prior_at=previous.get('at_ms',0)
    if prior_at>now_ms:return dict(state='HOLD',allow_new_work=False,reasons=['state_clock_regression'],at_ms=now_ms)
    since=previous.get('pressure_since_ms') if pressure else None
    if pressure and (since is None or not previous.get('pressure_reasons')):since=now_ms
    if pressure and now_ms-since>=10000:immediate+=pressure
    # One fresh initial observation admits only original configured work.
    # Following a hold, require 10 healthy seconds before new dispatch.
    recovery=previous.get('healthy_since_ms')
    if immediate or pressure:recovery=None
    elif recovery is None:recovery=now_ms
    recovering=previous.get('state') in ('BACKOFF','HOLD','RECOVERING') and (recovery is None or now_ms-recovery<10000)
    state='BACKOFF' if immediate else 'RECOVERING' if recovering else 'ADMIT'
    return dict(state=state,allow_new_work=state=='ADMIT',reasons=immediate,
                pressure_reasons=pressure,pressure_since_ms=since,healthy_since_ms=recovery,
                at_ms=now_ms,pending_overflow=obs['pending_overflow'],
                existing_cpu_and_inflight_limits_unchanged=True,max_existing_research_memory_bytes=8*GiB,
                host_available_memory_reserve_bytes=12*GiB,retain_inflight_and_all_evidence=True,
                does_not_override_provider_budget_deadline_or_disk_gate=True)
def check(base, state_path, now_ms=None):
    # The native batch controller owns serialization. Refuse arbitrary state paths.
    base=Path(base).resolve();state_path=Path(state_path)
    parent=(base/'evidence/acceleration-72h/evm-focus-v1/amsterdam-primary-v1/resources').resolve()
    if state_path.parent.resolve()!=parent or state_path.is_symlink():raise ValueError('HOST_GUARD_STATE_PATH')
    previous={}
    if state_path.exists():
        previous,_,_=_load(state_path)
    try:obs=observe(now_ms);result=decide(obs,previous,now_ms=now_ms)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result=dict(state='HOLD',allow_new_work=False,reasons=['telemetry_unavailable'],at_ms=time.time_ns()//1000000 if now_ms is None else now_ms)
        obs={'error_class':type(exc).__name__}
    result['observation']=obs
    parent.mkdir(parents=True,exist_ok=True)
    temporary=state_path.with_name(state_path.name+'.new')
    with open(temporary,'x') as f:
        json.dump(result,f,sort_keys=True);f.flush();os.fsync(f.fileno())
    os.replace(temporary,state_path)
    fd=os.open(parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    return result
