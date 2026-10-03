"""Real native-process boundary, durable restarts and adversarial dual-RPC fixtures."""
import hashlib
import json
import multiprocessing
import os
import subprocess
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
from eth_tester import EthereumTester, PyEVMBackend
from web3 import Web3, EthereumTesterProvider
from economic_machine.values import MachineError
from machine_engine.solution_chain import FinalizedChain, ReadRPC
from machine_engine.solution_operations import Frame, LocalMiner, MiningJournal, NativePipeline
from machine_engine.solution import SolutionLab
from solution_operator import export

ROOT=Path(__file__).resolve().parents[1]
CONTRACT='0x'+'1'*40
MINER='0x'+'2'*40
TX='0x'+'a'*64
CODE='6000'
CODE_SHA=hashlib.sha256(bytes.fromhex(CODE)).hexdigest()


class RPC:
    def __init__(self,name):
        self.identity=name;self.finalized=90;self.latest=104;self.now=int(time.time())
        self.wrong_chain=False;self.wrong_code=False;self.missing_receipt=False;self.status=1;self.reorg=False
        self.corrupt_input=False;self.round_reorg=False;self.changed=False;self.paused=False
    @staticmethod
    def block_hash(height):return '0x'+format(height,'064x')
    def __call__(self,method,params):
        if method=='eth_chainId':return hex(1 if self.wrong_chain else 421614)
        if method=='eth_getBlockByNumber':
            tag=params[0];n=self.finalized if tag=='finalized' else self.latest if tag=='latest' else int(tag,16)
            return {'number':hex(n),'timestamp':hex(self.now-10),'hash':self.block_hash(n+1 if self.changed else n)}
        if method=='eth_getCode':return '0x6001' if self.wrong_code else '0x'+CODE
        if method=='eth_call':
            selector=params[0]['data'][2:10]
            if selector==keccak(text='activeRound()')[:4].hex():return '0x'+encode(['uint256'],[1]).hex()
            if selector==keccak(text='admissionPaused()')[:4].hex():return '0x'+encode(['bool'],[self.paused]).hex()
            if self.round_reorg:self.changed=True
            return '0x'+encode(['uint256','uint256','uint64','uint64','uint64','uint16','uint8'],
                [1,12345,self.now-120,self.now+600,self.now+1200,5500,2]).hex()
        if method=='eth_getTransactionReceipt':
            if self.missing_receipt:return None
            return {'transactionHash':TX,'blockNumber':hex(95),'blockHash':self.block_hash(999 if self.reorg else 95),'status':hex(self.status)}
        if method=='eth_getTransactionByHash':
            return {'hash':TX,'blockHash':self.block_hash(999 if self.reorg else 95),'to':CONTRACT,'from':MINER,
                'input':'0xbad0' if self.corrupt_input else '0x1234','value':'0x0','chainId':hex(421614)}
        raise AssertionError(method)


def chain():return FinalizedChain(RPC('provider-a'),RPC('provider-b'),CONTRACT,CODE_SHA)


class ChainBoundary(unittest.TestCase):
    def test_working_state_is_explicitly_not_finalized(self):
        reader=chain();snapshot=reader.snapshot()
        self.assertEqual(snapshot['anchor']['number'],100);self.assertEqual(snapshot['seed'],12345)
        self.assertEqual(snapshot['assurance'],'DUAL_RPC_RECENT_CANONICAL_NOT_FINALIZED')
        self.assertEqual(reader.anchor()['number'],90)

    def test_provider_hosts_wrong_chain_code_and_disagreement(self):
        with self.assertRaises(MachineError):FinalizedChain(RPC('same'),RPC('same'),CONTRACT,CODE_SHA)
        for attr in ['wrong_code','wrong_chain']:
            reader=chain();setattr(reader.first,attr,True);setattr(reader.second,attr,True)
            with self.subTest(attr=attr),self.assertRaises(MachineError):reader.snapshot()
        reader=chain();reader.first.wrong_chain=True
        with self.assertRaises(MachineError):reader.snapshot()

    def test_reorg_between_eth_call_and_anchor_read_is_rejected(self):
        reader=chain();reader.first.round_reorg=reader.second.round_reorg=True
        with self.assertRaises(MachineError):reader.snapshot()

    def test_unknown_pending_finalized_and_reverted_are_distinct(self):
        intent={'transaction':{'to':CONTRACT,'from':MINER,'data':'0x1234','value':'0x0'}};reader=chain()
        reader.first.missing_receipt=reader.second.missing_receipt=True
        self.assertEqual(reader.outcome(TX,intent)['state'],'UNKNOWN')
        reader.first.missing_receipt=reader.second.missing_receipt=False
        self.assertEqual(reader.outcome(TX,intent)['state'],'PENDING_FINALITY')
        reader.first.status=reader.second.status=0
        self.assertEqual(reader.outcome(TX,intent)['state'],'PENDING_REVERT')
        reader.first.finalized=reader.second.finalized=100
        self.assertEqual(reader.outcome(TX,intent)['state'],'REVERTED')
        reader.first.status=reader.second.status=1
        self.assertEqual(reader.outcome(TX,intent)['state'],'CONFIRMED')

    def test_orphaned_and_foreign_transactions_never_confirm(self):
        reader=chain();intent={'transaction':{'to':CONTRACT,'from':MINER,'data':'0x1234'}}
        reader.first.reorg=reader.second.reorg=True
        self.assertEqual(reader.outcome(TX,intent)['state'],'ORPHANED')
        reader.first.corrupt_input=reader.second.corrupt_input=True
        with self.assertRaises(MachineError):reader.outcome(TX,intent)

    def test_read_only_transport_does_not_accept_send_or_embedded_credentials(self):
        for url in ['http://external.invalid','https://owner:secret@example.org','file:///etc/passwd']:
            with self.subTest(url=url),self.assertRaises(MachineError):ReadRPC(url)
        provider=ReadRPC('https://example.org')
        with self.assertRaises(MachineError):provider('eth_sendRawTransaction',[])
        with self.assertRaises(MachineError):ReadRPC('http://127.0.0.1:8545')
        ReadRPC('http://127.0.0.1:8545',allow_local=True)


def crash_after_handoff(directory,job,intent):
    journal=MiningJournal(directory)
    journal.handoff(intent)
    os._exit(17)


class DurableJournal(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)/'private';self.journal=MiningJournal(self.directory)
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def job(self,binding=None):
        job=self.journal.reserve(binding or {'seed':1},1000,2000,now=86400)
        self.journal.solved(job,{'bits':'42'});return job

    def test_restart_preserves_daily_budget_and_work_identity(self):
        job=self.job();self.journal.close();self.journal=MiningJournal(self.directory)
        self.assertIsNone(self.journal.reserve({'seed':1},1000,2000,now=86400))
        self.job({'seed':2})
        with self.assertRaises(MachineError):self.journal.reserve({'seed':3},1000,2000,now=86400)
        with self.assertRaises(MachineError):self.journal.reserve({'seed':4},1000,2000,now=0)
        self.assertEqual(self.journal.job(job)['state'],'SOLVED')
        self.assertEqual(self.journal.path.stat().st_mode&0o777,0o600)

    def test_atomic_budget_reservation_across_competing_connections(self):
        other=MiningJournal(self.directory)
        try:
            self.job();self.assertIsNone(other.reserve({'seed':1},1000,2000,now=86400))
            second=other.reserve({'seed':2},1000,2000,now=86400)
            self.assertIsNotNone(second)
            with self.assertRaises(MachineError):self.journal.reserve({'seed':3},1000,2000,now=86400)
        finally:other.close()

    def test_crash_after_handoff_never_returns_to_unsigned(self):
        job=self.job();intent=self.journal.prepare(job,'commit',{'transaction':{'data':'0x1234'}})
        worker=multiprocessing.get_context('fork').Process(target=crash_after_handoff,args=(str(self.directory),job,intent))
        worker.start();worker.join(10);self.assertEqual(worker.exitcode,17)
        self.assertEqual(self.journal.intent(intent)['state'],'HANDED_OFF')
        with self.assertRaises(MachineError):self.journal.handoff(intent)

    def test_unknown_orphaned_no_blind_retry_and_reveal_requires_commit(self):
        job=self.job();payload={'transaction':{'to':CONTRACT,'from':MINER,'data':'0x1234'}}
        with self.assertRaises(MachineError):self.journal.prepare(job,'reveal',payload)
        intent=self.journal.prepare(job,'commit',payload);self.journal.handoff(intent);self.journal.record_hash(intent,TX)
        reader=chain();reader.first.missing_receipt=reader.second.missing_receipt=True
        self.assertEqual(self.journal.reconcile(intent,reader)['state'],'UNKNOWN')
        with self.assertRaises(MachineError):self.journal.handoff(intent)
        reader.first.missing_receipt=reader.second.missing_receipt=False;reader.first.reorg=reader.second.reorg=True
        self.assertEqual(self.journal.reconcile(intent,reader)['state'],'ORPHANED')
        with self.assertRaises(MachineError):self.journal.prepare(job,'reveal',payload)
        reader.first.reorg=reader.second.reorg=False
        self.journal.reconcile(intent,reader)
        self.assertIsNotNone(self.journal.prepare(job,'reveal',payload))

    def test_private_path_and_immutable_intent(self):
        job=self.job();intent=self.journal.prepare(job,'commit',{'marker':1})
        self.assertEqual(intent,self.journal.prepare(job,'commit',{'marker':1}))
        with self.assertRaises(MachineError):self.journal.prepare(job,'commit',{'marker':2})
        self.assertNotIn('marker',json.dumps(self.journal.status()))
        self.directory.chmod(0o755)
        with self.assertRaises(MachineError):MiningJournal(self.directory)

    def test_owner_export_is_exclusive_private_and_handoff_precedes_file(self):
        job=self.job();intent=self.journal.prepare(job,'commit',{'private_marker':'owner_only'})
        output=self.directory/'owner-export.json'
        self.assertEqual(export(self.journal,intent,output)['state'],'HANDED_OFF')
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(output.read_text())['private_marker'],'owner_only')
        with self.assertRaises(FileExistsError):export(self.journal,intent,output)
        self.assertEqual(self.journal.intent(intent)['state'],'HANDED_OFF')


class PipelineBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build=json.loads((ROOT/'artifacts/solution-operations/native-build.json').read_text())
    def pipeline(self,**args):return NativePipeline(self.build['executable'],self.build['sha256'],**args)
    @staticmethod
    def jobs(n=16):return [{'seed':12345,'problem':i%16,'budget':100000,'algorithm':'integer_anneal','search_seed':42+i} for i in range(n)]
    def test_real_pipeline_order_parity_backpressure_and_cpu_pinning(self):
        cpus=sorted(os.sched_getaffinity(0))
        for count in [1,16,128]:
            result=self.pipeline(cpus=[cpus[0],cpus[-1]]).search(self.jobs(count))
            self.assertEqual(len(result),count)
            for i,row in enumerate(result):
                self.assertEqual(row['problem'],str(i%16));self.assertFalse(row['global_optimum_proven'])
                self.assertLessEqual(row['edge_visits'],100000)
    def test_hash_pin_capacity_and_malformed_binary_frames(self):
        blocked=subprocess.check_output([self.build['executable'],'--sandbox-self-test'],text=True,timeout=10)
        self.assertEqual(blocked.strip(),'SANDBOX_FILE_NETWORK_DENY_PASS')
        with self.assertRaises(MachineError):NativePipeline(self.build['executable'],'0'*64)
        with self.assertRaises(MachineError):self.pipeline(cpus=[-1,0])
        with self.assertRaises(MachineError):self.pipeline().search(self.jobs(129))
        for raw in [b'partial',bytes(Frame())]:
            run=subprocess.run([self.build['executable']],input=raw,capture_output=True,timeout=10)
            self.assertNotEqual(run.returncode,0)

    def test_stalled_workers_cannot_exhaust_unbounded_process_slots(self):
        ready=threading.Semaphore(0);release=threading.Event();errors=[]
        class Stalled(NativePipeline):
            def _search(self,jobs):
                ready.release()
                if not release.wait(5):raise RuntimeError('Test stall timeout')
                return []
        worker=Stalled(self.build['executable'],self.build['sha256'])
        def work():
            try:worker.search(self.jobs(1))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=work) for _ in range(2)]
        for thread in threads:thread.start()
        try:
            self.assertTrue(ready.acquire(timeout=5));self.assertTrue(ready.acquire(timeout=5))
            with self.assertRaises(MachineError):self.pipeline().search(self.jobs(1))
        finally:
            release.set()
            for thread in threads:thread.join(5)
        self.assertFalse(errors);self.assertEqual(len(self.pipeline().search(self.jobs(1))),1)

    def test_console_solution_lab_uses_the_sandboxed_process(self):
        with patch.dict(os.environ,{'ENGINE_SOLUTION_PIPELINE':self.build['executable'],'ENGINE_SOLUTION_PIPELINE_SHA256':self.build['sha256']}):
            lab=SolutionLab();self.assertEqual(lab.status()['execution_backend'],'SANDBOXED_CPP_PIPELINE')
            raw={'seed':'12345','problem':'0','algorithm':'integer_anneal','budget':'100000','search_seed':'42','bits':None}
            result=lab.calculate(raw)
            self.assertEqual(result['execution_backend'],'SANDBOXED_CPP_PIPELINE')
            self.assertEqual(result['pipeline_sha256'],self.build['sha256'])
            verified=lab.calculate(raw|{'bits':result['bits']})
            self.assertEqual(verified['score'],result['score']);self.assertFalse(verified['global_optimum_proven'])

    def test_local_miner_once_budget_recovery_and_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=MiningJournal(Path(temp)/'miner');reader=chain()
            miner=LocalMiner(journal,reader,self.pipeline(),MINER,daily_limit=1600000)
            now=reader.first.now
            self.assertEqual(miner.tick(now=now)['jobs'],16)
            self.assertEqual(miner.tick(now=now)['jobs'],0)
            job,=journal.db.execute("SELECT id FROM jobs WHERE state='SOLVED' LIMIT 1").fetchone()
            result=miner.prepare(job,'commit',now=now);self.assertEqual(result['state'],'UNSIGNED')
            secret=journal.directory/(job+'.secret.json');self.assertEqual(secret.stat().st_mode&0o777,0o600)
            # Simulate crash after secret fsync and before intent insert, then recover exact commitment.
            original=journal.intent(result['intent_id'])['payload']['commitment']
            journal.db.execute('DELETE FROM intents WHERE id=?',(result['intent_id'],))
            recovered=miner.prepare(job,'commit',now=now)
            self.assertEqual(journal.intent(recovered['intent_id'])['payload']['commitment'],original)
            reader.first.paused=reader.second.paused=True
            self.assertEqual(miner.tick(now=now)['state'],'WAITING_FOR_FRESH_COMMIT_WINDOW')
            journal.close()


class LocalEVMLifecycle(unittest.TestCase):
    def test_cpp_to_private_commit_reveal_claim_and_finalized_reward_readback(self):
        # Disposable EVM fixtures only. Both RPC adapters share this backend; no live VRF/finality claim.
        backend=PyEVMBackend();backend.chain.chain_id=421614
        tester=EthereumTester(backend);w3=Web3(EthereumTesterProvider(tester));owner=w3.eth.accounts[0]
        contracts=json.loads((ROOT/'artifacts/solution/contracts.json').read_text())
        def deploy(name,*args):
            item=contracts[name];factory=w3.eth.contract(abi=item['abi'],bytecode=item['bytecode'])
            receipt=w3.eth.wait_for_transaction_receipt(factory.constructor(*args).transact({'from':owner}))
            self.assertEqual(receipt.status,1)
            return w3.eth.contract(address=receipt.contractAddress,abi=item['abi'])
        vrf=deploy('SolutionVRFMock');contract=deploy('SolutionMining',vrf.address,b'k'*32,1)
        contract.functions.request().transact({'from':owner});vrf.functions.deliver(1,[12345]).transact({'from':owner})
        tester.mine_blocks(8)
        def hexadecimal(value):return '0x'+bytes(value).hex()
        class Adapter:
            def __init__(self,name):self.identity=name;self.omit_mint=False
            def __call__(self,method,params):
                if method=='eth_chainId':return hex(421614)
                if method=='eth_getBlockByNumber':
                    tag=params[0];block=w3.eth.get_block('latest' if tag in {'latest','finalized'} else int(tag,16))
                    return {'number':hex(block.number),'timestamp':hex(block.timestamp),'hash':hexadecimal(block.hash)}
                if method=='eth_getCode':return hexadecimal(w3.eth.get_code(params[0],int(params[1],16)))
                if method=='eth_call':return hexadecimal(w3.eth.call(params[0]|{'from':owner},block_identifier=int(params[1],16)))
                if method=='eth_getTransactionByHash':
                    tx=w3.eth.get_transaction(params[0])
                    return {'hash':hexadecimal(tx.hash),'blockHash':hexadecimal(tx.blockHash),'to':tx.to,'from':tx['from'],
                            'input':hexadecimal(tx.input),'value':hex(tx.value),'chainId':hex(421614)}
                if method=='eth_getTransactionReceipt':
                    receipt=w3.eth.get_transaction_receipt(params[0])
                    return {'transactionHash':hexadecimal(receipt.transactionHash),'blockHash':hexadecimal(receipt.blockHash),
                        'blockNumber':hex(receipt.blockNumber),'status':hex(receipt.status),
                        'logs':[{'address':log.address,'topics':[hexadecimal(t) for t in log.topics],
                                 'data':hexadecimal(log.data)} for log in receipt.logs
                                if not (self.omit_mint and hexadecimal(log.topics[0])=='0x'+keccak(text='Transfer(address,address,uint256)').hex())]}
                raise AssertionError(method)
        reader=FinalizedChain(Adapter('fixture-a'),Adapter('fixture-b'),contract.address,
                              hashlib.sha256(w3.eth.get_code(contract.address)).hexdigest())
        proof=json.loads((ROOT/'artifacts/solution-operations/native-build.json').read_text())
        pipeline=NativePipeline(proof['executable'],proof['sha256'])
        with tempfile.TemporaryDirectory() as temp:
            journal=MiningJournal(Path(temp)/'miner');miner=LocalMiner(journal,reader,pipeline,owner,daily_limit=1600000)
            now=w3.eth.get_block('latest').timestamp;self.assertEqual(miner.tick(now=now)['jobs'],16)
            job,=journal.db.execute("SELECT id FROM jobs WHERE state='SOLVED' LIMIT 1").fetchone()
            def execute(action):
                intent=miner.prepare(job,action,now=w3.eth.get_block('latest').timestamp)
                payload=journal.handoff(intent['intent_id']);tx=payload['transaction']
                # Explicit test-only owner submission. The actual runtime never performs this send.
                tx_hash=w3.eth.send_transaction({'from':owner,'to':tx['to'],'data':tx['data'],'value':0})
                receipt=w3.eth.wait_for_transaction_receipt(tx_hash);self.assertEqual(receipt.status,1)
                journal.record_hash(intent['intent_id'],hexadecimal(tx_hash))
                return journal.reconcile(intent['intent_id'],reader)
            self.assertEqual(execute('commit')['state'],'CONFIRMED')
            commit_end=contract.functions.rounds(1).call()[3];tester.time_travel(commit_end+1);tester.mine_blocks(8)
            self.assertEqual(execute('reveal')['state'],'CONFIRMED')
            reveal_end=contract.functions.rounds(1).call()[4];tester.time_travel(reveal_end+1);tester.mine_blocks(8)
            contract.functions.finalize(1).transact({'from':owner});tester.mine_blocks(8)
            reward=execute('claim');self.assertEqual(reward['state'],'CONFIRMED')
            self.assertEqual(reward['minted_reward'],str(10**18))
            self.assertEqual(reward['reward_state_at_finalized_anchor']['balance'],str(10**18))
            self.assertEqual(reward['reward_state_at_finalized_anchor']['supply'],str(10**18))
            identifier,=journal.db.execute("SELECT id FROM intents WHERE job=? AND action='claim'",(job,)).fetchone()
            reader.first.omit_mint=reader.second.omit_mint=True
            held=journal.reconcile(identifier,reader)
            self.assertEqual(held['state'],'HELD');self.assertEqual(held['reason'],'SOLUTION_CLAIM_MINT_EVENT_REQUIRED')
            self.assertEqual(journal.intent(identifier)['state'],'HELD')
            prior=journal.db.execute('SELECT outcome FROM observations WHERE intent=? ORDER BY id',(identifier,)).fetchall()
            self.assertEqual([json.loads(row[0])['state'] for row in prior],['CONFIRMED','HELD'])
            with self.assertRaises(MachineError):miner.prepare(job,'claim',now=w3.eth.get_block('latest').timestamp)
            journal.close()

    def test_existing_prepared_intent_cannot_bypass_fresh_deadline_check(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=MiningJournal(Path(temp)/'miner');reader=chain()
            build=json.loads((ROOT/'artifacts/solution-operations/native-build.json').read_text())
            miner=LocalMiner(journal,reader,NativePipeline(build['executable'],build['sha256']),MINER,daily_limit=1600000)
            now=reader.first.now;miner.tick(now=now)
            job,=journal.db.execute("SELECT id FROM jobs WHERE state='SOLVED' LIMIT 1").fetchone()
            intent=miner.prepare(job,'commit',now=now)
            self.assertEqual(miner.prepare(job,'commit',now=now)['intent_id'],intent['intent_id'])
            # RPC anchor stays fresh while the commit window approaches its end.
            original=reader.snapshot
            def closing(**kwargs):
                snapshot=original(**kwargs);snapshot['commit_end']=now+30;return snapshot
            reader.snapshot=closing
            with self.assertRaises(MachineError):miner.prepare(job,'commit',now=now)
            journal.close()


if __name__=='__main__':unittest.main()
