"""Bounded EIP-1559 quote transport. Keys belong to an external signer.

Verifies signed bytes before submission, persists the known hash first, and
reconciles every batch member against quote logs and canonical block state.
HTTP is allowed only on loopback; deployment identity and fees are pinned.
"""
import http.client,json,os,socket,ssl,stat,struct,threading,time,urllib.parse
import rlp
from eth_account import Account
from eth_utils import keccak,to_checksum_address
from batch_journal import decode,hexword
class RPCFault(ValueError):
    def __init__(self,code):self.code=code;super().__init__('RPC operation rejected')
class RPCRequestFailure(ValueError):
    def __init__(self,reason,http_status=None):
        self.reason=reason;self.http_status=http_status;super().__init__('RPC request failed')
class BatchRejected(ValueError):pass

class RPC:
    def __init__(self,url,timeout=10):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password:raise ValueError('RPC URL')
        if parsed.scheme=='http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('insecure nonloopback RPC')
        self.host=parsed.hostname;self.port=parsed.port;self.tls=parsed.scheme=='https';self.path=parsed.path or '/'
        if parsed.query:self.path+='?'+parsed.query
        self.timeout=timeout;self.conn=None;self.serial=0;self.mutex=threading.Lock()
    def batch(self,requests,allow_errors=False):
        if not 1<=len(requests)<=128:raise ValueError('RPC batch capacity')
        with self.mutex:
            body=[]
            for method,params in requests:
                self.serial+=1;body.append({'jsonrpc':'2.0','id':self.serial,'method':method,'params':params})
            status=None
            try:
                if self.conn is None:
                    self.conn=http.client.HTTPSConnection(self.host,self.port,timeout=self.timeout,context=ssl.create_default_context()) if self.tls else http.client.HTTPConnection(self.host,self.port,timeout=self.timeout)
                self.conn.request('POST',self.path,json.dumps(body).encode(),{'Content-Type':'application/json','User-Agent':'MachineBook/2'})
                response=self.conn.getresponse();status=response.status;raw=response.read(16*1024*1024+1)
                if response.status!=200 or len(raw)>16*1024*1024:raise ValueError('RPC response')
                replies=json.loads(raw)
                if not isinstance(replies,list) or len(replies)!=len(body):raise ValueError('RPC batch response')
                indexed={r['id']:r for r in replies}
                if len(indexed)!=len(body):raise ValueError('RPC response IDs')
                results=[]
                for request in body:
                    r=indexed.get(request['id'])
                    if not r:raise ValueError('RPC response IDs')
                    if 'error' in r:
                        failure=RPCFault(r['error'].get('code'))
                        if not allow_errors:raise failure
                        results.append(failure)
                    elif 'result' in r:results.append(r['result'])
                    else:raise ValueError('RPC operation rejected')
                return results
            except RPCFault:raise
            except Exception as error:
                if self.conn:self.conn.close()
                self.conn=None
                reasons={'RPC response','RPC batch response','RPC response IDs','RPC operation rejected'}
                reason=str(error) if str(error) in reasons else 'HTTP or decoding failure'
                raise RPCRequestFailure(reason,status) from None
    def __call__(self,method,params):return self.batch([(method,params)])[0]
    def close(self):
        if self.conn:self.conn.close()

class SocketSigner:
    def __init__(self,path,timeout=5):self.path=path;self.timeout=timeout
    def sign(self,request):
        info=os.stat(self.path,follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('signer socket identity')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout);client.connect(self.path)
            _,uid,_=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.getuid():raise ValueError('signer peer identity')
            client.sendall(json.dumps({'method':'sign_transaction','transaction':request}).encode()+b'\n')
            response=bytearray()
            while not response.endswith(b'\n'):
                chunk=client.recv(4096)
                if not chunk or len(response)+len(chunk)>16384:raise ValueError('signer response')
                response.extend(chunk)
            value=json.loads(response)['raw']
            if not isinstance(value,str) or not value.startswith('0x'):raise ValueError('signed bytes')
            return bytes.fromhex(value[2:])

def verify_signed(raw,record,journal,max_gas=16000000):
    raw=bytes(raw)
    if not raw or raw[0]!=2 or len(raw)>4096:raise ValueError('only bounded EIP-1559')
    fields=rlp.decode(raw[1:],strict=True)
    if not isinstance(fields,list) or len(fields)!=12:raise ValueError('signed RLP fields')
    def integer(i):
        v=fields[i]
        if not isinstance(v,bytes) or len(v)>32 or (v and v[0]==0):raise ValueError('noncanonical integer')
        return int.from_bytes(v,'big')
    chain,nonce,priority,fee,gas=map(integer,range(5));value=integer(6)
    if chain!=journal.chain or nonce!=record['sender_nonce'] or fields[5]!=bytes.fromhex(journal.market[2:]) or value or fields[7]!=record['payload'] or fields[8]!=[]:raise ValueError('signed transaction scope')
    if not 0<gas<=max_gas or not 0<=priority<=fee or not fee or gas*fee>int(record['fee_cap']):raise ValueError('signed fee scope')
    order=0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141
    if integer(9)>1 or not 0<integer(10)<order or not 0<integer(11)<=order//2:raise ValueError('signature domain')
    if Account.recover_transaction(raw).lower()!=journal.sender:raise ValueError('wrong signer')
    return '0x'+keccak(raw).hex()

class Transport:
    def __init__(self,rpc,journal,signer,max_gas=16000000,priority_fee=0):
        self.rpc,self.journal,self.signer=rpc,journal,signer
        if not 21000<=max_gas<=32000000 or priority_fee<0:raise ValueError('transport limits')
        self.max_gas,self.priority_fee=max_gas,priority_fee
        self.quote_topic='0x'+keccak(text='Quote(uint32,uint256)').hex()
        self.account_selector=keccak(text='accounts(uint32)')[:4]
    def _identity(self,block):
        chain,code=self.rpc.batch([('eth_chainId',[]),('eth_getCode',[self.journal.market,block])])
        if int(chain,16)!=self.journal.chain or '0x'+keccak(bytes.fromhex(code[2:])).hex()!=self.journal.codehash:raise ValueError('deployment mismatch')
    def prepare(self,payload,fee_cap):
        decode(payload)
        started=time.monotonic_ns();anchor=self.rpc('eth_getBlockByNumber',['latest',False])
        request={'from':self.journal.sender,'to':self.journal.market,'data':'0x'+bytes(payload).hex(),'value':'0x0'}
        # Same anchored execution state; one persistent HTTP round trip for
        # independent identity, admission, estimation and sender-nonce reads.
        values=self.rpc.batch([('eth_chainId',[]),('eth_getCode',[self.journal.market,anchor['number']]),('eth_call',[request,anchor['number']]),('eth_estimateGas',[request,anchor['number']]),('eth_getTransactionCount',[self.journal.sender,'latest'])],allow_errors=True)
        for i in (0,1,4):
            if isinstance(values[i],RPCFault):raise values[i]
        if int(values[0],16)!=self.journal.chain or '0x'+keccak(bytes.fromhex(values[1][2:])).hex()!=self.journal.codehash:raise ValueError('deployment mismatch')
        if isinstance(values[2],RPCFault):raise BatchRejected('atomic quote batch rejected')
        if isinstance(values[3],RPCFault):raise values[3]
        gas=int(values[3],16);gas=gas+max(5000,gas//10)
        if gas>self.max_gas:raise ValueError('gas ceiling')
        base=int(anchor['baseFeePerGas'],16);fee=base*2+self.priority_fee
        if fee*gas>fee_cap:raise ValueError('fee ceiling')
        observed_nonce=int(values[4],16)
        expected=self.journal.db.execute('select next_nonce from config where id=1').fetchone()[0]
        if observed_nonce!=expected:raise ValueError('sender nonce disagreement')
        if self.rpc('eth_getBlockByNumber',[anchor['number'],False])['hash']!=anchor['hash']:raise ValueError('preflight anchor reorg')
        if time.monotonic_ns()-started>=1000000000:raise ValueError('preflight freshness exceeded')
        identity=self.journal.prepare(payload,fee_cap);record=self.journal.get(identity)
        transaction={'type':2,'chainId':self.journal.chain,'nonce':record['sender_nonce'],'to':to_checksum_address(self.journal.market),'value':0,'data':'0x'+record['payload'].hex(),'gas':gas,'maxFeePerGas':fee,'maxPriorityFeePerGas':self.priority_fee,'accessList':[]}
        return identity,transaction
    def admit(self,payload):
        members=decode(payload);anchor=self.rpc('eth_getBlockByNumber',['latest',False]);self._identity(anchor['number'])
        requests=[]
        for account,_,command in members:
            data=bytes.fromhex('ac8e6b9e')+account.to_bytes(32,'big')+command.to_bytes(32,'big')
            requests.append(('eth_call',[{'from':self.journal.sender,'to':self.journal.market,'data':'0x'+data.hex()},anchor['number']]))
        results=self.rpc.batch(requests,allow_errors=True)
        if self.rpc('eth_getBlockByNumber',[anchor['number'],False])['hash']!=anchor['hash']:raise ValueError('admission anchor reorg')
        return {account:nonce for (account,nonce,_),result in zip(members,results) if not isinstance(result,RPCFault)}
    def sign(self,identity,transaction):
        # Even an external signer cannot change target, chain, amount or command.
        record=self.journal.get(identity)
        if transaction.get('type')!=2 or transaction.get('chainId')!=self.journal.chain or transaction.get('nonce')!=record['sender_nonce'] or transaction.get('to','').lower()!=self.journal.market or transaction.get('value')!=0 or transaction.get('data')!='0x'+record['payload'].hex() or transaction.get('accessList')!=[]:raise ValueError('unsigned signing scope')
        gas=transaction['gas'];fee=transaction['maxFeePerGas'];priority=transaction['maxPriorityFeePerGas']
        if not 0<gas<=self.max_gas or not 0<=priority<=fee or gas*fee>int(record['fee_cap']):raise ValueError('unsigned signing fee')
        self.journal.mark_signing(identity,transaction)
        raw=self.signer.sign(transaction)
        txhash=verify_signed(raw,record,self.journal,self.max_gas);self.journal.signed(identity,txhash,raw);return txhash
    def send(self,identity):
        record=self.journal.get(identity)
        if record['state'] not in ('signed','broadcast'):raise ValueError('not signed for broadcast')
        verify_signed(record['raw'],record,self.journal,self.max_gas)
        self.journal.broadcast(identity) # Durable before the request can be accepted.
        txhash=self.rpc('eth_sendRawTransaction',['0x'+record['raw'].hex()])
        if txhash.lower()!=record['txhash']:raise ValueError('broadcast hash disagreement')
        return txhash
    def reconcile(self,identity):
        record=self.journal.get(identity)
        if record['state'] in ('prepared','signing','signed'):return record['state']
        if record['state'] in ('finalized','reverted','fenced','aborted','reorg'):return record['state']
        if record['state'] in ('included','included_revert'):
            state=self.audit()
            return state if state=='halted_reorg' else self.journal.get(identity)['state']
        previous=self.journal.checkpoint()
        receipt=self.rpc('eth_getTransactionReceipt',[record['txhash']])
        if receipt is None:return 'pending'
        if receipt['transactionHash'].lower()!=record['txhash']:raise ValueError('receipt transaction')
        block=self.rpc('eth_getBlockByNumber',[receipt['blockNumber'],False])
        if record['state']=='broadcast':
            if block is None or block['hash']!=receipt['blockHash']:return 'pending'
            self._identity(receipt['blockNumber'])
            calls=[('eth_call',[{'to':self.journal.market,'data':'0x'+(self.account_selector+a.to_bytes(32,'big')).hex()},receipt['blockNumber']]) for a,_,_ in record['members']]
            values=self.rpc.batch(calls);observed={}
            for (a,_,_),value in zip(record['members'],values):
                raw=bytes.fromhex(value[2:])
                if len(raw)!=128:raise ValueError('account ABI')
                observed[a]=int.from_bytes(raw[64:96],'big')
            quotes={}
            for log in receipt['logs']:
                if log['address'].lower()==self.journal.market and log['topics'][0]==self.quote_topic:
                    a=int(log['topics'][1],16)
                    if a in quotes or len(log['data'])!=66:raise ValueError('duplicate/malformed quote receipt')
                    quotes[a]=int(log['data'],16)
            if previous:
                old=self.rpc('eth_getBlockByNumber',[hex(previous[2]),False])
                if old is None or old['hash']!=previous[1]:return self.journal.canonical(previous[0],old['hash'] if old else None,-1)
            self.journal.included(identity,record['txhash'],receipt['blockHash'],int(receipt['blockNumber'],16),block['hash'],observed,int(receipt['gasUsed'],16)*int(receipt['effectiveGasPrice'],16),bool(int(receipt['status'],16)),quotes)
        state=self.audit()
        return state if state=='halted_reorg' else self.journal.get(identity)['state']
    def audit(self):
        checkpoint=self.journal.checkpoint()
        if checkpoint is None:return 'clear'
        canonical=self.rpc('eth_getBlockByNumber',[hex(checkpoint[2]),False])
        if canonical is None or canonical['hash']!=checkpoint[1]:return self.journal.canonical(checkpoint[0],canonical['hash'] if canonical else None,-1)
        finalized=self.rpc('eth_getBlockByNumber',['finalized',False]);height=int(finalized['number'],16) if finalized else -1
        # Recheck after the finality read, before releasing reservations.
        check=self.rpc('eth_getBlockByNumber',[hex(checkpoint[2]),False])
        if check is None or check['hash']!=checkpoint[1]:return self.journal.canonical(checkpoint[0],check['hash'] if check else None,-1)
        self.journal.finalize_prefix(height);return 'canonical'
    def recover_fenced(self):
        finalized=self.rpc('eth_getBlockByNumber',['finalized',False])
        if not finalized:raise ValueError('finality unavailable')
        height=finalized['number'];self._identity(height)
        accounts=[a for a, in self.journal.db.execute('select account from accounts where halted=1')]
        if not accounts:raise ValueError('no halted accounts')
        calls=[('eth_call',[{'to':self.journal.market,'data':'0x'+(self.account_selector+a.to_bytes(32,'big')).hex()},height]) for a in accounts]
        observed={a:int.from_bytes(bytes.fromhex(v[2:])[64:96],'big') for a,v in zip(accounts,self.rpc.batch(calls))}
        nonce=int(self.rpc('eth_getTransactionCount',[self.journal.sender,height]),16)
        canonical=self.rpc('eth_getBlockByNumber',[height,False])
        self.journal.recover_fenced(observed,finalized['hash'],int(height,16),canonical['hash'],int(height,16),nonce)
