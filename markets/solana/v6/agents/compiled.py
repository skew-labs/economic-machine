"""Fixed ABI, no model/network calls. Installed program branches on every event."""
import ctypes as C,time
from pathlib import Path
from native import Quote
from program import parameters,content_hash
from feed_batch import Input,INPUT_BYTES
class Output(C.Structure):
    _fields_=[(n,C.c_uint64) for n in ('error','reduce','fingerprint','generation','until','authority','count')]+[('center',C.c_int64),('quotes',Quote*4),('mode',C.c_uint64),('version',C.c_uint64)]
class Compiled:
    def __init__(self,seat,expires_ns,proposal,now_ns=None):
        p=parameters({'p':proposal['parameters']})
        if proposal['program_hash']!=content_hash(p) or proposal['execution_authority'] is not False:raise RuntimeError('compiled program identity')
        self.lib=C.CDLL(str(Path(__file__).resolve().parents[1]/'build/libmachine_program.so'))
        self.lib.mp_create.argtypes=[C.c_uint64]*4+[C.POINTER(C.c_uint64)];self.lib.mp_create.restype=C.c_void_p
        self.lib.mp_decide.argtypes=[C.c_void_p,C.POINTER(Input),C.POINTER(Output)];self.lib.mp_decide.restype=C.c_int
        self.lib.mp_decode.argtypes=[C.c_char_p]+[C.c_uint64]*7+[C.POINTER(Input)];self.lib.mp_decode.restype=C.c_int
        self.lib.mp_destroy.argtypes=[C.c_void_p];self.lib.mp_checkpoint.argtypes=[C.c_void_p,C.c_uint64]
        self.ptr=self.lib.mp_create(seat,now_ns or time.monotonic_ns(),expires_ns,proposal['version'],(C.c_uint64*6)(*p));self.seat=seat
        if not self.ptr:raise RuntimeError('conditional program rejected by upstream runtime')
    def checkpoint(self,d):
        if self.lib.mp_checkpoint(self.ptr,d['state_generation']):raise RuntimeError('checkpoint rejected')
    def close(self):
        if self.ptr:self.lib.mp_destroy(self.ptr);self.ptr=None
    def decide(self,st,slot,observed_ns,features,now_ns=None):
        now=now_ns or time.monotonic_ns();wall=time.time()
        other=[o for o in st['orders'] if o['seat']!=self.seat and o['expiry']>=slot and (not o.get('policy_until') or o['policy_until']>wall)]
        bid=max((o['price'] for o in other if o['side']==0),default=0);ask=min((o['price'] for o in other if o['side']==1),default=0)
        x=Input(now,observed_ns,slot,st['oracle_slot'],st['oracle'],st['market_sequence'],st['seats'][self.seat]['position'],bid,ask,features['imbalance'],features['movement'],features['depth']);y=Output()
        return self.evaluate(x)
    def decide_bytes(self,data,slot,observed_ns,previous_mark=0,now_ns=None,wall=None):
        x=Input();now=now_ns or time.monotonic_ns();wall=int(time.time()) if wall is None else wall
        code=self.lib.mp_decode(data,len(data),self.seat,slot,observed_ns,now,wall,previous_mark,C.byref(x))
        if code:raise RuntimeError('compact market rejected: '+str(code))
        return self.evaluate(x),{'imbalance':x.imbalance,'movement':x.movement,'depth':x.depth},x.mark
    def decide_prepared(self,data,prepared,prepared_wall,slot,observed_ns,previous_mark=0,now_ns=None,wall=None):
        wall=int(time.time()) if wall is None else wall;now=now_ns or time.monotonic_ns()
        # A deadline may cross between publication and evaluation. Rescan at that
        # boundary instead of letting cached features extend an order's lifetime.
        if wall!=prepared_wall:return self.decide_bytes(data,slot,observed_ns,previous_mark,now,wall)
        x=Input.from_buffer_copy(prepared[self.seat*INPUT_BYTES:(self.seat+1)*INPUT_BYTES])
        if not x.now or x.slot!=slot or x.observed!=observed_ns:raise RuntimeError('invalid prepared market facts')
        x.now=now;x.movement=min(10000,abs(x.mark-previous_mark)*10000//previous_mark) if previous_mark else 0
        return self.evaluate(x),{'imbalance':x.imbalance,'movement':x.movement,'depth':x.depth},x.mark
    def evaluate(self,x):
        y=Output();start=time.perf_counter_ns();code=self.lib.mp_decide(self.ptr,C.byref(x),C.byref(y));elapsed=time.perf_counter_ns()-start
        if code or y.error or y.authority:raise RuntimeError(f'conditional decision rejected: {code}/{y.error}')
        return {'seat':self.seat,'position':x.position,'center':y.center,'reduce':bool(y.reduce),'mode':y.mode,'program_version':y.version,'fingerprint':y.fingerprint,'state_generation':y.generation,'valid_until_ns':y.until,'execution_authority':y.authority,'kernel_ns':elapsed,
                'quotes':[{'slot':q.slot,'price':q.price,'lots':q.lots,'reduce':bool(q.reduce)} for q in y.quotes[:y.count]]}
