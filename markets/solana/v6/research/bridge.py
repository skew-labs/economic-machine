"""Cold serialization and C ABI only; replay, quote limits and history are C++."""
import copy,ctypes as C,hashlib,json,os,sys,time
from contextlib import ExitStack
from pathlib import Path
SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'evolution'))
from schema import validate_program,validate_profile,digest
from runtime import Node
LIB=Path(os.environ.get('MP_RESEARCH_LIB',str(SOURCE/'build/libmachine_research.so')))
U=C.c_uint64;I=C.c_int64
class Digest(C.Structure):
    _fields_=[('value',C.c_ubyte*32)]
    @classmethod
    def from_hex(cls,h):
        b=bytes.fromhex(h)
        if len(b)!=32:raise ValueError('digest width')
        return cls((C.c_ubyte*32).from_buffer_copy(b))
    def hex(self):return bytes(self.value).hex()
class Profile(C.Structure):
    _fields_=[('abi',U),('revision',U),('user',Digest),('hash',Digest),('parent',Digest)]+[(k,I) for k in ('position','clip','min_spread','max_spread','max_skew','loss')]+[('weights',I*5),('seat',U),('style',U),('reserved',U)]
class Quote(C.Structure):_fields_=[(k,I) for k in ('side','price','lots','reduce')]
class Decision(C.Structure):_fields_=[('raw',I*4),('quotes',Quote*2),('count',U),('reduce',U)]
class Frame(C.Structure):
    _fields_=[(k,U) for k in ('id','slot','unix_ns','oracle_slot','publish_ns')]+[(k,I) for k in ('mark','funding','imbalance','momentum','depth')]+[(k,U) for k in ('order_offset','order_count','verified','provenance','external','reserved')]
class Order(C.Structure):_fields_=[(k,I) for k in ('side','seat','price','lots')]
class Trade(C.Structure):_fields_=[('slot',U)]+[(k,I) for k in ('side','lots','turnover')]
class Fence(C.Structure):_fields_=[(k,U) for k in ('cutoff_id','finished_ns','consumed_id','purge_frames')]+[(k,I) for k in ('quote_cost','exit_cost','maker_fee_bps')]+[('training',U)]
class Metrics(C.Structure):_fields_=[(k,I) for k in ('pnl','drawdown','inventory_sum','max_position','adverse','fills','turnover','updates','exit_fee','maker_fees','network_cost','score_micro','frames','loss_stop')]
class Trace(C.Structure):_fields_=[('slot',U),('position',I),('equity',I)]
class Comparison(C.Structure):_fields_=[('parent',Metrics*2),('candidate',Metrics*2),('eligible',U)]
class Outcome(C.Structure):_fields_=[(k,Digest) for k in ('user','profile','parent','candidate','window')]+[(k,U) for k in ('revision','first_id','last_id','first_ns','last_ns','cutoff_id','finished_ns')]+[('comparison',Comparison)]
assert C.sizeof(Profile)==224 and C.sizeof(Frame)==128
_lib=None
def library():
    global _lib
    if _lib is not None:return _lib
    lib=C.CDLL(str(LIB));ptr=C.c_void_p
    specs={'rp_engine_create':([ptr,U,C.POINTER(U),C.POINTER(Profile),U,U],ptr),
        'rp_engine_step':([ptr,C.POINTER(I),I,I,I,U,U,C.POINTER(Decision)],C.c_int),'rp_engine_destroy':([ptr],None),
        'rp_replay':([ptr,C.POINTER(Frame),U,C.POINTER(Order),U,C.POINTER(Trade),U,C.POINTER(Fence),U,U,C.POINTER(Metrics),C.POINTER(Trace)],C.c_int),
        'rp_compare':([C.POINTER(ptr),C.POINTER(Frame),U,C.POINTER(Order),U,C.POINTER(Trade),U,C.POINTER(Fence),C.POINTER(Comparison)],C.c_int),
        'rp_registry_open':([C.c_char_p],ptr),'rp_registry_close':([ptr],None),
        'rp_profile_append':([ptr,C.POINTER(Profile),U],C.c_int),'rp_profile_get':([ptr,C.POINTER(Digest),U,C.POINTER(Profile)],C.c_int),
        'rp_outcome_append':([ptr,C.POINTER(Outcome)],C.c_int),'rp_outcome_get':([ptr,C.POINTER(Digest),U,U,C.POINTER(Outcome)],C.c_int),
        'rp_cursor':([ptr,C.POINTER(Digest),U,C.POINTER(U)],C.c_int)}
    for name,(args,result) in specs.items():f=getattr(lib,name);f.argtypes=args;f.restype=result
    _lib=lib;return lib
def check(code):
    if code:raise ValueError('native research rejection '+str(code))
def integer(x,minimum=-(2**63),maximum=2**63-1):
    if type(x) is not int or not minimum<=x<=maximum:raise ValueError('integer ABI range')
    return x
def user_key(name):return Digest.from_hex(hashlib.sha256(name.encode()).hexdigest())
def profile_wire(p,revision=1,parent=None):
    validate_profile(p);integer(revision,1)
    lim=p['limits'];return Profile(1,revision,user_key(p['user_namespace']),Digest.from_hex(p['profile_hash']),Digest.from_hex(parent) if parent else Digest(),
        *(lim[k] for k in ('position','clip','min_spread','max_spread','max_skew','loss_ticks')),
        (I*5)(*(p['weights'][k] for k in ('pnl','drawdown','inventory','adverse','churn'))),p['actor_seat'],('defensive','balanced','active').index(p['style']),0)
class Engine:
    def __init__(self,program,profile,now=1,expires=10**18,wire=None):
        validate_program(program);validate_profile(profile);self.profile=copy.deepcopy(profile);self.program=copy.deepcopy(program);self.wire=wire or profile_wire(profile);self.lib=library();self.ptr=None
        if bytes(self.wire)!=bytes(profile_wire(profile,self.wire.revision,self.wire.parent.hex())):raise ValueError('profile revision binding')
        integer(now,1,2**64-1);integer(expires,1,2**64-1)
        nodes=(Node*len(program['nodes']))(*(Node(*n) for n in program['nodes']));outputs=(U*4)(*program['outputs'])
        self.ptr=self.lib.rp_engine_create(nodes,len(nodes),outputs,C.byref(self.wire),now,expires)
        if not self.ptr:raise ValueError('native profile/program install rejected')
        self._input=(I*6)();self._decision=Decision()
    def decide(self,features,mark,bid=0,ask=0,now=2,observed=None):
        if len(features)!=6:raise ValueError('six features')
        for i,x in enumerate(features):self._input[i]=integer(x)
        for x in (mark,bid,ask):integer(x)
        integer(now,1,2**64-1);observed=now if observed is None else integer(observed,1,2**64-1)
        if not self.ptr:raise ValueError('closed engine')
        start=time.perf_counter_ns();code=self.lib.rp_engine_step(self.ptr,self._input,mark,bid,ask,now,observed,C.byref(self._decision));elapsed=time.perf_counter_ns()-start;check(code)
        d=self._decision;return {'quotes':[{'side':q.side,'price':q.price,'lots':q.lots,'reduce':bool(q.reduce)} for q in list(d.quotes)[:d.count]],'reduce':bool(d.reduce),'raw':list(d.raw),'kernel_ns':elapsed}
    def close(self):
        if self.ptr:self.lib.rp_engine_destroy(self.ptr);self.ptr=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
class Window:
    def __init__(self,frames,receipts):
        self.source=frames;self.receipts=receipts;orders=[];packed=[]
        if not 24<=len(frames)<=8192:raise ValueError('window size')
        for f in frames:
            offset=len(orders)
            for o in f['orders']:orders.append(Order(*(integer(o[k]) for k in ('side','seat','price','lots'))))
            packed.append(Frame(integer(f['id'],1),integer(f['slot'],1),integer(integer(f['unix'],1)*10**9,1),integer(f['oracle_slot'],0),integer(integer(f['publish_time'],1)*10**9,1),
                *(integer(f[k]) for k in ('mark','funding','imbalance','momentum','depth')),offset,len(orders)-offset,int(f['accounting_verified']),1 if f['provenance']=='controlled_devnet_self_play' else 0,int(f['external_holdout']),0))
        self.frames=(Frame*len(packed))(*packed);self.orders=(Order*len(orders))(*orders);self.trades=(Trade*len(receipts))(*(Trade(integer(r['slot'],1),*(integer(r[k]) for k in ('side','lots','turnover'))) for r in receipts))
        self.hash=digest({'frames':frames,'receipts':receipts})
    def fence(self,cutoff,finished_ns,consumed=0,purge=0,quote_cost=0,exit_cost=0,maker_fee_bps=0,training=False):
        return Fence(*(integer(v,0) for v in (cutoff,finished_ns,consumed,purge)),*(integer(v,0) for v in (quote_cost,exit_cost,maker_fee_bps)),int(training))
def replay(program,profile,window,fence,delay=1,queue=1,wire=None,with_trace=False):
    metrics=Metrics();trace=(Trace*len(window.frames))() if with_trace else None
    with Engine(program,profile,wire=wire) as e:
        start=time.perf_counter_ns();check(e.lib.rp_replay(e.ptr,window.frames,len(window.frames),window.orders,len(window.orders),window.trades,len(window.trades),C.byref(fence),delay,queue,C.byref(metrics),trace));elapsed=time.perf_counter_ns()-start
    return metrics,([[t.slot,t.position,t.equity] for t in trace] if trace is not None else None),elapsed
def report(m):
    return {'pnl_ticks':m.pnl,'drawdown_ticks':m.drawdown,'mean_abs_inventory':round(m.inventory_sum/m.frames,6),'max_position':m.max_position,
        'adverse_ticks':m.adverse,'filled_lots_estimate':m.fills,'turnover_ticks_estimate':m.turnover,'quote_updates':m.updates,'exit_fee_ticks':m.exit_fee,
        'maker_fees_ticks':m.maker_fees,'network_cost_ticks':m.network_cost,'loss_stop':bool(m.loss_stop),'score':m.score_micro/1e6}
def compare(parent,candidate,profile,window,fence,wire=None):
    if fence.training:raise ValueError('training cannot qualify as holdout')
    result=Comparison();scenarios=[]
    with ExitStack() as stack:
        engines=[stack.enter_context(Engine(p,profile,wire=wire)) for p in (parent,candidate,parent,candidate)]
        pointers=(C.c_void_p*4)(*(e.ptr for e in engines));start=time.perf_counter_ns()
        check(library().rp_compare(pointers,window.frames,len(window.frames),window.orders,len(window.orders),window.trades,len(window.trades),C.byref(fence),C.byref(result)))
        elapsed=time.perf_counter_ns()-start
    for i,(delay,queue) in enumerate(((1,1),(3,2))):
        a,b=result.parent[i],result.candidate[i];scenarios.append({'delay_frames':delay,'queue_multiplier':queue,'parent':report(a),'candidate':report(b),'score_delta':(b.score_micro-a.score_micro)/1e6})
    return result,{'scenarios':scenarios,'research_champion_eligible':bool(result.eligible),'live_promotion_eligible':False,'native_compare_ns':elapsed}
class Registry:
    def __init__(self,path):
        path=Path(path)
        if not path.exists():
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        self.lib=library();self.ptr=self.lib.rp_registry_open(os.fsencode(path))
        if not self.ptr:raise ValueError('native registry open')
    def get(self,name,revision=0):
        integer(revision,0)
        result=Profile();key=user_key(name);code=self.lib.rp_profile_get(self.ptr,C.byref(key),revision,C.byref(result))
        if code==106:return None
        check(code);return result
    def append(self,profile,expected):
        previous=self.get(profile['user_namespace']);wire=profile_wire(profile,expected+1,previous.hash.hex() if previous else None)
        check(self.lib.rp_profile_append(self.ptr,C.byref(wire),expected));return wire
    def ensure(self,profile):
        current=self.get(profile['user_namespace'])
        if current and current.hash.hex()==profile['profile_hash']:return current
        return self.append(profile,current.revision if current else 0)
    def cursor(self,profile):
        out=U();check(self.lib.rp_cursor(self.ptr,C.byref(profile.user),profile.revision,C.byref(out)));return out.value
    def commit(self,wire,parent,candidate,window,fence,comparison):
        if fence.training:raise ValueError('training cannot enter outcome history')
        a,b=window.frames[0],window.frames[-1];out=Outcome(wire.user,wire.hash,Digest.from_hex(digest(parent)),Digest.from_hex(digest(candidate)),Digest.from_hex(window.hash),wire.revision,a.id,b.id,a.unix_ns,b.unix_ns,fence.cutoff_id,fence.finished_ns,comparison)
        check(self.lib.rp_outcome_append(self.ptr,C.byref(out)));return out
    def outcome(self,wire,last_id):
        integer(last_id,1)
        out=Outcome();check(self.lib.rp_outcome_get(self.ptr,C.byref(wire.user),wire.revision,last_id,C.byref(out)));return out
    def close(self):
        if self.ptr:self.lib.rp_registry_close(self.ptr);self.ptr=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
