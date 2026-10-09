"""One signer lane, at most 32 native Economic Machine policies.
Hot path has fixed C++ storage; signing and durable IO stay on the cold path.
"""
import ctypes as C
class Profile(C.Structure):_fields_=[(x,C.c_uint64) for x in ['chain','account','version','expires_ns','initial_nonce','max_position','clip','spread','inventory_weight','ttl_seconds','session_epoch']]
class Frame(C.Structure):_fields_=[(x,C.c_uint64) for x in ['chain','now_ns','observed_ns','sequence','account_nonce','unix_seconds','index','best_bid','best_ask']]+[('inventory',C.c_int64),('momentum',C.c_int64)]+[(x,C.c_uint64) for x in ['resting_bid','resting_ask','resting_bid_lots','resting_ask_lots','resting_expiry','session_epoch']]
class Command(C.Structure):_fields_=[('calldata',C.c_uint8*68),('size',C.c_uint32)]+[(x,C.c_uint64) for x in ['nonce','generation','policy_version','execution_authority']]
class NativeLane:
    def __init__(self,path,profiles,now):
        if not 1<=len(profiles)<=32 or len({p.account for p in profiles})!=len(profiles):raise ValueError('lane capacity')
        self.lib=C.CDLL(str(path))
        self.lib.em_abi_version.restype=C.c_uint32
        if self.lib.em_abi_version()!=2:raise ValueError('native ABI mismatch')
        self.lib.em_create.argtypes=[C.POINTER(Profile),C.c_uint64];self.lib.em_create.restype=C.c_void_p
        self.lib.em_step.argtypes=[C.c_void_p,C.POINTER(Frame),C.POINTER(Command)];self.lib.em_step.restype=C.c_int
        self.lib.em_ack.argtypes=[C.c_void_p,C.c_uint64,C.c_int];self.lib.em_ack.restype=C.c_int
        self.lib.em_pack_batch.argtypes=[C.POINTER(Command),C.c_uint32,C.POINTER(C.c_uint8),C.c_uint32];self.lib.em_pack_batch.restype=C.c_int
        self.lib.em_bound_quote.argtypes=[C.c_void_p,C.c_uint64,C.c_uint64,C.POINTER(Command)];self.lib.em_bound_quote.restype=C.c_int
        self.lib.em_destroy.argtypes=[C.c_void_p]
        self.engines={};self.outstanding={};self.proposals={};self.suppressed=0;self.cohort_guards=0
        try:
            for p in profiles:
                engine=self.lib.em_create(C.byref(p),now)
                if not engine:raise ValueError('native profile rejected')
                self.engines[p.account]=engine
        except Exception:self.close();raise
    def propose(self,frames):
        if set(frames)!=set(self.engines):raise ValueError('incomplete coherent frame')
        emitted={}
        try:
            for account,engine in self.engines.items():
                command=Command();code=self.lib.em_step(engine,C.byref(frames[account]),C.byref(command))
                if code==7:self.suppressed+=1
                elif code==0:emitted[account]=command;self.proposals[account]=command;self.outstanding[account]=command.nonce
                elif code!=1:raise ValueError('native frame rejected')
            if not emitted:return b'',{}
            words=[int.from_bytes(bytes(c.calldata)[36:68],'big') for c in emitted.values()]
            bids=[(w>>104)&65535 for w in words if (w>>136)&0xffffffff]
            asks=[(w>>120)&65535 for w in words if (w>>168)&0xffffffff]
            if bids and asks and max(bids)>=min(asks):
                pivot=(max(bids)+min(asks))//2;self.cohort_guards+=1
                for account,command in emitted.items():
                    if self.lib.em_bound_quote(self.engines[account],pivot,pivot+1,C.byref(command))!=0:raise ValueError('cohort price guard')
            commands=(Command*len(emitted))(*emitted.values());out=(C.c_uint8*1220)()
            size=self.lib.em_pack_batch(commands,len(emitted),out,len(out))
            if size<0:raise ValueError('native pack rejected')
            return bytes(out[:size]),{a:c.nonce for a,c in emitted.items()}
        except Exception:
            self.ack({a:c.nonce for a,c in emitted.items()},False);raise
    def ack(self,members,success):
        if any(self.outstanding.get(a)!=n for a,n in members.items()):raise ValueError('acknowledgement identity')
        for account,nonce in members.items():
            if self.lib.em_ack(self.engines[account],nonce if success else nonce-1,int(success))!=0:raise ValueError('native acknowledgement')
            del self.outstanding[account]
            del self.proposals[account]
    def repack(self,members):
        if not members:return b''
        commands=(Command*len(members))(*(self.proposals[a] for a in members));out=(C.c_uint8*1220)()
        size=self.lib.em_pack_batch(commands,len(members),out,len(out))
        if size<0:raise ValueError('admitted repack')
        return bytes(out[:size])
    def close(self):
        for engine in self.engines.values():self.lib.em_destroy(engine)
        self.engines={}

class Coordinator:
    def __init__(self,lane,transport):self.lane,self.transport=lane,transport;self.pending={};self.halted=False
    def submit(self,frames,fee_cap):
        if self.halted:raise ValueError('lane halted')
        payload,members=self.lane.propose(frames)
        if not payload:return {'state':'coalesced','emitted':0}
        try:identity,request=self.transport.prepare(payload,fee_cap)
        except Exception as error:
            from transport import BatchRejected
            if not isinstance(error,BatchRejected):self.lane.ack(members,False);raise
            try:
                admitted=self.transport.admit(payload)
                self.lane.ack({a:n for a,n in members.items() if a not in admitted},False);members=admitted
                if not members:return {'state':'quarantined','emitted':0}
                identity,request=self.transport.prepare(self.lane.repack(members),fee_cap)
            except Exception:
                self.lane.ack(members,False);raise
        self.pending[identity]=members
        # Any failure from this point leaves the native proposals pending.
        self.transport.sign(identity,request);txhash=self.transport.send(identity)
        return {'state':'broadcast','emitted':len(members),'identity':identity,'hash':txhash}
    def poll(self):
        journal=self.transport.journal;results={}
        # Audit earlier provisional inclusions before allowing descendants.
        state=self.transport.audit()
        if state=='halted_reorg':self.halted=True;return {'checkpoint':state}
        for identity,members in list(self.pending.items()):
            state=self.transport.reconcile(identity);results[identity]=state
            if state in ('included','finalized','reverted'):
                self.lane.ack(members,state!='reverted');del self.pending[identity]
            elif state in ('halted_reorg','reorg'):self.halted=True
        return results
