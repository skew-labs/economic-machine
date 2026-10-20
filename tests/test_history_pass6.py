import gzip
import hashlib
import http.client
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from economic_machine.pump_research.source_checkpoints import migrate_sources
from economic_machine.pump_research.research_cloud import retire_file, CloudFile
from economic_machine.robinhood_research.rpc_http import PersistentRPC
from economic_machine.arbitrum_research.source import SourceBackoff, SourceIntegrityError, SourceLimit
from economic_machine.robinhood_research.source import PublicSource, RPC, parse_page
from test_pump_research_cloud import FakeCloud
from test_pump_research_spend import setup_budget
from test_robinhood_hash_fetch import Network
from test_robinhood_research import envelope


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); setup_budget(self.base)
        self.cloud = FakeCloud(self.base); self.addCleanup(self.cloud.close)
        self.folder = self.base/'epoch'; (self.folder/'chunks').mkdir(parents=True)
        self.rows = []
        for n in range(33):
            raw = bytes([n])*100
            name = 'part'+str(n); (self.folder/'chunks'/name).write_bytes(raw)
            self.rows.append(dict(path=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
        self.source = dict(chunks=self.rows); self.events = []
        self.patch = patch('economic_machine.pump_research.research_cloud.Cloud',FakeCloud)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def run_migration(self, count=16):
        return migrate_sources(self.folder,909,self.source,{'source':'fixed'},{'import':'fixed'},
            SimpleNamespace(guard=lambda:None),self.cloud,
            lambda state,**body:self.events.append(body),checkpoint_chunks=count)

    def test_batched_catalogues_and_full_cloud_readback(self):
        uploads=[]; original=self.cloud.upload
        def upload(path,expected=None):
            if Path(path).name=='cloud-progress-catalog.json':
                uploads.append(json.loads(Path(path).read_text())['completed_chunks'])
            return original(path,expected)
        with patch.object(self.cloud,'upload',side_effect=upload):self.assertEqual(self.run_migration(),3300)
        self.assertEqual(uploads,[16,32,33])
        self.assertEqual(self.events[14]['durable_source_checkpoint_chunks'],0)
        self.assertEqual(self.events[15]['durable_source_checkpoint_chunks'],16)
        for n,row in enumerate(self.rows):
            reader=CloudFile(self.base,self.folder/'cloud-chunks'/(row['path']+'.json'),row)
            try:self.assertEqual(reader.read(0,100),bytes([n])*100)
            finally:reader.close()
        before=(self.folder/'cloud-progress-catalog.json').read_bytes()
        self.assertEqual(self.run_migration(count=1),0)
        self.assertEqual((self.folder/'cloud-progress-catalog.json').read_bytes(),before)

    def test_catalogue_failure_retires_nothing_in_window(self):
        original=self.cloud.upload
        def fail(path,expected=None):
            if Path(path).name=='cloud-progress-catalog.json':raise RuntimeError('readback failed')
            return original(path,expected)
        with patch.object(self.cloud,'upload',side_effect=fail):
            with self.assertRaises(RuntimeError):self.run_migration()
        self.assertEqual(len(list((self.folder/'chunks').iterdir())),33)
        self.assertEqual(self.run_migration(),3300)

    def test_restart_after_partial_retirement_reuses_receipts(self):
        calls=[]
        def interrupt(*args):
            calls.append(1)
            if len(calls)==2:raise RuntimeError('interrupted')
            return retire_file(*args)
        with patch('economic_machine.pump_research.source_checkpoints.retire_file',side_effect=interrupt):
            with self.assertRaises(RuntimeError):self.run_migration()
        self.assertEqual(len(list((self.folder/'chunks').iterdir())),32)
        self.assertEqual(self.run_migration(),3200)

    def test_bounds_and_mutated_source_keep_originals(self):
        for n in (0,33,True,'16'):
            with self.assertRaises(ValueError):self.run_migration(n)
        (self.folder/'chunks/part5').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.run_migration()
        self.assertEqual(len(list((self.folder/'chunks').iterdir())),33)


class Response(io.BytesIO):
    def __init__(self,raw,status=200,encoding='gzip',close=False,retry='900'):
        super().__init__(raw); self.status=status; self.will_close=close
        self.headers={'Content-Encoding':encoding,'Retry-After':retry}

class Connection:
    def __init__(self,responses):self.responses=list(responses); self.requests=[]; self.closed=False
    def request(self,*args,**kwargs):self.requests.append((args,kwargs))
    def getresponse(self):
        response=self.responses.pop(0)
        if isinstance(response,Exception):raise response
        return response
    def close(self):self.closed=True

class HTTPTests(unittest.TestCase):
    def test_reuses_connection_and_validates_gzip(self):
        c=Connection([Response(gzip.compress(b'first')),Response(b'second',encoding='identity')]); created=[]
        def factory(*a,**kw):created.append(1);return c
        client=PersistentRPC('https://example.test/rpc',factory=factory)
        self.assertEqual(client.post(b'{}',1024)[0],b'first')
        self.assertEqual(client.post(b'{}',1024)[0],b'second')
        self.assertEqual(len(created),1);self.assertEqual(len(c.requests),2)
        self.assertEqual(c.requests[0][1]['headers']['Accept-Encoding'],'gzip');client.close()
        self.assertTrue(c.closed)

    def test_provider_backoff_closes_without_retry(self):
        c=Connection([Response(b'',status=429)]);client=PersistentRPC('https://example.test',factory=lambda *a,**k:c)
        with self.assertRaises(SourceBackoff) as e:client.post(b'{}',1024)
        self.assertEqual(e.exception.retry_after_seconds,900);self.assertTrue(c.closed)
        self.assertEqual(len(c.requests),1)

    def test_malformed_truncated_and_bomb_close(self):
        data=gzip.compress(b'hello');bad=bytearray(data);bad[-5]^=1
        for raw in (bytes(bad),data[:-1],data+data,gzip.compress(b'x'*10000)):
            c=Connection([Response(raw)]);client=PersistentRPC('https://example.test',factory=lambda *a,**k:c)
            with self.assertRaises((SourceIntegrityError,SourceLimit)):client.post(b'{}',1024)
            self.assertTrue(c.closed)

    def test_connection_loss_is_a_visible_no_retry_backoff(self):
        c=Connection([http.client.RemoteDisconnected('closed')]);client=PersistentRPC('https://example.test',factory=lambda *a,**k:c)
        with self.assertRaises(SourceBackoff):client.post(b'{}',1024)
        self.assertEqual(len(c.requests),1);self.assertTrue(c.closed)

    def test_shared_gate_eliminates_only_the_second_idle_sleep(self):
        rpc=RPC();rpc.transport=SimpleNamespace(post=lambda *a:(b'{"result":1}',{}))
        with patch.dict('os.environ',{'EM_RPC_GATE_ROOT':'active'}),patch('economic_machine.robinhood_research.source.time.sleep') as sleep:
            self.assertEqual(rpc._exchange({}),{'result':1});sleep.assert_not_called()


class GroupTests(unittest.TestCase):
    def fetch(self,group,network=None):
        network=network or Network();source=PublicSource(hash_bound=True,page_blocks=64)
        source.identity=envelope('eth_chainId',[],'0x1237')
        with patch.dict('os.environ',{'EM_RPC_HEADER_GROUP':str(group)}),patch.object(source.rpc,'exchange',side_effect=network.exchange):
            return source.fetch(1,64),network

    def test_same_64_blocks_in_12_batches_and_no_larger_rpc_batch(self):
        old,a=self.fetch(8);new,b=self.fetch(16)
        self.assertEqual(old['blocks'],new['blocks']);self.assertEqual((len(a.calls),len(b.calls)),(16,12))
        self.assertEqual(sum(map(len,b.calls)),192);self.assertLessEqual(max(map(len,b.calls)),16)
        self.assertEqual(parse_page(new['raw'],1,64)['blocks'],old['blocks'])

    def test_partial_group_is_not_advanced_after_backoff(self):
        net=Network();net.fail_at_batch=5
        page,_=self.fetch(16,net)
        self.assertEqual((page['last_block'],page['retry_after_seconds']),(16,180))
        self.assertEqual(len(parse_page(page['raw'],1,64)['blocks']),16)

    def test_oversized_group_is_rejected(self):
        with self.assertRaises(ValueError):self.fetch(32)
