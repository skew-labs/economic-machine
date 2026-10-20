"""Canonical Nitro message joins across Arbitrum/Robinhood and Ethereum.

Paired messages prove protocol linkage in retained RPC observations. They do not
prove common wallet ownership, CEX ledger continuity, token amount equivalence,
or independent L1 consensus. Direct third-party bridges remain explicit gaps.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from eth_hash.auto import keccak
from economic_machine.pump_research.store import canonical,atomic
from economic_machine.arbitrum_research.graph import _address,_fixed,_data,_uint,_topic_address
from .source import RPC,SourceIntegrityError

ARBSYS='0x0000000000000000000000000000000000000064'
ROUTES={
 42161:dict(outbox='0x0b9857ae2d4a3dbe74ffe1d7df045bb7f96e4840',inbox='0x4dbd4fc535ac27206064b68ffcf827b0a60bab3f',start=22207818),
 4663:dict(outbox='0xf0ce991ea4a0d2400a4ab49b20ae333f6dce3de9',inbox='0x1a07cc4bd17e0118bdb54d70990d2158abad7a2d',start=0)}
def topic(s):return '0x'+keccak(s.encode()).hex()
SEND=topic('L2ToL1Tx(address,address,uint256,uint256,uint256,uint256,uint256,uint256,bytes)')
EXECUTED=topic('OutBoxTransactionExecuted(address,address,uint256,uint256)')
INBOX=topic('InboxMessageDelivered(uint256,bytes)')
EXECUTE=topic('executeTransaction(bytes32[],uint256,address,address,uint256,uint256,uint256,uint256,bytes)')[:10]


def words(data):
    raw=_data(data)
    if len(raw)%32:raise ValueError('BRIDGE_ABI_SIZE')
    return [raw[i:i+32] for i in range(0,len(raw),32)]
def word_address(raw):return _topic_address('0x'+raw.hex())
def dynamic(raw,offset_word,head_words):
    offset=int.from_bytes(offset_word,'big')
    if offset!=head_words*32 or len(raw)<offset+32:raise ValueError('BRIDGE_DYNAMIC_OFFSET')
    size=int.from_bytes(raw[offset:offset+32],'big');end=offset+32+size
    if len(raw)!=(end+31)//32*32 or any(raw[end:]):raise ValueError('BRIDGE_DYNAMIC_LENGTH')
    return raw[offset+32:end]


def decode(chain,log):
    address=_address(log['address']);topics=log['topics'];data=_data(log['data'])
    if log.get('removed') is True:raise ValueError('REMOVED_BRIDGE_LOG')
    for t in topics:_fixed(t,32,'topic')
    if chain in ROUTES and address==ARBSYS and topics and topics[0].lower()==SEND:
        if len(topics)!=4 or len(data)<224:raise ValueError('BRIDGE_SEND_ABI')
        w=words(log['data']);payload=dynamic(data,w[5],6)
        return dict(child_chain=chain,kind='withdrawal_sent',message_id=str(int(topics[3],16)),
            sender=word_address(w[0]),recipient=_topic_address(topics[1]),value_wei=str(int.from_bytes(w[4],'big')),
            child_block=int.from_bytes(w[1],'big'),parent_block=int.from_bytes(w[2],'big'),
            timestamp=int.from_bytes(w[3],'big'),payload='0x'+payload.hex(),message_hash=topics[2].lower())
    if chain==1:
        for child,route in ROUTES.items():
            if address==route['outbox'] and topics and topics[0].lower()==EXECUTED:
                if len(topics)!=4 or len(data)!=32 or int(topics[3],16)!=0:raise ValueError('BRIDGE_EXEC_ABI')
                return dict(child_chain=child,kind='withdrawal_executed',message_id=str(int.from_bytes(data,'big')),
                    sender=_topic_address(topics[2]),recipient=_topic_address(topics[1]))
            if address==route['inbox'] and topics and topics[0].lower()==INBOX:
                if len(topics)!=2 or len(data)<64:raise ValueError('BRIDGE_INBOX_ABI')
                payload=dynamic(data,data[:32],1)
                return dict(child_chain=child,kind='deposit_message',message_id=str(int(topics[1],16)),
                    payload_sha256=hashlib.sha256(payload).hexdigest(),recipient=None,sender=None)
    from .layerzero import decode as lz_decode
    return lz_decode(chain,log)


def validate_receipt(chain,receipt,header,txhash,log):
    if _uint(receipt.get('status'),'status')!=1:raise ValueError('BRIDGE_FAILED_RECEIPT')
    h=_fixed(header['hash'],32,'blockhash');n=_uint(header['number'],'block')
    if (_fixed(receipt['transactionHash'],32,'txhash')!=txhash.lower()
        or _fixed(receipt['blockHash'],32,'hash')!=h or _uint(receipt['blockNumber'],'block')!=n
        or _fixed(log['blockHash'],32,'hash')!=h or _uint(log['blockNumber'],'block')!=n
        or _fixed(log['transactionHash'],32,'txhash')!=txhash.lower()
        or _uint(log['transactionIndex'],'index')!=_uint(receipt['transactionIndex'],'index')):
        raise ValueError('BRIDGE_RECEIPT_BINDING')
    hashes=[x.get('hash') if isinstance(x,dict) else x for x in header.get('transactions',[])]
    idx=_uint(receipt['transactionIndex'],'index')
    if idx>=len(hashes) or hashes[idx].lower()!=txhash.lower():raise ValueError('BRIDGE_BLOCK_MEMBERSHIP')
    hits=[r for r in receipt.get('logs',[]) if r.get('logIndex')==log.get('logIndex')]
    fields=('address','topics','data','blockHash','blockNumber','transactionHash','transactionIndex','logIndex')
    if len(hits)!=1 or any(hits[0].get(k)!=log.get(k) for k in fields) or hits[0].get('removed') is True:raise ValueError('BRIDGE_LOG_RECEIPT')
    return decode(chain,log)


class Bridge:
    def __init__(self,base,storage):
        self.out=Path(base)/'evidence/crosschain';self.out.mkdir(parents=True,exist_ok=True)
        self.storage=storage;self.clients={c:RPC(c) for c in (1,42161,4663)}
        self.db=sqlite3.connect(self.out/'journal.sqlite3');self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS legs(chain INTEGER,tx TEXT,log_index INTEGER,child INTEGER,kind TEXT,message TEXT,body TEXT,proof TEXT,
          PRIMARY KEY(chain,tx,log_index));
        CREATE INDEX IF NOT EXISTS by_message ON legs(child,message,kind);
        CREATE TABLE IF NOT EXISTS joins(child INTEGER,message TEXT,status TEXT,body TEXT,PRIMARY KEY(child,message));
        CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY,chain INTEGER,start INTEGER,end INTEGER,proof TEXT,at_ms INTEGER);
        CREATE TABLE IF NOT EXISTS cursors(chain INTEGER PRIMARY KEY,head_next INTEGER,history_next INTEGER);
        CREATE TABLE IF NOT EXISTS jobs(chain INTEGER,tx TEXT,log_index INTEGER,state TEXT,attempts INTEGER,body TEXT,
          PRIMARY KEY(chain,tx,log_index));
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
        ''')
    def close(self):self.db.close()
    def request(self,chain,method,params,records):
        rpc=self.clients[chain]
        if not rpc.identity_checked:records.append(rpc.check())
        cache=getattr(self,'receipt_batch_cache',None)
        cacheable=(method=='eth_getTransactionReceipt' or method=='eth_getTransactionByHash'
            or (method=='eth_getBlockByNumber' and params and isinstance(params[0],str) and params[0].startswith('0x')))
        key=(chain,method,canonical(params))
        if cacheable and cache is not None and key in cache:
            value,record=cache[key]
        else:
            value,record=rpc.result(method,params)
            if cacheable and cache is not None:cache[key]=(value,record)
        records.append(record);return value
    def retain(self,records,kind):
        return self.storage.put((canonical(dict(schema='economic-machine.crosschain-rpc.v1',records=records))+'\n').encode(),kind,
            dict(chains=sorted({x['chain_id'] for x in records}),provider_observation=True))
    def admit(self,chain,log,records):
        txhash=_fixed(log['transactionHash'],32,'txhash')
        receipt=self.request(chain,'eth_getTransactionReceipt',[txhash],records)
        header=self.request(chain,'eth_getBlockByNumber',[log['blockNumber'],False],records)
        item=validate_receipt(chain,receipt,header,txhash,log)
        if item is None:return None
        item.update(chain_id=chain,tx_hash=txhash,log_index=_uint(log['logIndex'],'log'),block_hash=header['hash'],
            block_number=_uint(header['number'],'block'),evidence='successful_rpc_receipt_and_block_membership',
            independent_consensus_verified=False)
        return item
    def insert(self,item,proof):
        key=(item['chain_id'],item['tx_hash'],item['log_index']);body=canonical(item)
        old=self.db.execute('SELECT body FROM legs WHERE chain=? AND tx=? AND log_index=?',key).fetchone()
        if old and old[0]!=body:raise SourceIntegrityError('CROSSCHAIN_CANONICAL_RECORD_CHANGED')
        with self.db:self.db.execute('INSERT OR IGNORE INTO legs VALUES(?,?,?,?,?,?,?,?)',
            (*key,item['child_chain'],item['kind'],item['message_id'],body,canonical(proof)))
        if item['kind']!='deposit_message':self.join(item['child_chain'],item['message_id'])
    def join(self,child,message):
        rows=self.db.execute('SELECT body FROM legs WHERE child=? AND message=? AND kind IN (?,?,?,?)',
            (child,message,'withdrawal_sent','withdrawal_executed','layerzero_sent','layerzero_delivered')).fetchall()
        items=[json.loads(r[0]) for r in rows];sent=[i for i in items if i['kind'] in ('withdrawal_sent','layerzero_sent')];done=[i for i in items if i['kind'] in ('withdrawal_executed','layerzero_delivered')]
        status='MISSING_COUNTERPART'
        if len(sent)>1 or len(done)>1:status='CONFLICT'
        elif sent and done:
            lz=sent[0]['kind']=='layerzero_sent'
            fields=('sender','recipient','destination_chain','src_eid','dst_eid','nonce') if lz else ('sender','recipient')
            paired=all(sent[0].get(k)==done[0].get(k) for k in fields) and done[0]['kind']==('layerzero_delivered' if lz else 'withdrawal_executed')
            status=('PAIRED_LAYERZERO_MESSAGE' if lz else 'PAIRED_CANONICAL_MESSAGE') if paired else 'CONFLICT'
        value=dict(child_chain=child,destination_chain=(items[0].get('destination_chain',1) if items else 1),message_id=message,status=status,legs=items,
            same_owner_proven=False,token_amount_equivalence_proven=False,cex_private_ledger=False,
            independent_consensus_verified=False)
        with self.db:self.db.execute('INSERT OR REPLACE INTO joins VALUES(?,?,?,?)',(child,message,status,canonical(value)))
        if status=='CONFLICT':raise SourceIntegrityError('CROSSCHAIN_MESSAGE_CONFLICT')
    def snapshot(self):
        return dict(at_ms=time.time_ns()//1000000,chains=[1,42161,4663],
            receipt_checked_legs=self.db.execute('SELECT count(*) FROM legs').fetchone()[0],
            paired_canonical_messages=self.db.execute("SELECT count(*) FROM joins WHERE status='PAIRED_CANONICAL_MESSAGE'").fetchone()[0],
            missing_counterpart=self.db.execute("SELECT count(*) FROM joins WHERE status='MISSING_COUNTERPART'").fetchone()[0],
            deposits_unresolved=self.db.execute("SELECT count(*) FROM legs WHERE kind='deposit_message'").fetchone()[0],
            pending_receipt_jobs=self.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0],
            query_ranges=[dict(r) for r in self.db.execute('SELECT chain,min(start) first_block,max(end) last_block,count(*) pages FROM queries GROUP BY chain')],
            coverage_is_sparse=True,full_history=False,
            paired_layerzero_messages=self.db.execute("SELECT count(*) FROM joins WHERE status='PAIRED_LAYERZERO_MESSAGE'").fetchone()[0],
            direct_arbitrum_robinhood_message_pairs=sum(1 for r in self.db.execute("SELECT body FROM joins WHERE status='PAIRED_LAYERZERO_MESSAGE'") if {json.loads(r[0])['child_chain'],json.loads(r[0])['destination_chain']}=={42161,4663}),
            unsupported=['LayerZeroV1','CCIP','Across','Relay','canonical_deposit_redemption','Arbitrum_Classic_outboxes'],
            private_exchange_ledger_available=False,independent_consensus_verified=False)
    def publish(self):
        value=self.snapshot();atomic(self.out/'status.json',value);return value
    def follow_execution(self,item,records):
        if item['kind']!='withdrawal_executed':return []
        tx=self.request(1,'eth_getTransactionByHash',[item['tx_hash']],records)
        if not tx or tx.get('blockHash')!=item['block_hash'] or tx.get('hash')!=item['tx_hash']:raise SourceIntegrityError('EXECUTION_TX_BINDING')
        route=ROUTES[item['child_chain']]
        if (tx.get('to','').lower()!=route['outbox'] or not tx.get('input','').startswith(EXECUTE)):
            return [] # Router/internal calls need a trace; preserve missing counterpart.
        raw=_data('0x'+tx['input'][10:])
        if len(raw)<9*32:raise SourceIntegrityError('EXECUTION_CALLDATA')
        w=[raw[i:i+32] for i in range(0,9*32,32)]
        if (str(int.from_bytes(w[1],'big'))!=item['message_id'] or word_address(w[2])!=item['sender']
            or word_address(w[3])!=item['recipient']):raise SourceIntegrityError('EXECUTION_INPUT_EVENT')
        block=int.from_bytes(w[4],'big');chain=item['child_chain']
        logs=self.request(chain,'eth_getLogs',[dict(fromBlock=hex(block),toBlock=hex(block),address=ARBSYS,
            topics=[SEND,None,None,'0x'+int(item['message_id']).to_bytes(32,'big').hex()])],records)
        if len(logs)!=1:return []
        observed=self.admit(chain,logs[0],records)
        if observed is None:return []
        if (observed['child_block']!=block or observed['sender']!=item['sender'] or observed['recipient']!=item['recipient']
            or observed['parent_block']!=int.from_bytes(w[5],'big') or observed['timestamp']!=int.from_bytes(w[6],'big')
            or observed['value_wei']!=str(int.from_bytes(w[7],'big'))):raise SourceIntegrityError('WITHDRAWAL_CALLDATA_MISMATCH')
        offset=int.from_bytes(w[8],'big')
        if offset%32 or offset<9*32 or offset+32>len(raw):raise SourceIntegrityError('EXECUTION_PAYLOAD_OFFSET')
        size=int.from_bytes(raw[offset:offset+32],'big')
        if offset+32+size>len(raw) or '0x'+raw[offset+32:offset+32+size].hex()!=observed['payload']:raise SourceIntegrityError('EXECUTION_PAYLOAD_MISMATCH')
        return [observed]
    def scan(self,chain,*,history=False):
        self.verify_registry(chain)
        records=[];head=self.request(chain,'eth_getBlockByNumber',['finalized',False],records)
        tip=_uint(head['number'],'head');window=512 if chain==1 else 4096
        row=self.db.execute('SELECT * FROM cursors WHERE chain=?',(chain,)).fetchone()
        if row is None:
            start=max(0,tip-window+1);end=tip;history_next=start-1
        elif history:
            end=row['history_next'];start=max(0,end-window+1);history_next=start-1
            if end<0:return
        else:
            start=row['head_next'];end=min(tip,start+window-1);history_next=row['history_next']
            if start>end:return
        if chain==1:
            addresses=[r[k] for r in ROUTES.values() for k in ('inbox','outbox')];topics=[[EXECUTED,INBOX]]
        else:addresses=[ARBSYS];topics=[SEND]
        from .layerzero import REGISTRY,SENT,DELIVERED
        addresses.append(REGISTRY[chain][1]);topics=[([EXECUTED,INBOX,SENT,DELIVERED] if chain==1 else [SEND,SENT,DELIVERED])]
        logs=self.request(chain,'eth_getLogs',[dict(fromBlock=hex(start),toBlock=hex(end),address=addresses,topics=topics)],records)
        if not isinstance(logs,list):raise SourceIntegrityError('BRIDGE_LOGS_SHAPE')
        for log in logs:
            if not start<=_uint(log['blockNumber'],'block')<=end:raise SourceIntegrityError('BRIDGE_QUERY_RANGE')
            if _address(log['address']) not in addresses or log['topics'][0] not in topics[0]:raise SourceIntegrityError('BRIDGE_QUERY_FILTER')
        proof=self.retain(records,'crosschain_query')
        with self.db:
            self.db.execute('INSERT INTO queries(chain,start,end,proof,at_ms) VALUES(?,?,?,?,?)',(chain,start,end,canonical(proof),time.time_ns()//1000000))
            for log in logs:
                if decode(chain,log) is not None:self.db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)',
                    (chain,log['transactionHash'],_uint(log['logIndex'],'log'),'pending',0,canonical(log)))
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?)',
                (chain,(row['head_next'] if history and row else end+1),history_next))
    def process(self,maximum=6,only_chain=None):
        if type(maximum) is not int or not 1<=maximum<=24:raise ValueError('RECEIPT_BATCH_BOUND')
        self.receipt_batch_cache={}
        try:return self._process(maximum,only_chain)
        finally:self.receipt_batch_cache=None
    def _process(self,maximum,only_chain):
        # First receipt jobs are durable before any lookup. Pending errors keep original cursor/log.
        jobs=self.db.execute("SELECT * FROM jobs WHERE state='pending' AND (? IS NULL OR chain=?) ORDER BY attempts,chain,tx LIMIT ?",(only_chain,only_chain,maximum)).fetchall()
        for job in jobs:
            key=(job['chain'],job['tx'],job['log_index']);records=[]
            with self.db:self.db.execute('UPDATE jobs SET attempts=attempts+1 WHERE chain=? AND tx=? AND log_index=?',key)
            item=self.admit(job['chain'],json.loads(job['body']),records)
            linked=self.follow_execution(item,records) if item else []
            proof=self.retain(records,'crosschain_receipt_pair')
            if item:self.insert(item,proof)
            for other in linked:self.insert(other,proof)
            with self.db:self.db.execute("UPDATE jobs SET state='receipt_checked' WHERE chain=? AND tx=? AND log_index=?",key)
    def verify_registry(self,only_chain=None):
        key='layerzero_registry_proof:'+str(only_chain)
        if self.db.execute('SELECT 1 FROM state WHERE key=?',(key,)).fetchone():return
        from .layerzero import REGISTRY
        records=[]
        for chain,(eid,address) in REGISTRY.items():
            if only_chain is not None and chain!=only_chain:continue
            value=self.request(chain,'eth_call',[dict(to=address,data=topic('eid()')[:10]),'finalized'],records)
            if _uint(value,'endpoint eid')!=eid:raise SourceIntegrityError('LAYERZERO_ENDPOINT_EID_MISMATCH')
        proof=self.retain(records,'layerzero_endpoint_identity')
        with self.db:self.db.execute('INSERT INTO state VALUES(?,?)',(key,canonical(proof)))
    def step(self):
        self.verify_registry()
        row=self.db.execute("SELECT value FROM state WHERE key='round'").fetchone();number=int(row[0]) if row else 0
        chain=(1,42161,4663)[number%3];self.scan(chain,history=(number//3)%2==1);self.process()
        with self.db:self.db.execute("INSERT OR REPLACE INTO state VALUES('round',?)",(str(number+1),))
        return self.publish()
