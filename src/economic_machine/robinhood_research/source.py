"""Read-only public RPC bundles, validated before contiguous graph admission.

Raw envelopes preserve errors and Nitro before/after-EVM transfers. Their asset
semantics remain unclassified; callTracer rows are never a completeness claim.
"""
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request
from economic_machine.pump_research.store import canonical
from economic_machine.arbitrum_research.source import (SourceBackoff,SourceLimit,
    SourceIntegrityError,SourceHTTPError,_NoRedirect,retry_after,_json)
from economic_machine.arbitrum_research.graph import _fixed,_uint,_address

ENDPOINTS={4663:'https://robinhood.rpc.pinax.network',42161:'https://arb1.arbitrum.io/rpc',1:'https://ethereum-rpc.publicnode.com'}
OFFICIAL='https://rpc.mainnet.chain.robinhood.com'
GENESIS='0xaad15f3d702aaea00caf3e9bb56395efe9127bc3b31b24921abf1eee3409305c'
LIMIT=32*1024**2
PAGE_V1='economic-machine.robinhood-rpc-page.v1'
PAGE_V2='economic-machine.robinhood-rpc-page.v2'
ALLOWED={'eth_chainId','eth_getBlockByNumber','eth_getBlockReceipts','eth_getTransactionReceipt','eth_getTransactionByHash','eth_getLogs','eth_call','debug_traceBlockByNumber','debug_traceBlockByHash'}

class RPC:
    def __init__(self,chain_id=4663,*,official=False):
        if chain_id not in ENDPOINTS:raise ValueError('RPC_CHAIN')
        self.chain_id=chain_id;self.url=OFFICIAL if official and chain_id==4663 else ENDPOINTS[chain_id]
        self.opener=urllib.request.build_opener(_NoRedirect());self.last=0;self.identity_checked=False
        self.transport=None;self.last_transport=None
        if os.environ.get('EM_RPC_KEEPALIVE')=='1':
            from .rpc_http import PersistentRPC
            self.transport=PersistentRPC(self.url)
    def close(self):
        if self.transport is not None:self.transport.close()
    def call(self,method,params):
        if method not in ALLOWED:raise ValueError('READ_ONLY_RPC_METHOD')
        req=dict(jsonrpc='2.0',id=1,method=method,params=params)
        value=self.exchange(req)
        if not isinstance(value,dict) or value.get('id')!=1 or value.get('jsonrpc')!='2.0':raise SourceIntegrityError('RPC_ENVELOPE')
        if ('result' in value)==('error' in value):raise SourceIntegrityError('RPC_RESULT_ERROR')
        return dict(chain_id=self.chain_id,provider=self.url,request=req,response=value)
    def exchange(self,req):
        from .rpc_gate import request_gate
        with request_gate(self.url,len(req) if isinstance(req,list) else 1):
            return self._exchange(req)
    def _exchange(self,req):
        # The shared gate already charges every RPC and owns inter-request
        # pacing. An extra post-response sleep only idles an admitted reader.
        if not os.environ.get('EM_RPC_GATE_ROOT'):
            time.sleep(max(0,self.last+.15-time.monotonic()))
        if self.transport is not None:
            try:
                raw,self.last_transport=self.transport.post(canonical(req).encode(),LIMIT)
                return _json(raw)
            finally:self.last=time.monotonic()
        request=urllib.request.Request(self.url,data=canonical(req).encode(),headers={
            'Content-Type':'application/json','User-Agent':'EconomicMachineResearch/0.1'})
        try:
            with self.opener.open(request,timeout=30) as response:
                raw=response.read(LIMIT+1)
        except urllib.error.HTTPError as e:
            if e.code in (408,429,500,502,503,504,529):raise SourceBackoff(retry_after(e.headers.get('Retry-After')),e.code)
            raise SourceHTTPError(e.code) from None
        finally:self.last=time.monotonic()
        if len(raw)>LIMIT:raise SourceLimit('RPC_RESPONSE_BOUND')
        return _json(raw)
    def batch(self,calls):
        if not isinstance(calls,list) or not 1<=len(calls)<=16:raise ValueError('RPC_BATCH_BOUND')
        if any(method not in ALLOWED for method,params in calls):raise ValueError('READ_ONLY_RPC_METHOD')
        requests=[dict(jsonrpc='2.0',id=i+1,method=m,params=p) for i,(m,p) in enumerate(calls)]
        values=self.exchange(requests)
        if not isinstance(values,list) or len(values)!=len(requests):raise SourceIntegrityError('RPC_BATCH_COUNT')
        indexed={}
        for value in values:
            if (not isinstance(value,dict) or type(value.get('id')) is not int
                or not 1<=value['id']<=len(requests) or value['id'] in indexed
                or value.get('jsonrpc')!='2.0' or ('result' in value)==('error' in value)):
                raise SourceIntegrityError('RPC_BATCH_IDENTITY')
            indexed[value['id']]=value
        return [dict(chain_id=self.chain_id,provider=self.url,request=req,response=indexed[req['id']]) for req in requests]
    def result(self,method,params):
        record=self.call(method,params)
        if 'error' in record['response']:raise SourceBackoff(60,None)
        return record['response']['result'],record
    def check(self):
        result,record=self.result('eth_chainId',[])
        if _uint(result,'chain')!=self.chain_id:raise SourceIntegrityError('RPC_WRONG_CHAIN')
        self.identity_checked=True;return record


def result(record,method,params,chain=4663):
    ident=record.get('request',{}).get('id')
    if (record.get('chain_id')!=chain or record.get('provider') not in (ENDPOINTS[chain],OFFICIAL)
        or type(ident) is not int or not 1<=ident<=16
        or record.get('request')!={'jsonrpc':'2.0','id':ident,'method':method,'params':params}):
        raise SourceIntegrityError('RPC_REQUEST_IDENTITY')
    response=record['response']
    if response.get('id')!=ident or response.get('jsonrpc')!='2.0' or 'error' in response or 'result' not in response:
        raise SourceIntegrityError('RPC_RESULT_REQUIRED')
    return response['result']


def flatten(frame,index,txhash,path=(),depth=0):
    if not isinstance(frame,dict) or depth>128:raise SourceIntegrityError('TRACE_DEPTH_OR_SHAPE')
    kind=frame.get('type','').upper();children=frame.get('calls',[])
    if not isinstance(children,list):raise SourceIntegrityError('TRACE_CHILDREN')
    row=dict(transactionIndex=index,transactionHash=txhash,traceAddress=list(path),subtraces=len(children),
        error=frame.get('error'),revertReason=frame.get('revertReason'))
    if kind in ('CREATE','CREATE2'):
        row.update(type='create',action=dict(from_=None),result={'address':frame.get('to')})
        row['action']={'from':frame.get('from'),'value':frame.get('value','0x0'),'init':frame.get('input')}
    elif kind in ('SELFDESTRUCT','SUICIDE'):
        row.update(type='suicide',action={'address':frame.get('from'),'refundAddress':frame.get('to'),'balance':frame.get('value')})
    else:
        row.update(type='call',action={'from':frame.get('from'),'to':frame.get('to'),
            'value':frame.get('value','0x0'),'callType':kind.lower(),'input':frame.get('input')})
    out=[row]
    for i,child in enumerate(children):out.extend(flatten(child,index,txhash,path+(i,),depth+1))
    return out


def normalize(entry,number):
    tag=hex(number);block=result(entry['block'],'eth_getBlockByNumber',[tag,True])
    if not isinstance(block,dict) or _uint(block.get('number'),'number')!=number:raise SourceIntegrityError('BLOCK_NUMBER')
    h=_fixed(block.get('hash'),32,'block hash');parent=_fixed(block.get('parentHash'),32,'parent hash')
    if number==0 and h!=GENESIS:raise SourceIntegrityError('GENESIS_CHANGED')
    binding=entry.get('binding','block_number')
    if binding not in ('block_number','block_hash'):raise SourceIntegrityError('BLOCK_BINDING_MODE')
    txs=block.get('transactions');receipts=result(entry['receipts'],'eth_getBlockReceipts',[h if binding=='block_hash' else tag])
    if not isinstance(txs,list) or not isinstance(receipts,list) or len(txs)!=len(receipts):raise SourceIntegrityError('RECEIPT_COUNT')
    byindex={}
    for receipt in receipts:
        i=_uint(receipt.get('transactionIndex'),'receipt index')
        if i in byindex:raise SourceIntegrityError('DUPLICATE_RECEIPT')
        byindex[i]=receipt
    normalized=[];logs=[]
    for i,tx in enumerate(txs):
        if i not in byindex:raise SourceIntegrityError('MISSING_RECEIPT')
        receipt=byindex[i];txhash=_fixed(tx.get('hash'),32,'tx hash')
        for record in (tx,receipt):
            if (_uint(record.get('transactionIndex'),'index')!=i or _uint(record.get('blockNumber'),'block')!=number
                or _fixed(record.get('blockHash'),32,'hash')!=h):raise SourceIntegrityError('RECEIPT_BLOCK_BINDING')
        if _fixed(receipt.get('transactionHash'),32,'hash')!=txhash:raise SourceIntegrityError('RECEIPT_TX_BINDING')
        status=_uint(receipt.get('status'),'status')
        if status not in (0,1):raise SourceIntegrityError('RECEIPT_STATUS')
        loglist=receipt.get('logs')
        if not isinstance(loglist,list) or (status==0 and loglist):raise SourceIntegrityError('FAILED_RECEIPT_LOG')
        for log in loglist:
            if (log.get('removed') is True or _uint(log.get('blockNumber'),'block')!=number
                or _fixed(log.get('blockHash'),32,'hash')!=h or _fixed(log.get('transactionHash'),32,'hash')!=txhash
                or _uint(log.get('transactionIndex'),'index')!=i):raise SourceIntegrityError('LOG_BINDING')
            normalized_log=dict(log);normalized_log.update(transactionIndex=i,logIndex=_uint(log['logIndex'],'log index'))
            logs.append(normalized_log)
        merged=dict(tx);merged.update(transactionIndex=i,status=status,contractAddress=receipt.get('contractAddress'))
        normalized.append(merged)
    if sorted(x['logIndex'] for x in logs)!=list(range(len(logs))):raise SourceIntegrityError('LOG_INDEX_GAP')
    traces=[];gap='TRACE_UNAVAILABLE' if txs else 'NO_TRANSACTIONS'
    trace=entry.get('traces')
    if trace and 'result' in trace.get('response',{}):
        values=result(trace,'debug_traceBlockByHash' if binding=='block_hash' else 'debug_traceBlockByNumber',
            [h if binding=='block_hash' else tag,{'tracer':'callTracer'}])
        if not isinstance(values,list) or len(values)!=len(txs):raise SourceIntegrityError('TRACE_COUNT')
        for i,row in enumerate(values):
            if _fixed(row.get('txHash'),32,'trace tx')!=txs[i]['hash'].lower():raise SourceIntegrityError('TRACE_TX_BINDING')
            traces.extend(flatten(row['result'],i,txs[i]['hash']))
        # Number-based traces require a later hash recheck. Hash-based requests
        # already select the exact header; tx/receipt/log bindings still apply.
        if binding=='block_number':
            check=result(entry['trace_block'],'eth_getBlockByNumber',[tag,False])
            if check.get('hash','').lower()!=h:raise SourceIntegrityError('TRACE_BLOCK_CHANGED')
        gap='CALL_TRACER_AVAILABLE_NITRO_SYSTEM_TRANSFERS_UNCLASSIFIED'
    return dict(header=dict(number=number,hash=h,parentHash=parent,timestamp=block.get('timestamp')),
        transactions=normalized,logs=logs,traces=traces,trace_boundary=gap)


def parse_page(raw,start,end):
    if not isinstance(raw,bytes) or len(raw)>LIMIT:raise SourceLimit('PAGE_BOUND')
    bundle=json.loads(raw)
    schema=bundle.get('schema')
    if (schema not in (PAGE_V1,PAGE_V2) or bundle.get('chain_id')!=4663
        or bundle.get('start')!=start or bundle.get('requested_end')!=end):raise SourceIntegrityError('PAGE_IDENTITY')
    if _uint(result(bundle['identity'],'eth_chainId',[]),'identity')!=4663:raise SourceIntegrityError('CHAIN_IDENTITY')
    entries=bundle.get('entries')
    bound=64 if schema==PAGE_V2 else 16
    if not isinstance(entries,list) or not 1<=len(entries)<=min(bound,end-start+1):raise SourceIntegrityError('PAGE_RANGE')
    if schema==PAGE_V2 and any(e.get('binding')!='block_hash' for e in entries):
        raise SourceIntegrityError('PAGE_HASH_BINDING_REQUIRED')
    blocks=[normalize(entry,start+i) for i,entry in enumerate(entries)]
    for prior,later in zip(blocks,blocks[1:]):
        if prior['header']['hash']!=later['header']['parentHash']:raise SourceIntegrityError('PAGE_PARENT')
    delay=bundle.get('source_retry_after_seconds',0)
    if type(delay) not in (int,float) or not math.isfinite(delay) or not 0<=delay<=31536000:raise SourceIntegrityError('PAGE_RETRY_BOUND')
    return dict(raw=raw,sha256=hashlib.sha256(raw).hexdigest(),blocks=blocks,retry_after_seconds=delay,
        first_block=start,last_block=start+len(entries)-1,requested_end=end)

class PublicSource:
    def __init__(self,*,hash_bound=None,page_blocks=None):
        self.rpc=RPC();self.identity=None;self.trace_retry_at=0
        if hash_bound is not None and type(hash_bound) is not bool:raise ValueError('HASH_BOUND_MODE')
        self.hash_bound=os.environ.get('EM_ROBINHOOD_HASH_BOUND')=='1' if hash_bound is None else hash_bound
        if page_blocks is None:page_blocks=int(os.environ.get('EM_ROBINHOOD_PAGE_BLOCKS','16'))
        if type(page_blocks) is not int or page_blocks not in (16,64):raise ValueError('PAGE_BLOCKS')
        if page_blocks>16 and not self.hash_bound:raise ValueError('LARGE_PAGE_REQUIRES_HASH_BINDING')
        self.page_blocks=page_blocks
    def head(self):
        self.identity=self.rpc.check();head,_=self.rpc.result('eth_getBlockByNumber',['finalized',False])
        return _uint(head['number'],'head')
    def fetch(self,start,end):
        if self.hash_bound and hasattr(self.rpc,'batch'):return self.fetch_hash_bound(start,end)
        if hasattr(self.rpc,'batch'):return self.fetch_batched(start,end)
        return self.fetch_serial(start,end)

    def fetch_hash_bound(self,start,end):
        if type(start) is not int or type(end) is not int or not 0<=start<=end:raise ValueError('FETCH_RANGE')
        if self.identity is None:self.identity=self.rpc.check()
        entries=[];delay=0;batches=0;units=0;wire=0;decoded=0
        group_blocks=int(os.environ.get('EM_RPC_HEADER_GROUP','8'))
        if group_blocks not in (8,16):raise ValueError('RPC_HEADER_GROUP_BOUND')
        schema=PAGE_V2 if self.page_blocks>16 else PAGE_V1
        def batch(calls):
            nonlocal batches,units,wire,decoded
            records=[]
            for offset in range(0,len(calls),16):
                section=calls[offset:offset+16]
                batches+=1;units+=len(section)
                records.extend(self.rpc.batch(section))
                transport=getattr(self.rpc,'last_transport',None)
                if transport:
                    wire+=transport['wire_bytes'];decoded+=transport['decoded_bytes']
            return records
        for first in range(start,min(end,start+self.page_blocks-1)+1,group_blocks):
            numbers=list(range(first,min(end,start+self.page_blocks-1,first+group_blocks-1)+1))
            try:
                blocks=batch([('eth_getBlockByNumber',[hex(n),True]) for n in numbers])
                if any('error' in r['response'] for r in blocks):raise SourceBackoff(60,None)
                group=[];calls=[];targets=[]
                for i,record in enumerate(blocks):
                    block=result(record,'eth_getBlockByNumber',[hex(numbers[i]),True])
                    if not isinstance(block,dict) or _uint(block.get('number'),'number')!=numbers[i]:
                        raise SourceIntegrityError('BLOCK_NUMBER')
                    h=_fixed(block.get('hash'),32,'block hash')
                    if not isinstance(block.get('transactions'),list):raise SourceIntegrityError('BLOCK_TRANSACTIONS')
                    group.append(dict(binding='block_hash',block=record))
                    calls.append(('eth_getBlockReceipts',[h]));targets.append((i,'receipts'))
                    if block['transactions'] and time.time()>=self.trace_retry_at:
                        calls.append(('debug_traceBlockByHash',[h,{'tracer':'callTracer'}]));targets.append((i,'traces'))
                for (i,kind),record in zip(targets,batch(calls)):
                    group[i][kind]=record
                    if 'error' in record['response']:
                        if kind=='receipts':raise SourceBackoff(60,None)
                        if record['response']['error'].get('code')==-32601:self.trace_retry_at=time.time()+3600
                for i,entry in enumerate(group):normalize(entry,numbers[i])
                # Include the largest permitted retry field and newline in the
                # admission size so an accepted prefix always remains writable.
                proposed=dict(schema=schema,chain_id=4663,start=start,requested_end=end,
                    identity=self.identity,entries=entries+group,source_retry_after_seconds=31536000)
                if len((canonical(proposed)+'\n').encode())>LIMIT:
                    if not entries:raise SourceLimit('PAGE_BOUND')
                    break
                entries.extend(group)
            except (SourceBackoff,urllib.error.URLError,TimeoutError) as e:
                if not entries:raise
                delay=max(60,getattr(e,'retry_after_seconds',60));break
        bundle=dict(schema=schema,chain_id=4663,start=start,requested_end=end,
            identity=self.identity,entries=entries,source_retry_after_seconds=delay)
        page=parse_page((canonical(bundle)+'\n').encode(),start,end)
        page['response']=dict(fetch_mode='block_hash',request_batches=batches,rpc_units=units,
            page_block_cap=self.page_blocks,raw_schema=schema,consensus_finality_verified=False,
            header_group_blocks=group_blocks,http_keepalive=getattr(self.rpc,'transport',None) is not None,
            wire_bytes=wire,decoded_bytes=decoded)
        return page

    def fetch_batched(self,start,end):
        if type(start) is not int or type(end) is not int or not 0<=start<=end:raise ValueError('FETCH_RANGE')
        if self.identity is None:self.identity=self.rpc.check()
        entries=[];delay=0
        # Eight blocks per group, three round trips instead of 32. This does not
        # add concurrent requests or ignore the source's Retry-After instruction.
        for first in range(start,min(end,start+15)+1,8):
            numbers=list(range(first,min(end,start+15,first+7)+1))
            try:
                records=self.rpc.batch([call for n in numbers for call in
                    [('eth_getBlockByNumber',[hex(n),True]),('eth_getBlockReceipts',[hex(n)])]])
                if any('error' in r['response'] for r in records):raise SourceBackoff(60,None)
                group=[dict(block=records[2*i],receipts=records[2*i+1]) for i in range(len(numbers))]
                occupied=[i for i,e in enumerate(group) if e['block']['response']['result']['transactions']]
                if occupied and time.time()>=self.trace_retry_at:
                    traces=self.rpc.batch([('debug_traceBlockByNumber',[hex(numbers[i]),{'tracer':'callTracer'}]) for i in occupied])
                    traced=[]
                    for i,trace in zip(occupied,traces):
                        group[i]['traces']=trace
                        if 'error' in trace['response']:
                            if trace['response']['error'].get('code')==-32601:self.trace_retry_at=time.time()+3600
                        else:traced.append(i)
                    if traced:
                        checks=self.rpc.batch([('eth_getBlockByNumber',[hex(numbers[i]),False]) for i in traced])
                        if any('error' in r['response'] for r in checks):raise SourceBackoff(60,None)
                        for i,check in zip(traced,checks):group[i]['trace_block']=check
                for i,entry in enumerate(group):normalize(entry,numbers[i])
                # Keep a verified prefix when a dense second group exceeds the
                # page cap. Never claim uncommitted rows as covered blocks.
                proposed=dict(schema='economic-machine.robinhood-rpc-page.v1',chain_id=4663,start=start,
                    requested_end=end,identity=self.identity,entries=entries+group)
                if len(canonical(proposed).encode())>LIMIT:
                    if not entries:raise SourceLimit('PAGE_BOUND')
                    break
                entries.extend(group)
            except (SourceBackoff,urllib.error.URLError,TimeoutError) as e:
                if not entries:raise
                delay=max(60,getattr(e,'retry_after_seconds',60));break
        bundle=dict(schema='economic-machine.robinhood-rpc-page.v1',chain_id=4663,start=start,requested_end=end,
            identity=self.identity,entries=entries,source_retry_after_seconds=delay)
        return parse_page((canonical(bundle)+'\n').encode(),start,end)

    def fetch_serial(self,start,end):
        if type(start) is not int or type(end) is not int or not 0<=start<=end:raise ValueError('FETCH_RANGE')
        if self.identity is None:self.identity=self.rpc.check()
        entries=[];delay=0
        for n in range(start,min(end,start+15)+1):
            try:
                tag=hex(n);_,block=self.rpc.result('eth_getBlockByNumber',[tag,True]);_,receipts=self.rpc.result('eth_getBlockReceipts',[tag])
                entry=dict(block=block,receipts=receipts)
                if block['response']['result']['transactions'] and time.time()>=self.trace_retry_at:
                    entry['traces']=self.rpc.call('debug_traceBlockByNumber',[tag,{'tracer':'callTracer'}])
                    if 'error' in entry['traces']['response']:
                        if entry['traces']['response']['error'].get('code')==-32601:self.trace_retry_at=time.time()+3600
                    else:_,entry['trace_block']=self.rpc.result('eth_getBlockByNumber',[tag,False])
                normalize(entry,n);entries.append(entry)
            except (SourceBackoff,urllib.error.URLError,TimeoutError) as e:
                if not entries:raise
                delay=max(60,getattr(e,'retry_after_seconds',60));break
            bundle=dict(schema='economic-machine.robinhood-rpc-page.v1',chain_id=4663,start=start,requested_end=end,
                identity=self.identity,entries=entries)
            raw=(canonical(bundle)+'\n').encode()
            if len(raw)>LIMIT:raise SourceLimit('PAGE_BOUND')
            if len(raw)>16*1024**2:break
        bundle=dict(schema='economic-machine.robinhood-rpc-page.v1',chain_id=4663,start=start,requested_end=end,
            identity=self.identity,entries=entries,source_retry_after_seconds=delay)
        return parse_page((canonical(bundle)+'\n').encode(),start,end)
