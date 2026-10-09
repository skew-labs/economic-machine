"""Default research-worker backend; persistent state changes stay off the tick path."""
import ctypes as C,hashlib,os,time
from pathlib import Path
from . import bridge as native
from evaluate import load_window,manifest
from schema import digest
ROOT=Path(os.environ.get('MP_RESEARCH_ROOT',str(Path(__file__).resolve().parents[1]/'runtime')))
STORE=ROOT/'private/native-research/owners.sqlite'
def registry():
    STORE.parent.mkdir(mode=0o700,parents=True,exist_ok=True);return native.Registry(STORE)
def register_profiles(profiles):
    with registry() as r:
        return [r.ensure(p).revision for p in profiles]
def current_profile(r,p):
    wire=r.get(p['user_namespace'])
    if wire is None or wire.hash.hex()!=p['profile_hash']:raise ValueError('owner revision changed; refresh research proposal')
    return wire
def replay(program,profile,frames,receipts,delay=1,queue_multiplier=1):
    window=native.Window(frames,receipts);fence=window.fence(frames[0]['id']-1,window.frames[0].unix_ns-1,training=True)
    with registry() as r:wire=current_profile(r,profile)
    m,trace,elapsed=native.replay(program,profile,window,fence,delay,queue_multiplier,wire,with_trace=True)
    return {**native.report(m),'trace':trace,'trace_hash':digest(trace),'kernel_ns':[elapsed],'timing_scope':'entire native replay; not one decision'}
def compare(parent,candidate,profile,frames,receipts,*,cutoff,finished_unix,purge=10):
    data=manifest(frames,receipts);window=native.Window(frames,receipts)
    with registry() as r:wire=current_profile(r,profile);consumed=r.cursor(wire)
    fence=window.fence(cutoff,int(finished_unix*10**9),consumed,purge)
    result,report=native.compare(parent,candidate,profile,window,fence,wire)
    seat=profile['actor_seat'];a,b=frames[0],frames[-1]
    report.update(window=data,native_profile_revision=wire.revision,native_comparison_hex=bytes(result).hex(),native_fence_hex=bytes(fence).hex(),
        native_engine_sha256=hashlib.sha256(native.LIB.read_bytes()).hexdigest(),native_backend='economic-machine-cpp-v1',
        observed_actor={'seat':seat,'equity_change_tokens':b['equities'][seat]-a['equities'][seat],'ending_position':b['positions'][seat],'meaning':'observed existing v6 actor, not candidate performance'})
    return report
def commit_report(profile,parent,candidate,report):
    # Called after the old budget archive commits. If interrupted, reconciliation
    # below copies that exact committed record idempotently; it never calls Muse.
    result=native.Comparison.from_buffer_copy(bytes.fromhex(report['native_comparison_hex']))
    result.eligible=int(bool(result.eligible) and report['research_champion_eligible'])
    fence=native.Fence.from_buffer_copy(bytes.fromhex(report['native_fence_hex']));window=report['window']
    with registry() as r:
        wire=r.get(profile['user_namespace'],report['native_profile_revision'])
        if wire is None or wire.hash.hex()!=profile['profile_hash']:raise ValueError('native outcome profile mismatch')
        out=native.Outcome(wire.user,wire.hash,native.Digest.from_hex(digest(parent)),native.Digest.from_hex(digest(candidate)),native.Digest.from_hex(window['hash']),
            wire.revision,window['first_id'],window['last_id'],int(window['first_unix']*10**9),int(window['last_unix']*10**9),fence.cutoff_id,fence.finished_ns,result)
        native.check(r.lib.rp_outcome_append(r.ptr,C.byref(out)))
def reconcile_archive(archive):
    import json
    for row in archive.db.execute('SELECT * FROM generations ORDER BY id'):
        report=json.loads(row['report'])
        if report.get('native_backend')!='economic-machine-cpp-v1':continue
        p=json.loads(archive.profile(row['profile'])['body']);commit_report(p,archive.program(row['parent']),archive.program(row['candidate']),report)

class InstalledEngine(native.Engine):
    """Cold profile revision lookup once at an existing reconciled handoff."""
    def __init__(self,program,profile,now=1,expires=10**18):
        with registry() as r:
            wire=r.get(profile['user_namespace'])
            if wire is None:wire=r.append(profile,0)
            elif wire.hash.hex()!=profile['profile_hash']:raise ValueError('stale installation cannot replace owner preference revision')
        super().__init__(program,profile,now,expires,wire)
