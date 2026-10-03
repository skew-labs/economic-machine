"""Frozen math parity, escrow accounting, hostile inputs, recovery and authority boundaries."""
import copy
import itertools
import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from fastapi.testclient import TestClient
from web3 import EthereumTesterProvider, Web3

from economic_machine.journal import verify_journal
from economic_machine.values import MachineError
from machine_engine.api import create_engine_app
from machine_engine.mining import EDGE_FIELDS, NativeMining, example, normalize
from machine_engine.mining_client import commitment, reveal, seal
from machine_engine.workspace import Workspace
from machine_commerce.api import create_app

ROOT = Path(__file__).resolve().parents[1]


class MiningContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = json.loads((ROOT / 'artifacts/mining/contracts.json').read_text())

    def setUp(self):
        self.tester = EthereumTester(PyEVMBackend()); self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.owner, self.a, self.b = self.w3.eth.accounts[:3]
        self.token = self.deploy('TestCommerceToken'); self.machine = self.deploy('MachineMining', self.token.address)
        for who in [self.owner, self.a, self.b]:
            self.send(self.token.functions.mint(who, 1000000), self.owner)
            self.send(self.token.functions.approve(self.machine.address, 1000000), who)
        self.native = NativeMining(); self.raw = example(); self.jid = self.create(self.raw)

    def deploy(self, name, *args):
        artifact = self.compiled[name]
        factory = self.w3.eth.contract(abi=artifact['abi'], bytecode=artifact['bytecode'])
        receipt = self.w3.eth.wait_for_transaction_receipt(factory.constructor(*args).transact({'from': self.owner}))
        self.assertEqual(receipt.status, 1)
        return self.w3.eth.contract(address=receipt.contractAddress, abi=artifact['abi'])

    def send(self, call, who):
        receipt = self.w3.eth.wait_for_transaction_receipt(call.transact({'from': who}))
        self.assertEqual(receipt.status, 1); return receipt

    def create(self, raw):
        c = raw['constraints']; at = self.w3.eth.get_block('latest')['timestamp']
        terms = tuple(int(c[k]) for k in ['asset_in','asset_out','amount_in','minimum_net','maximum_cost','maximum_impact_bps','maximum_hops'])
        terms += (at + 300, at + 600, int(raw['reward_units']), int(raw['bond_units']), bytes.fromhex(raw['source_root'][2:]))
        edges = [tuple(int(e[k]) for k in EDGE_FIELDS) for e in raw['edges']]
        jid = self.machine.functions.nextJob().call()
        self.send(self.machine.functions.createJob(terms, edges), self.owner); return jid

    def clock(self, phase):
        terms = self.machine.functions.job(self.jid).call()[1]
        self.tester.time_travel(terms[7 if phase == 'reveal' else 8]); self.tester.mine_blocks(1)

    def commit(self, who, path, salt):
        fingerprint = self.machine.functions.commitmentFor(self.jid, who, path, salt).call()
        self.send(self.machine.functions.commit(self.jid, fingerprint), who)

    def test_native_and_solidity_rounding_and_score_match_all_sample_paths(self):
        for length in range(1, 5):
            for path in itertools.product(range(3), repeat=length):
                native = self.native.calculate(self.raw, path=list(path))
                try:
                    score = self.machine.functions.score(self.jid, path).call()
                except TransactionFailed:
                    self.assertFalse(native['valid'], path)
                else:
                    self.assertTrue(native['valid'], path)
                    self.assertEqual(list(map(int, [native['net_output'], native['gross_output'], native['cost']])), list(score))

    def test_funded_reward_bond_refund_and_actual_exact_token_withdrawal(self):
        path = self.native.calculate(self.raw)['path']; salt = b'a' * 32
        self.commit(self.a, path, salt); self.clock('reveal')
        self.send(self.machine.functions.reveal(self.jid, path, salt), self.a)
        self.assertEqual(self.machine.functions.credits(self.a).call(), 1000)
        self.clock('finalize'); self.send(self.machine.functions.finalize(self.jid), self.b)
        self.assertEqual(self.machine.functions.credits(self.a).call(), 11000)
        before = self.token.functions.balanceOf(self.a).call()
        self.send(self.machine.functions.withdraw(), self.a)
        self.assertEqual(self.token.functions.balanceOf(self.a).call() - before, 11000)
        self.assertEqual(self.machine.functions.liability().call(), 0)
        self.assertEqual(self.token.functions.balanceOf(self.machine.address).call(), 0)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.withdraw(), self.a)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.finalize(self.jid), self.a)

    def test_tie_uses_earlier_commit_not_earlier_reveal_duplicate_no_extra_reward(self):
        path = [1, 2]; salt = b'b' * 32
        self.commit(self.a, path, salt); self.commit(self.b, path, salt); self.clock('reveal')
        self.send(self.machine.functions.reveal(self.jid, path, salt), self.b)
        self.send(self.machine.functions.reveal(self.jid, path, salt), self.a)
        self.clock('finalize'); self.send(self.machine.functions.finalize(self.jid), self.owner)
        self.assertEqual(self.machine.functions.job(self.jid).call()[3], self.a)
        self.assertEqual(self.machine.functions.credits(self.a).call(), 11000)
        self.assertEqual(self.machine.functions.credits(self.b).call(), 1000)

    def test_abandoned_and_invalid_reveal_bond_returns_to_requester(self):
        self.commit(self.a, [2], b'c' * 32); self.clock('reveal')
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.reveal(self.jid, [2], b'c' * 32), self.a)
        self.clock('finalize'); self.send(self.machine.functions.finalize(self.jid), self.owner)
        self.assertEqual(self.machine.functions.credits(self.owner).call(), 11000)
        self.assertEqual(self.machine.functions.credits(self.a).call(), 0)

    def test_sender_binding_and_phase_boundaries(self):
        path = [0]; salt = b'd' * 32
        fingerprint = self.machine.functions.commitmentFor(self.jid, self.a, path, salt).call()
        self.send(self.machine.functions.commit(self.jid, fingerprint), self.a)
        self.send(self.machine.functions.commit(self.jid, fingerprint), self.b)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.reveal(self.jid, path, salt), self.a)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.finalize(self.jid), self.a)
        self.clock('reveal')
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.reveal(self.jid, path, salt), self.b)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.commit(self.jid, fingerprint), self.owner)
        self.clock('finalize')
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.reveal(self.jid, path, salt), self.a)

    def test_false_return_and_fee_token_preserve_escrow(self):
        self.send(self.token.functions.setChargeTransferFee(True), self.owner)
        with self.assertRaises(TransactionFailed): self.create(self.raw)
        self.assertEqual(self.machine.functions.nextJob().call(), 2)
        self.assertEqual(self.machine.functions.liability().call(), 10000)
        self.send(self.token.functions.setChargeTransferFee(False), self.owner)
        self.send(self.token.functions.setFailTransfers(True), self.owner)
        with self.assertRaises(TransactionFailed): self.commit(self.a, [0], b'e' * 32)
        self.assertEqual(self.machine.functions.job(self.jid).call()[6], 0)

    def test_withdraw_fee_failure_keeps_credit_recoverable(self):
        self.clock('finalize'); self.send(self.machine.functions.finalize(self.jid), self.owner)
        self.send(self.token.functions.setChargeTransferFee(True), self.owner)
        with self.assertRaises(TransactionFailed): self.send(self.machine.functions.withdraw(), self.owner)
        self.assertEqual(self.machine.functions.credits(self.owner).call(), 10000)
        self.assertEqual(self.machine.functions.liability().call(), 10000)
        self.send(self.token.functions.setChargeTransferFee(False), self.owner)
        self.send(self.machine.functions.withdraw(), self.owner)

    def test_callback_cannot_reenter_funding_or_credit_withdrawal(self):
        self.token = self.deploy('MiningAdversary'); self.machine = self.deploy('MachineMining', self.token.address)
        self.send(self.token.functions.mint(self.owner,100000),self.owner)
        self.send(self.token.functions.approve(self.machine.address,100000),self.owner)
        # If entered during funding, even finalize of an older expired job must be blocked.
        self.jid = self.create(self.raw); self.clock('finalize')
        payload = self.machine.functions.finalize(self.jid)._encode_transaction_data()
        self.send(self.token.functions.hook(self.machine.address,bytes.fromhex(payload[2:])),self.owner)
        self.create(self.raw)
        self.assertTrue(self.token.functions.attempted().call()); self.assertTrue(self.token.functions.blocked().call())
        self.assertFalse(self.machine.functions.job(self.jid).call()[5])
        self.send(self.machine.functions.finalize(self.jid),self.owner)
        self.send(self.machine.functions.withdraw(),self.owner)
        self.assertTrue(self.token.functions.blocked().call()); self.assertEqual(self.machine.functions.liability().call(),10000)

    def test_better_net_wins_and_no_double_bond_refund(self):
        self.commit(self.a,[0],b'g'*32);self.commit(self.b,[1,2],b'h'*32);self.clock('reveal')
        self.send(self.machine.functions.reveal(self.jid,[0],b'g'*32),self.a)
        self.send(self.machine.functions.reveal(self.jid,[1,2],b'h'*32),self.b)
        with self.assertRaises(TransactionFailed):self.send(self.machine.functions.reveal(self.jid,[1,2],b'h'*32),self.b)
        self.clock('finalize');self.send(self.machine.functions.finalize(self.jid),self.owner)
        self.assertEqual(self.machine.functions.credits(self.a).call(),1000)
        self.assertEqual(self.machine.functions.credits(self.b).call(),11000)
        self.assertEqual(self.machine.functions.liability().call(),12000)

    def test_reverse_edge_cannot_reuse_same_physical_pool(self):
        raw=example();raw['edges'] += [dict(zip(EDGE_FIELDS,map(str,(2,2,1,1200000000,1000000000,1000,0)),strict=True))]
        jid=self.create(raw);path=[1,3,0]
        self.assertFalse(self.native.calculate(raw,path=path)['valid'])
        with self.assertRaises(TransactionFailed):self.machine.functions.score(jid,path).call()

    def test_random_native_solidity_boundary_differential(self):
        rng = random.Random(4721)
        for _ in range(18):
            raw = example(); raw['edges'] = [raw['edges'][0]]
            e = raw['edges'][0]
            e['reserve_in'] = str(rng.randint(1, 10**13)); e['reserve_out'] = str(rng.randint(1, 10**13))
            e['fee_ppm'] = str(rng.choice([0, 1, 3000, 999999])); e['cost'] = str(rng.randint(0, 100))
            raw['constraints']['amount_in'] = str(rng.choice([1, 2, 100, 10**6, 10**12, (1<<63)-1]))
            raw['constraints']['maximum_impact_bps'] = str(rng.choice([0, 1, 500, 10000]))
            jid = self.create(raw); native = self.native.calculate(raw, path=[0])
            try: scored = self.machine.functions.score(jid, [0]).call()
            except TransactionFailed: self.assertFalse(native['valid'])
            else:
                self.assertTrue(native['valid']); self.assertEqual(int(native['net_output']), scored[0])

    def test_commitment_abi_python_matches_contract_and_is_not_portable(self):
        # Pure client only permits Sepolia; independently encode the EVM test chain here.
        from eth_abi import encode
        salt = b'f' * 32; path = [1,2]
        encoded = self.w3.keccak(encode(['address','uint256','uint256','address','uint8[]','bytes32'],
                                      [self.machine.address,self.w3.eth.chain_id,self.jid,self.a,path,salt]))
        self.assertEqual(encoded, self.machine.functions.commitmentFor(self.jid,self.a,path,salt).call())
        self.assertNotEqual(encoded, commitment(self.machine.address,421614,self.jid,self.a,path,salt))


class MiningWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.db = Path(self.tmp.name)/'owner.sqlite3'
        self.work = Workspace(self.db); self.native = NativeMining()

    def tearDown(self): self.tmp.cleanup()

    def test_restart_snapshot_journal_and_no_implied_payment(self):
        raw = example(); job = self.work.mining.create(raw); raw['edges'][0]['cost'] = '999'
        result = self.work.mining.solve(job['id'], {'budget':70000})
        reopened = Workspace(self.db).mining.get(job['id'])
        self.assertEqual(reopened['snapshot']['edges'][0]['cost'], '100')
        self.assertEqual(reopened['result'], result); self.assertEqual(reopened['confirmed_reward'],'0')
        self.assertEqual(reopened['status'], 'UNPUBLISHED_DRAFT')
        with self.work.runtime.connect() as db: self.assertTrue(verify_journal(db))

    def test_budget_cutoff_does_not_claim_optimality(self):
        answer = self.native.calculate(example(), budget=1)
        self.assertFalse(answer['search_complete']); self.assertEqual(answer['expansions'],1)
        full = self.native.calculate(example()); self.assertTrue(full['search_complete'])
        self.assertEqual(full['path'],[1,2]); self.assertGreater(int(full['net_output']),int(answer['net_output']))

    def test_unfeasible_is_not_fabricated_reward(self):
        raw=example(); raw['constraints']['minimum_net']='2000000000'
        out=self.native.calculate(raw); self.assertFalse(out['valid']); self.assertEqual(out['path'],[])

    def test_exact_integers_bounds_and_boolean_rejection(self):
        for value in [True,1,1.5,'01','-1','1e9',str(1<<63)]:
            raw=example();raw['constraints']['amount_in']=value
            with self.subTest(value=value), self.assertRaises(MachineError):normalize(raw)
        for budget in [True,0,70001]:
            with self.assertRaises(MachineError):self.native.calculate(example(),budget=budget)

    def test_library_hash_pinning(self):
        with self.assertRaises(MachineError):NativeMining(os.environ['ENGINE_MINING_LIBRARY'],'0'*64)

    def test_owner_isolation_and_api_boundary(self):
        job=self.work.mining.create(example()); other=Workspace(Path(self.tmp.name)/'other.sqlite3')
        with self.assertRaises(MachineError):other.mining.get(job['id'])
        app=create_engine_app(self.db,workspace=self.work,admin_token='a'*48)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/engine/mining').status_code,401)
            headers={'Authorization':'Bearer '+'a'*48}
            self.assertEqual(client.get('/api/engine/mining',headers=headers).status_code,200)
            self.assertEqual(client.post('/api/engine/mining/jobs/'+job['id']+'/solve',json={'budget':70000},headers=headers).status_code,200)
            self.assertEqual(client.get('/mining.js').status_code,200)

    def test_seal_exclusive_private_file_no_salt_in_commit_and_reveal_integrity(self):
        raw=example();result=self.native.calculate(raw);path=Path(self.tmp.name)/'secret.json'
        args={'contract':'0x'+'1'*40,'jid':1,'miner':'0x'+'2'*40,'secret_file':path}
        out=seal(raw,result,**args)
        self.assertNotIn('salt',out);self.assertFalse(out['chain_job_verified'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):seal(raw,result,**args)
        self.assertTrue(reveal(path)['transaction']['data'].startswith('0x'))
        private=json.loads(path.read_text());private['path']=[0];path.write_text(json.dumps(private))
        with self.assertRaises(MachineError):reveal(path)

    def test_supplied_candidate_is_recomputed_and_snapshot_bound(self):
        raw=example();good=self.native.calculate(raw,path=[1,2]);bad=copy.deepcopy(raw);bad['edges'][0]['cost']='101'
        with self.assertRaises(MachineError):seal(bad,good,contract='0x'+'1'*40,jid=1,miner='0x'+'2'*40,secret_file=Path(self.tmp.name)/'secret')

    def test_hosted_agent_key_cannot_freeze_solve_or_read_owner_mining_jobs(self):
        app=create_app(Path(self.tmp.name)/'commerce.sqlite3')
        with TestClient(app) as owner, TestClient(app) as agent:
            self.assertEqual(owner.post('/api/sessions',json={}).status_code,200)
            created=owner.post('/api/keys',json={'name':'Read only','scopes':['read'],'policy_id':None,'ttl_seconds':3600})
            self.assertEqual(created.status_code,200)
            agent.headers['Authorization']='Bearer '+created.json()['secret']
            job=owner.post('/api/engine/mining/jobs',json=example());self.assertEqual(job.status_code,200)
            jid=job.json()['id']
            self.assertEqual(agent.get('/api/engine/mining').status_code,403)
            self.assertEqual(agent.get('/api/engine/mining/jobs/'+jid).status_code,403)
            self.assertEqual(agent.post('/api/engine/mining/jobs',json=example()).status_code,403)
            self.assertEqual(agent.post('/api/engine/mining/jobs/'+jid+'/solve',json={'budget':70000}).status_code,403)
