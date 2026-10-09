"""Canonical block snapshot for actual MachineBook + IMarketOracle deployments.
Batch view calls, bound response age, and never claim a sequencer hint is a fill.
"""
import time
from eth_utils import keccak
from coordinator import Frame
def calldata(signature,value=None):
    raw=keccak(text=signature)[:4]
    if value is not None:raw+=value.to_bytes(32,'big')
    return '0x'+raw.hex()
def words(value,count):
    raw=bytes.fromhex(value[2:])
    if len(raw)!=count*32:raise ValueError('snapshot ABI')
    return [int.from_bytes(raw[i:i+32],'big') for i in range(0,len(raw),32)]
def signed(value):return value-(1<<256) if value>>255 else value
class Observer:
    def __init__(self,rpc,chain,market,codehash,sender,profiles,max_head_age=5):
        if not 1<=len(profiles)<=32 or not 1<=max_head_age<=60:raise ValueError('observer bounds')
        self.rpc,self.chain,self.market,self.sender,self.profiles=rpc,chain,market.lower(),sender.lower(),profiles
        self.max_head_age=max_head_age;self.sequence=0
        identity,code,oracle=self.rpc.batch([('eth_chainId',[]),('eth_getCode',[self.market,'latest']),('eth_call',[{'to':self.market,'data':calldata('oracle()')},'latest'])])
        if int(identity,16)!=chain or '0x'+keccak(bytes.fromhex(code[2:])).hex()!=codehash.lower():raise ValueError('observer deployment identity')
        raw=words(oracle,1)[0]
        if not 0<raw<2**160:raise ValueError('oracle identity')
        self.oracle='0x'+raw.to_bytes(20,'big').hex()
    def read(self,momentum=None):
        observed=time.monotonic_ns();anchor=self.rpc('eth_getBlockByNumber',['latest',False]);timestamp=int(anchor['timestamp'],16)
        if not -2<=time.time()-timestamp<=self.max_head_age:raise ValueError('stalled or future chain head')
        height=anchor['number'];requests=[('eth_call',[{'to':self.oracle,'data':calldata('read()')},height]),('eth_call',[{'to':self.market,'data':calldata('best(uint8)',0)},height]),('eth_call',[{'to':self.market,'data':calldata('best(uint8)',1)},height])]
        for p in self.profiles:
            requests.extend(('eth_call',[{'to':self.market,'data':calldata(sig,argument)},height]) for sig,argument in [('accounts(uint32)',p.account),('orders(uint32)',p.account*2),('orders(uint32)',p.account*2+1),('sessions(uint32)',p.account)])
        values=[]
        for offset in range(0,len(requests),128):values.extend(self.rpc.batch(requests[offset:offset+128]))
        if self.rpc('eth_getBlockByNumber',[height,False])['hash']!=anchor['hash']:raise ValueError('snapshot reorg')
        now=time.monotonic_ns()
        if now-observed>=1000000000:raise ValueError('snapshot exceeded fact freshness')
        index,_=words(values[0],2);bid=words(values[1],1)[0];ask=words(values[2],1)[0];self.sequence+=1;frames={}
        for i,p in enumerate(self.profiles):
            account=words(values[3+i*4],4);b=words(values[4+i*4],6);a=words(values[5+i*4],6);session=words(values[6+i*4],6)
            epoch=session[2] if p.session_epoch else 0
            if p.session_epoch and (epoch!=p.session_epoch or session[0]!=int(self.sender,16) or session[1]<=timestamp or session[5]&1==0):raise ValueError('quote session expired/revoked')
            alive=[o for o in (b,a) if o[2]];expiry=alive[0][4] if alive and all(o[4]==alive[0][4] for o in alive) else 0
            frames[p.account]=Frame(self.chain,now,observed,self.sequence,account[2],timestamp,index,bid,ask,signed(account[1]),(momentum or {}).get(p.account,0),b[3],a[3],b[2],a[2],expiry,epoch)
        return frames,{'number':height,'hash':anchor['hash'],'observed_ns':observed,'complete_ns':now}
