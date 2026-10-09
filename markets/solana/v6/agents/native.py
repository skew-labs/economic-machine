"""Fixed C ABI into the real Economic Machine event runtime. No model/network I/O."""
import ctypes as C
import time
from pathlib import Path
class Input(C.Structure):
    _fields_=[(n,C.c_int64 if n=='position' else C.c_uint64) for n in ('now','observed','slot','oracle_slot','mark','market_sequence','position','best_bid','best_ask')]
class Quote(C.Structure):
    _fields_=[(n,C.c_uint64) for n in ('slot','price','lots','reduce')]
class Output(C.Structure):
    _fields_=[(n,C.c_uint64) for n in ('error','reduce','fingerprint','generation','until','authority','count')]+[('center',C.c_int64),('quotes',Quote*4)]
class Native:
    def __init__(self,seat,expires_ns,now_ns=None):
        self.lib=C.CDLL(str(Path(__file__).resolve().parents[1]/'build/libmachine_mm.so'))
        self.lib.mm_create.argtypes=[C.c_uint64]*3;self.lib.mm_create.restype=C.c_void_p
        self.lib.mm_decide.argtypes=[C.c_void_p,C.POINTER(Input),C.POINTER(Output)];self.lib.mm_decide.restype=C.c_int
        self.lib.mm_destroy.argtypes=[C.c_void_p]
        self.lib.mm_policy.argtypes=[C.c_void_p,C.c_uint64,C.c_uint64,C.c_uint64]
        self.lib.mm_checkpoint.argtypes=[C.c_void_p,C.c_uint64]
        self.ptr=self.lib.mm_create(seat,now_ns or time.monotonic_ns(),expires_ns)
        if not self.ptr:raise RuntimeError('native mandate rejected')
        self.seat=seat
    def policy(self,code,until_ns,now_ns=None):
        if self.lib.mm_policy(self.ptr,code,now_ns or time.monotonic_ns(),until_ns):raise RuntimeError('native policy rejected')
    def checkpoint(self,decision):
        if self.lib.mm_checkpoint(self.ptr,decision['state_generation']):raise RuntimeError('native checkpoint rejected')
    def close(self):
        if self.ptr:self.lib.mm_destroy(self.ptr);self.ptr=None
    def decide(self,st,slot,observed_ns,now_ns=None):
        now_ns=now_ns or time.monotonic_ns()
        others=[o for o in st['orders'] if o['seat']!=self.seat and o['expiry']>=slot]
        bid=max((o['price'] for o in others if o['side']==0),default=0)
        ask=min((o['price'] for o in others if o['side']==1),default=0)
        x=Input(now_ns,observed_ns,slot,st['oracle_slot'],st['oracle'],st['market_sequence'],st['seats'][self.seat]['position'],bid,ask);y=Output()
        start=time.perf_counter_ns();code=self.lib.mm_decide(self.ptr,C.byref(x),C.byref(y));duration=time.perf_counter_ns()-start
        if code or y.error or y.authority:raise RuntimeError(f'native decision rejected: {code}/{y.error}')
        return {'seat':self.seat,'position':x.position,'center':y.center,'reduce':bool(y.reduce),'fingerprint':y.fingerprint,'state_generation':y.generation,'valid_until_ns':y.until,'execution_authority':y.authority,'kernel_ns':duration,
                'quotes':[{'slot':q.slot,'price':q.price,'lots':q.lots,'reduce':bool(q.reduce)} for q in y.quotes[:y.count]]}
