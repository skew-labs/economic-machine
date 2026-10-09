"""Persistent RPC transport and one account-stream publisher. Endpoints never logged."""
import asyncio,base64,fcntl,json,mmap,os,struct,time
from pathlib import Path
import aiohttp
from devnet import RPC
from wire import SIZE,MAGIC,SEATS,snapshot
from feed_batch import INPUT_BYTES
from account_codec import decode_account
class InjectedSocketOutage(RuntimeError):pass

def socket_fault_until(store,mandate):
    seconds=mandate.get('feed_retry_fault_seconds',0)
    if type(seconds) is not int or not 0<=seconds<=30 or (seconds and not mandate['fault_drill']):raise RuntimeError('invalid bounded feed fault')
    row=store.db.execute("SELECT body FROM event WHERE role=-1 AND kind='feed_disconnect_injected' ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row['body']).get('retry_fault_until_unix',0) if row else 0

class Net:
    def __init__(self):self.session=None;self.request_id=0
    async def __aenter__(self):
        self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8,connect=3),connector=aiohttp.TCPConnector(limit=8,keepalive_timeout=60));return self
    async def __aexit__(self,*args):await self.session.close()
    async def call(self,method,params):
        self.request_id+=1
        try:
            async with self.session.post(RPC,json={'jsonrpc':'2.0','id':self.request_id,'method':method,'params':params},allow_redirects=False) as response:
                if response.status!=200:raise RuntimeError(f'RPC HTTP {response.status}')
                body=await response.json()
        except (aiohttp.ClientError,asyncio.TimeoutError):raise RuntimeError('RPC transport unavailable') from None
        if 'error' in body:raise RuntimeError(f'RPC {method} code {body["error"].get("code")}')
        return body['result']
    async def market(self,pub,commitment='confirmed',minimum=0):
        start=time.monotonic_ns();r=await self.call('getAccountInfo',[str(pub),{'encoding':'base64+zstd','commitment':commitment,'minContextSlot':minimum}]);v=r['value']
        if v is None:raise RuntimeError('missing market')
        return decode_account(v['data']),r['context']['slot'],start,v['owner']
class Cache:
    SIZE=64+SIZE+SEATS*INPUT_BYTES
    def __init__(self,path,writer=False):
        self.file=open(path,('r+b' if path.exists() else 'w+b') if writer else 'rb')
        if writer and os.fstat(self.file.fileno()).st_size!=self.SIZE:self.file.truncate(self.SIZE)
        self.map=mmap.mmap(self.file.fileno(),self.SIZE,access=mmap.ACCESS_WRITE if writer else mmap.ACCESS_READ);self.writer=writer
        if writer:
            prior=struct.unpack_from('<Q',self.map,0)[0];struct.pack_into('<Q',self.map,0,prior+(1 if prior%2==0 else 2))
    def publish(self,data,slot,received,prepared=None,prepared_wall=0):
        if len(data)!=SIZE:raise RuntimeError('invalid market length')
        if prepared is not None and len(prepared)!=SEATS*INPUT_BYTES:raise RuntimeError('invalid prepared length')
        old,oldslot=struct.unpack_from('<QQ',self.map,0)
        if slot<oldslot:return False
        generation=old+(1 if old%2==0 else 2)
        struct.pack_into('<Q',self.map,0,generation)
        self.map[64:64+SIZE]=data;self.map[64+SIZE:]=prepared if prepared is not None else bytes(SEATS*INPUT_BYTES)
        struct.pack_into('<QQQQ',self.map,8,slot,received,time.time_ns(),prepared_wall)
        struct.pack_into('<Q',self.map,0,generation+1);return True
    def read(self,max_age_ns=3_000_000_000):
        for _ in range(3):
            generation,slot,received,wall=struct.unpack_from('<QQQQ',self.map,0)
            if not generation or generation%2:continue
            data=self.map[64:64+SIZE]
            if generation!=struct.unpack_from('<Q',self.map,0)[0]:continue
            if not 0<=time.monotonic_ns()-received<=max_age_ns:raise RuntimeError('market stream stale')
            st=snapshot(data);st['read_slot']=slot;return st,slot,received,generation
        raise RuntimeError('incomplete market cache')
    def read_if_changed(self,prior):
        generation,slot,received=struct.unpack_from('<QQQ',self.map,0)
        if not generation or generation%2:raise RuntimeError('incomplete market cache')
        if not 0<=time.monotonic_ns()-received<=3_000_000_000:raise RuntimeError('market stream stale')
        return None if generation==prior else self.read()
    def read_binary_if_changed(self,prior):
        for _ in range(3):
            generation,slot,received=struct.unpack_from('<QQQ',self.map,0)
            if not generation or generation%2:continue
            if not 0<=time.monotonic_ns()-received<=3_000_000_000:raise RuntimeError('market stream stale')
            if generation==prior:return None
            data=self.map[64:64+SIZE]
            if generation==struct.unpack_from('<Q',self.map,0)[0]:return data,slot,received,generation
        raise RuntimeError('incomplete market cache')
    def read_prepared_if_changed(self,prior):
        for _ in range(3):
            generation,slot,received,_,wall=struct.unpack_from('<QQQQQ',self.map,0)
            if not generation or generation%2:continue
            if not 0<=time.monotonic_ns()-received<=3_000_000_000:raise RuntimeError('market stream stale')
            if generation==prior:return None
            data=self.map[64:64+SIZE];prepared=self.map[64+SIZE:]
            if generation==struct.unpack_from('<Q',self.map,0)[0]:return data,slot,received,generation,prepared,wall
        raise RuntimeError('incomplete market cache')
    def close(self):self.map.close();self.file.close()
async def publish_feed(folder,private,manifest,mandate,store):
    lock=(private/'feed.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    cache=Cache(private/'market.cache',True);market=manifest['market'];last_slot=0;backoff=0.5
    fault_until=socket_fault_until(store,mandate)
    from feed_batch import Batch
    batch=Batch() if mandate.get('compact_feed',False) else None
    def publish(data,slot,received,owner):
        if owner!=manifest['program'] or data[:8]!=MAGIC or data[216]!=1:raise RuntimeError('market stream identity')
        wall=int(time.time());prepared=batch.prepare(data,slot,received,time.monotonic_ns(),wall) if batch else None
        return cache.publish(data,slot,received,prepared,wall if batch else 0)
    try:
        async with Net() as net:
            while time.time()<mandate['ends_unix'] and not (private/'stop').exists():
                try:
                    if time.time()<fault_until:
                        store.event(-1,'feed_retry_rejected_injected',{'until_unix':fault_until})
                        raise InjectedSocketOutage('bounded socket-only fault')
                    async with net.session.ws_connect(RPC.replace('https://','wss://',1),heartbeat=15,receive_timeout=5,max_msg_size=1_000_000) as ws:
                        await ws.send_json({'jsonrpc':'2.0','id':1,'method':'accountSubscribe','params':[market,{'encoding':'base64+zstd','commitment':'confirmed'}]})
                        ack=await ws.receive_json(timeout=5)
                        if 'result' not in ack:raise RuntimeError('account subscription rejected')
                        store.event(-1,'feed_connected',{'slot':last_slot,'monotonic_ns':time.monotonic_ns()});backoff=0.5
                        data,slot,received,owner=await net.market(market,minimum=last_slot);publish(data,slot,received,owner);last_slot=max(last_slot,slot)
                        while time.time()<mandate['ends_unix'] and not (private/'stop').exists():
                            if mandate['fault_drill'] and time.time()>mandate['starts_unix']+90 and not store.count_events(-1,'feed_disconnect_injected'):
                                fault_until=time.time()+5+mandate.get('feed_retry_fault_seconds',0)
                                store.event(-1,'feed_disconnect_injected',{'retry_fault_until_unix':fault_until,'monotonic_ns':time.monotonic_ns()});await ws.close();await asyncio.sleep(5);break
                            try:message=await ws.receive_json(timeout=1.5)
                            except asyncio.TimeoutError:
                                data,slot,received,owner=await net.market(market,minimum=last_slot);publish(data,slot,received,owner);last_slot=max(last_slot,slot);continue
                            if message.get('method')!='accountNotification':continue
                            result=message['params']['result'];v=result['value'];slot=result['context']['slot'];received=time.monotonic_ns()
                            publish(decode_account(v['data']),slot,received,v['owner']);last_slot=max(last_slot,slot)
                except Exception as error:
                    store.event(-1,'feed_disconnected',{'error_type':type(error).__name__,'http_status':getattr(error,'status',None)})
                    # Same approved endpoint, same account identity. A rejected
                    # WebSocket handshake does not discard usable HTTP snapshots.
                    retry_at=time.monotonic()+backoff;recovered=False
                    while time.monotonic()<retry_at and time.time()<mandate['ends_unix'] and not (private/'stop').exists():
                        try:
                            data,slot,received,owner=await net.market(market,minimum=last_slot)
                            if publish(data,slot,received,owner):
                                last_slot=max(last_slot,slot)
                                if not recovered:store.event(-1,'feed_http_recovered',{'slot':slot,'source_ns':received,'monotonic_ns':time.monotonic_ns()});recovered=True
                        except RuntimeError:store.event(-1,'feed_http_unavailable',{})
                        await asyncio.sleep(min(1,max(0,retry_at-time.monotonic())))
                    backoff=min(30,backoff*2)
    finally:cache.close()
