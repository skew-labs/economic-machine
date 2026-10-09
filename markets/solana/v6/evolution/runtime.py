import ctypes as C,time,copy,os
from pathlib import Path
from schema import validate_program,validate_profile
LIB=Path(os.environ.get('MP_EVOLUTION_LIB',str(Path(__file__).resolve().parents[1]/'build/libevolution.so')))
class Node(C.Structure):_fields_=[(s,C.c_int64) for s in ('op','a','b','c','value')]
class Output(C.Structure):_fields_=[('error',C.c_uint64),('values',C.c_int64*4),('fingerprint',C.c_uint64),('generation',C.c_uint64),('authority',C.c_uint64)]
class Engine:
    def __init__(self,program,profile,now=1,expires=10**18):
        validate_program(program);validate_profile(profile);self.profile=copy.deepcopy(profile);self.lib=C.CDLL(str(LIB));self.ptr=None
        self.lib.ev_create.argtypes=[C.POINTER(Node),C.c_uint64,C.POINTER(C.c_uint64),C.c_uint64,C.c_uint64];self.lib.ev_create.restype=C.c_void_p
        self.lib.ev_step.argtypes=[C.c_void_p,C.POINTER(C.c_int64),C.c_uint64,C.c_uint64,C.POINTER(Output)];self.lib.ev_step.restype=C.c_int
        self.lib.ev_destroy.argtypes=[C.c_void_p]
        self.ptr=self.lib.ev_create((Node*len(program['nodes']))(*(Node(*n) for n in program['nodes'])),len(program['nodes']),(C.c_uint64*4)(*program['outputs']),now,expires)
        if not self.ptr:raise ValueError('Economic Machine rejected graph')
    def raw(self,features,now=2,observed=None):
        if len(features)!=6 or any(type(x) is not int for x in features):raise ValueError('typed features')
        if any(not -10000<=x<=10000 for x in features) or not 0<now<2**64 or (observed is not None and not 0<observed<2**64):raise ValueError('input range')
        out=Output();start=time.perf_counter_ns();code=self.lib.ev_step(self.ptr,(C.c_int64*6)(*features),now,now if observed is None else observed,C.byref(out));elapsed=time.perf_counter_ns()-start
        if code or out.error or out.authority:raise ValueError('native decision rejected')
        return list(out.values),elapsed
    def decide(self,features,mark,bid=0,ask=0,now=2):
        if any(type(x) is not int for x in (mark,bid,ask)) or not 0<mark<0xffffff or not 0<=bid<=0xffffff or not 0<=ask<=0xffffff:raise ValueError('price input')
        values,ns=self.raw(features,now);skew,spread,clip,action=values;limits=self.profile['limits'];position=features[0]
        spread=max(limits['min_spread'],min(limits['max_spread'],spread));skew=max(-limits['max_skew'],min(limits['max_skew'],skew));clip=min(limits['clip'],clip)
        reduce=action==1 or abs(position)>=limits['position']
        if action==2 or not clip:return {'quotes':[],'reduce':reduce,'kernel_ns':ns,'raw':values}
        bp=max(1,mark+skew-spread);ap=min(0xffffff,mark+skew+spread)
        if ask:bp=min(bp,ask-1)
        if bid:ap=max(ap,bid+1)
        buy=min(clip,max(0,-position if reduce else limits['position']-position));sell=min(clip,max(0,position if reduce else limits['position']+position))
        quotes=[]
        if buy and 0<bp<ap:quotes.append({'side':0,'price':bp,'lots':buy,'reduce':reduce})
        if sell and 0<bp<ap<=0xffffff:quotes.append({'side':1,'price':ap,'lots':sell,'reduce':reduce})
        return {'quotes':quotes,'reduce':reduce,'kernel_ns':ns,'raw':values}
    def close(self):
        if self.ptr:self.lib.ev_destroy(self.ptr);self.ptr=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()
