"""Real compiled EVM + C++ parity, emission caps, randomness failure and attack simulations."""
import ctypes as c
import json
import tempfile
import unittest
from pathlib import Path

from eth_abi import encode
from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from fastapi.testclient import TestClient
from web3 import Web3, EthereumTesterProvider

from economic_machine.values import MachineError
from machine_engine.api import create_engine_app
from machine_engine.solution import SolutionLab, Graph, Edge, Answer, graph
from machine_engine.solution_client import seal, reveal
from machine_engine.workspace import Workspace

ROOT=Path(__file__).resolve().parents[1]


def request(seed='12345',problem='0',**changes):
    return {'seed':seed,'problem':problem,'algorithm':'integer_anneal','budget':'100000','search_seed':'42','bits':None}|changes


class SolutionContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.artifacts=json.loads((ROOT/'artifacts/solution/contracts.json').read_text())

    def setUp(self):
        self.tester=EthereumTester(PyEVMBackend());self.w3=Web3(EthereumTesterProvider(self.tester));self.a,self.b=self.w3.eth.accounts[:2]
        self.coordinator=self.deploy('SolutionVRFMock');self.machine=self.deploy('SolutionMining',self.coordinator.address,b'k'*32,1)
        self.token=self.w3.eth.contract(address=self.machine.functions.rewardToken().call(),abi=self.artifacts['SolutionResearchToken']['abi'])
        self.send(self.machine.functions.request(),self.a);self.rid=1

    def deploy(self,name,*args):
        a=self.artifacts[name];factory=self.w3.eth.contract(abi=a['abi'],bytecode=a['bytecode'])
        r=self.w3.eth.wait_for_transaction_receipt(factory.constructor(*args).transact({'from':self.a}));self.assertEqual(r.status,1)
        return self.w3.eth.contract(address=r.contractAddress,abi=a['abi'])

    def send(self,call,who):
        r=self.w3.eth.wait_for_transaction_receipt(call.transact({'from':who}));self.assertEqual(r.status,1);return r

    def start(self,seed=12345):self.send(self.coordinator.functions.deliver(self.machine.functions.rounds(self.rid).call()[0],[seed]),self.a)

    def time(self,index):
        self.tester.time_travel(self.machine.functions.rounds(self.rid).call()[index]);self.tester.mine_blocks(1)

    def commit(self,p,bits,salt,who):
        value=self.machine.functions.commitmentFor(self.rid,p,who,bits,salt).call();self.send(self.machine.functions.commit(self.rid,p,value),who)

    def test_cpp_graph_score_parity_for_all16_problems(self):
        self.start();lab=SolutionLab()
        for p in range(16):
            result=lab.calculate(request(problem=str(p)));score,total=self.machine.functions.score(1,p,int(result['bits'])).call()
            self.assertEqual(score,int(result['score']));self.assertEqual(total,int(result['total_weight']))
            edges=graph(12345,p)
            for i in [0,247,495]:self.assertEqual(edges[i][2],self.machine.functions.weight(12345,p,i).call())

    def test_local_evm_emission_only_after_finalized_winner_once(self):
        self.start();bits=int(SolutionLab().calculate(request())['bits']);salt=b'a'*32
        self.commit(0,bits,salt,self.a)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.claim(1,0),self.a)
        self.time(3);self.send(self.machine.functions.reveal(1,0,bits,salt),self.a)
        self.time(4);self.send(self.machine.functions.finalize(1),self.b)
        self.assertEqual(self.token.functions.totalSupply().call(),0)
        self.send(self.machine.functions.claim(1,0),self.a)
        self.assertEqual(self.token.functions.totalSupply().call(),10**18);self.assertEqual(self.token.functions.balanceOf(self.a).call(),10**18)
        for who in [self.a,self.b]:
            with self.assertRaises(TransactionFailed):self.send(self.machine.functions.claim(1,0),who)
        with self.assertRaises(TransactionFailed):self.send(self.token.functions.mint(self.a,10**18),self.a)

    def test_sender_round_problem_binding_and_reveal_order_tie(self):
        self.start();bits=int(SolutionLab().calculate(request())['bits']);salt=b'b'*32
        value=self.machine.functions.commitmentFor(1,0,self.a,bits,salt).call()
        self.send(self.machine.functions.commit(1,0,value),self.a);self.send(self.machine.functions.commit(1,0,value),self.b)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.reveal(1,0,bits,salt),self.a)
        self.time(3)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.reveal(1,0,bits,salt),self.b)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.reveal(1,1,bits,salt),self.a)
        self.send(self.machine.functions.reveal(1,0,bits,salt),self.a)
        self.assertEqual(self.machine.functions.best(1,0).call()[0],self.a)

    def test_duplicate_canonical_solutions_one_reward_earlier_commit_wins(self):
        self.start();bits=int(SolutionLab().calculate(request())['bits']);salt=b'c'*32
        self.commit(0,bits,salt,self.a);self.commit(0,bits,salt,self.b);self.time(3)
        self.send(self.machine.functions.reveal(1,0,bits,salt),self.b);self.send(self.machine.functions.reveal(1,0,bits,salt),self.a)
        self.assertEqual(self.machine.functions.best(1,0).call()[0],self.a)
        with self.assertRaises(TransactionFailed):self.machine.functions.score(1,0,bits^((1<<32)-1)).call()

    def test_no_valid_answers_no_emission_and_bounded_difficulty(self):
        self.start();threshold=self.machine.functions.rounds(1).call()[5];self.time(4);self.send(self.machine.functions.finalize(1),self.a)
        self.assertEqual(self.machine.functions.nextThreshold().call(),threshold-100)
        self.assertEqual(self.token.functions.totalSupply().call(),0)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.claim(1,0),self.a)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.finalize(1),self.a)

    def test_all16_qualified_round_emission_cap_and_future_threshold_only(self):
        self.start();lab=SolutionLab();salt=b'd'*32;solutions=[]
        for p in range(16):
            bits=int(lab.calculate(request(problem=str(p)))['bits']);solutions.append(bits);self.commit(p,bits,salt,self.a)
        original=self.machine.functions.rounds(1).call()[5];self.time(3)
        for p,bits in enumerate(solutions):self.send(self.machine.functions.reveal(1,p,bits,salt),self.a)
        self.time(4);self.send(self.machine.functions.finalize(1),self.a)
        for p in range(16):self.send(self.machine.functions.claim(1,p),self.a)
        self.assertEqual(self.token.functions.totalSupply().call(),16*10**18)
        self.assertEqual(self.machine.functions.nextThreshold().call(),original+100)
        self.assertEqual(self.machine.functions.rounds(1).call()[5],original)
        self.assertEqual(self.token.functions.cap().call(),10000*16*10**18)

    def test_vrf_spoof_timeout_and_late_callback_cannot_restart(self):
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.rawFulfillRandomWords(1,[123]),self.a)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.commit(1,0,b'x'*32),self.a)
        at=self.machine.functions.rounds(1).call()[2];self.tester.time_travel(at+3600);self.tester.mine_blocks(1)
        self.send(self.machine.functions.abort(1),self.b);self.start()
        self.assertEqual(self.machine.functions.rounds(1).call()[6],4)
        self.assertEqual(self.token.functions.totalSupply().call(),0)
        self.assertEqual(self.machine.functions.nextThreshold().call(),5500)

    def test_duplicate_randomness_and_pending_round_no_reroll(self):
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.request(),self.a)
        self.start();self.time(4);self.send(self.machine.functions.finalize(1),self.a)
        at=self.machine.functions.rounds(1).call()[2];self.tester.time_travel(at+1800);self.tester.mine_blocks(1)
        self.send(self.machine.functions.request(),self.a);self.rid=2;self.start()
        self.assertEqual(self.machine.functions.rounds(2).call()[6],4)

    def test_commitment_python_abi_and_immutable_seed_callback(self):
        self.start();before=self.machine.functions.rounds(1).call();self.send(self.coordinator.functions.deliver(1,[6789]),self.a)
        self.assertEqual(before,self.machine.functions.rounds(1).call())
        bits=42;salt=b'e'*32
        expected=self.w3.keccak(encode(['address','uint256','uint256','uint8','address','uint32','bytes32'],[self.machine.address,self.w3.eth.chain_id,1,0,self.a,bits,salt]))
        self.assertEqual(expected,self.machine.functions.commitmentFor(1,0,self.a,bits,salt).call())

    def test_withholding_cannot_lower_below_floor_or_mint_any_tokens(self):
        thresholds=[]
        for i in range(8):
            self.start(seed=200+i);thresholds.append(self.machine.functions.rounds(self.rid).call()[5])
            self.time(4);self.send(self.machine.functions.finalize(self.rid),self.a)
            if i<7:
                at=self.machine.functions.rounds(self.rid).call()[2];self.tester.time_travel(at+1800);self.tester.mine_blocks(1)
                self.send(self.machine.functions.request(),self.a);self.rid+=1
        self.assertEqual(self.machine.functions.nextThreshold().call(),5200)
        self.assertTrue(all(a-b<=100 for a,b in zip(thresholds,thresholds[1:])))
        self.assertEqual(self.token.functions.totalSupply().call(),0)

    def test_admission_saturation_is_bounded_but_remains_research_dos_risk(self):
        self.start()
        # Simulate 64 independent wallets. Fees/identity/bonds are not modeled as Sybil resistance.
        for i in range(64):
            private=(1000+i).to_bytes(32,'big').hex();address=self.tester.add_account(private)
            self.w3.eth.send_transaction({'from':self.a,'to':address,'value':10**17})
            self.send(self.machine.functions.commit(1,0,(i+1).to_bytes(32,'big')),address)
        self.assertEqual(self.machine.functions.counts(1,0).call(),64)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.commit(1,0,b'y'*32),self.a)
        self.time(4);self.send(self.machine.functions.finalize(1),self.a)
        self.assertEqual(self.token.functions.totalSupply().call(),0)


class SolutionNative(unittest.TestCase):
    def test_random_and_anneal_same_budget_and_exact_rescore(self):
        lab=SolutionLab()
        for algorithm in ['random','integer_anneal']:
            result=lab.calculate(request(algorithm=algorithm,budget='50000'))
            self.assertLessEqual(result['edge_visits'],50000);self.assertFalse(result['global_optimum_proven'])
            self.assertEqual(lab.calculate(request(bits=result['bits']))['score'],result['score'])
            self.assertEqual(result['confirmed_reward'],'0');self.assertIsNone(result['chain_transaction'])

    def test_exact_small_graph_and_canonical_duplicate(self):
        lab=SolutionLab();g=Graph();g.nodes=3;g.count=3
        for i,e in enumerate([(0,1,1),(0,2,1),(1,2,1)]):g.edges[i]=Edge(*e)
        out=Answer();self.assertEqual(lab.library.solution_search(c.byref(g),42,1000,2,c.byref(out)),0)
        self.assertEqual(out.score,2);self.assertTrue(out.complete);self.assertEqual(out.bits,2)

    def test_fixed_batch_matches_scalar_and_rejects_noncanonical_candidates(self):
        lab=SolutionLab();g=Graph();g.nodes=32;edges=graph(12345,0);g.count=len(edges)
        for i,e in enumerate(edges):g.edges[i]=Edge(*e)
        candidates=(c.c_uint64*4)(0,42,84,1);results=(Answer*4)()
        self.assertEqual(lab.library.solution_score_batch(c.byref(g),candidates,4,results),0)
        for i in range(3):
            one=Answer();self.assertEqual(lab.library.solution_score(c.byref(g),candidates[i],c.byref(one)),0)
            self.assertEqual(results[i].score,one.score)
        self.assertFalse(results[3].valid)
        self.assertNotEqual(lab.library.solution_score_batch(c.byref(g),candidates,65,results),0)

    def test_malformed_bounds_and_research_only_api(self):
        lab=SolutionLab()
        for key,value in [('seed',True),('seed','01'),('seed',str(2**256)),('problem','16'),('budget','10000001'),('bits','1'),('algorithm',[])]:
            with self.subTest(key=key,value=value),self.assertRaises(MachineError):lab.calculate(request(**{key:value}))
        with tempfile.TemporaryDirectory() as temp:
            work=Workspace(Path(temp)/'workspace.db');app=create_engine_app(work.runtime.db_path if hasattr(work.runtime,'db_path') else Path(temp)/'workspace.db',workspace=work,admin_token='q'*48)
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/engine/mining/solution/evaluate',json=request()).status_code,401)
                response=client.post('/api/engine/mining/solution/evaluate',json=request(),headers={'Authorization':'Bearer '+'q'*48})
                self.assertEqual(response.status_code,200);self.assertEqual(response.json()['confirmed_reward'],'0')

    def test_private_seal_restart_reveal_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'salt.json';out=seal(request(),contract='0x'+'1'*40,round_id=1,miner='0x'+'2'*40,secret_file=path)
            self.assertNotIn('salt',out);self.assertFalse(out['chain_round_verified']);self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):seal(request(),contract='0x'+'1'*40,round_id=1,miner='0x'+'2'*40,secret_file=path)
            self.assertEqual(reveal(path)['status'],'UNSIGNED_OFFLINE')
            raw=json.loads(path.read_text());raw['problem']=1;path.write_text(json.dumps(raw))
            with self.assertRaises(MachineError):reveal(path)
