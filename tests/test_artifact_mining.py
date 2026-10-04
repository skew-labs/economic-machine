"""Contract bytecode -> rights-bound release -> SKEW mint -> paid license -> delivered bytes."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from web3 import EthereumTesterProvider, Web3

from economic_machine.values import MachineError, canonical, digest
from machine_commerce.datapass import NETWORKS, DataPassChain, bytes32, release_identity
from machine_commerce.work_artifacts import TERMS, WorkProducts, build_artifact, score, verify_artifact
from machine_engine.mining import EDGE_FIELDS, JOB_FIELDS, example

ROOT = Path(__file__).resolve().parents[1]


class ArtifactMining(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = json.loads((ROOT / "artifacts/datapass-mining/contracts.json").read_text())

    def setUp(self):
        self.tester = EthereumTester(PyEVMBackend())
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.publisher, self.miner, self.buyer, self.other = self.w3.eth.accounts[:4]
        self.payment = self.deploy("TestCommerceToken")
        self.passport = self.deploy("SkewDataPass", self.publisher, self.payment.address)
        self.mining = self.deploy("SkewArtifactMining", self.publisher, self.passport.address)
        self.reward = self.w3.eth.contract(address=self.mining.functions.rewardToken().call(), abi=self.compiled["SkewSolutionToken"]["abi"])
        self.raw = {k: v for k, v in example().items() if k not in {"reward_units", "bond_units"}}
        self.now = self.w3.eth.get_block("latest").timestamp
        self.grant = {"schema": "skew-source-grant-1", "input_sha256": digest(self.raw),
                      "source_root": self.raw["source_root"], "publisher": self.publisher,
                      "license": "publisher-owned-or-explicitly-licensed", "evidence_sha256": "a" * 64,
                      "valid_until": self.now + 300000, "allow_derived_sale": True, "allow_miner_distribution": True}
        self.report = build_artifact(self.raw, [1, 2], self.grant)
        self.root = bytes32(self.report["report_sha256"])
        self.terms = tuple(int(self.raw["constraints"][k]) for k in JOB_FIELDS) + (self.now + 120, self.now + 240,
            bytes32(self.raw["source_root"]), bytes32(digest(self.grant)), bytes32(digest(TERMS)))
        self.edges = [tuple(int(e[k]) for k in EDGE_FIELDS) for e in self.raw["edges"]]
        self.send(self.mining.functions.createJob(self.terms, self.edges), self.publisher)
        self.salt = b"s" * 32
        self.release_id = release_identity(self.report["report_sha256"], self.report["terms_sha256"])

    def deploy(self, name, *args):
        c = self.compiled[name]
        tx = self.w3.eth.contract(abi=c["abi"], bytecode=c["bytecode"]).constructor(*args).transact({"from": self.publisher})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.assertEqual(receipt.status, 1)
        return self.w3.eth.contract(address=receipt.contractAddress, abi=c["abi"])

    def send(self, call, who):
        r = self.w3.eth.wait_for_transaction_receipt(call.transact({"from": who}))
        self.assertEqual(r.status, 1)
        return r

    def commit(self, who=None, path=None, salt=None):
        who, path, salt = who or self.miner, path or [1, 2], salt or self.salt
        seal = self.mining.functions.commitmentFor(1, who, path, self.root, salt).call()
        self.send(self.mining.functions.commit(1, seal), who)

    def finish(self):
        self.commit()
        self.tester.time_travel(self.terms[7])
        self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.miner)
        self.tester.time_travel(self.terms[8])
        self.send(self.mining.functions.finalize(1), self.buyer)

    def publish(self, *, root=None, rights=None, terms=None, rid=None):
        value = (self.publisher, self.payment.address, root or self.root, terms or bytes32(digest(TERMS)),
                 rights or bytes32(digest(self.grant)), 10000, 86400, self.now + 10000, True, True,
                 "urn:skew:work:sha256:" + self.report["report_sha256"])
        self.send(self.passport.functions.registerRelease(rid or self.release_id, value), self.publisher)

    def test_complete_reward_purchase_delivery_and_exact_balances(self):
        self.assertEqual(self.reward.functions.totalSupply().call(), 0)
        self.assertEqual(self.reward.functions.cap().call(), 160000 * 10**18)
        self.finish()
        with self.assertRaises(TransactionFailed):
            self.send(self.mining.functions.claim(1, self.release_id), self.miner)
        self.publish()
        self.send(self.mining.functions.claim(1, self.release_id), self.other)
        self.assertEqual(self.reward.functions.balanceOf(self.miner).call(), 10**18)
        self.assertEqual(self.reward.functions.balanceOf(self.other).call(), 0)
        self.assertEqual(self.reward.functions.totalSupply().call(), 10**18)
        self.send(self.payment.functions.mint(self.buyer, 100000), self.publisher)
        self.send(self.payment.functions.approve(self.passport.address, 10000), self.buyer)
        order = b"o" * 32
        self.send(self.passport.functions.purchase(self.release_id, self.root, bytes32(digest(TERMS)), 10000, order), self.buyer)
        self.assertEqual(self.payment.functions.balanceOf(self.buyer).call(), 90000)
        self.assertEqual(self.payment.functions.balanceOf(self.publisher).call(), 10000)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), self.buyer)
        chain = self.reader()
        with tempfile.TemporaryDirectory() as folder:
            directory, grants = Path(folder) / "releases", Path(folder) / "grants"
            directory.mkdir(); grants.mkdir()
            (directory / (self.report["report_sha256"] + ".json")).write_bytes(canonical(self.report))
            (grants / (digest(self.grant) + ".json")).write_bytes(canonical(self.grant))
            service = WorkProducts(directory, grants, chain)
            result = service.purchase_status({"chain_id": 42161, "address": self.buyer}, order.hex(), self.report["report_sha256"])
            self.assertEqual(result["status"], "DELIVERED")
            self.assertEqual(digest(result["delivery"]["artifact"]), self.report["report_sha256"])
            with self.assertRaises(PermissionError):
                service.delivery(1, {"chain_id": 42161, "address": self.other}, self.report["report_sha256"])
            (grants / (digest(self.grant) + ".json")).unlink()
            with self.assertRaises(MachineError):
                service.version(self.report["report_sha256"])

    def reader(self):
        def read(_url, method, params):
            block = self.w3.eth.get_block("latest")
            if method == "eth_chainId": return hex(42161)
            if method == "eth_getBlockByNumber":
                return {"number": hex(block.number), "hash": self.w3.to_hex(block.hash), "timestamp": hex(block.timestamp)}
            if method == "eth_getCode": return self.w3.to_hex(self.w3.eth.get_code(params[0]))
            if method == "eth_call": return self.w3.to_hex(self.w3.eth.call(params[0]))
            raise AssertionError(method)
        code = hashlib.sha256(self.w3.eth.get_code(self.passport.address)).hexdigest()
        return DataPassChain(self.passport.address, code, read, lambda: self.w3.eth.get_block("latest").timestamp, chain_id=42161)

    def test_replay_matches_contract_score_and_tamper_rejected(self):
        expected = score(self.raw, [1, 2])
        self.assertEqual(self.mining.functions.score(1, [1, 2]).call(), [int(expected[k]) for k in ["net_output", "gross_output", "cost"]])
        bad = copy.deepcopy(self.report); bad["derived"]["result"]["net_output"] = "9999999"
        with self.assertRaises(MachineError): verify_artifact(bad)
        bad = copy.deepcopy(self.grant); bad["allow_derived_sale"] = False
        with self.assertRaises(MachineError): build_artifact(self.raw, [1, 2], bad)
        for path in ([2, 1], [0, 0], [99], [], [True]):
            with self.subTest(path=path), self.assertRaises(MachineError): score(self.raw, path)

    def test_publisher_only_admission_duplicate_work_and_no_admin_mint(self):
        changed = list(self.terms); changed[7] += 50; changed[8] += 50
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.createJob(tuple(changed), self.edges), self.publisher)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.createJob(tuple(changed), self.edges), self.other)
        with self.assertRaises(TransactionFailed): self.send(self.reward.functions.mint(self.publisher, 1), self.publisher)

    def test_commitment_binds_miner_artifact_and_closed_windows(self):
        self.commit()
        with self.assertRaises(TransactionFailed): self.commit()
        self.tester.time_travel(self.terms[7])
        with self.assertRaises(TransactionFailed): self.commit(self.other)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.reveal(1, [1, 2], b"z" * 32, self.salt), self.miner)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.other)
        self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.miner)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.miner)

    def test_wrong_rights_release_duplicate_claim_and_pause(self):
        self.finish()
        other_release = b"r" * 32
        self.publish(rights=b"x" * 32, rid=other_release)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.claim(1, other_release), self.miner)
        self.publish()
        self.send(self.passport.functions.setPaused(True), self.publisher)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.claim(1, self.release_id), self.miner)
        self.send(self.passport.functions.setPaused(False), self.publisher)
        self.send(self.mining.functions.claim(1, self.release_id), self.miner)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.claim(1, self.release_id), self.miner)

    def test_ties_follow_commit_order_and_empty_job_mints_nothing(self):
        self.commit(); self.commit(self.other)
        self.tester.time_travel(self.terms[7])
        self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.other)
        self.send(self.mining.functions.reveal(1, [1, 2], self.root, self.salt), self.miner)
        self.tester.time_travel(self.terms[8])
        self.send(self.mining.functions.finalize(1), self.buyer)
        self.assertEqual(self.mining.functions.job(1).call()[3], self.miner)
        with self.assertRaises(TransactionFailed): self.send(self.mining.functions.finalize(1), self.buyer)
        self.assertEqual(self.reward.functions.totalSupply().call(), 0)

    def test_mainnet_reader_network_identity_and_payment_asset(self):
        chain = self.reader()
        self.assertEqual(chain.chain_id, 42161)
        self.assertEqual(chain.asset, NETWORKS[42161]["asset"])
        with self.assertRaises(MachineError): DataPassChain(chain_id=1)

    def test_one_deployment_bundle_retains_owner_and_zero_supply(self):
        bundle = self.deploy("SkewLaunchBundle", self.publisher, self.payment.address)
        passport = self.w3.eth.contract(address=bundle.functions.dataPass().call(), abi=self.compiled["SkewDataPass"]["abi"])
        mining = self.w3.eth.contract(address=bundle.functions.mining().call(), abi=self.compiled["SkewArtifactMining"]["abi"])
        token = self.w3.eth.contract(address=bundle.functions.token().call(), abi=self.compiled["SkewSolutionToken"]["abi"])
        self.assertEqual(passport.functions.owner().call(), self.publisher)
        self.assertEqual(mining.functions.publisher().call(), self.publisher)
        self.assertEqual(token.functions.mining().call(), mining.address)
        self.assertEqual(token.functions.totalSupply().call(), 0)
