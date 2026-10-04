"""Mainnet ABI/emission tests on disposable PyEVM, never live funds or real VRF."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from web3 import EthereumTesterProvider, Web3
from web3.exceptions import TransactionNotFound

from economic_machine.values import MachineError
from machine_engine.mining_client import binding as legacy_binding
from machine_engine.solution_chain import FinalizedChain
from machine_engine.solution_client import binding, reveal, seal
from machine_engine.solution_operations import LocalMiner, MiningJournal, NativePipeline

ROOT = Path(__file__).resolve().parents[1]
GAS = {}


def hx(value):
    return "0x" + bytes(value).hex()


class Fixture:
    def __init__(self):
        backend = PyEVMBackend()
        backend.chain.chain_id = 42161
        self.tester = EthereumTester(backend)
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.owner, self.miner, self.other = self.w3.eth.accounts[:3]
        self.artifacts = json.loads((ROOT / "artifacts/solution-mainnet/contracts.json").read_text())
        self.vrf = self.deploy("SkewVRFMock")
        self.contract = self.deploy("SkewSolutionMining", self.vrf.address, b"k" * 32, 1, self.owner, 10**17)
        self.token = self.w3.eth.contract(
            address=self.contract.functions.rewardToken().call(),
            abi=self.artifacts["SkewSolutionToken"]["abi"],
        )

    def deploy(self, name, *args):
        item = self.artifacts[name]
        factory = self.w3.eth.contract(abi=item["abi"], bytecode=item["bytecode"])
        receipt = self.w3.eth.wait_for_transaction_receipt(
            factory.constructor(*args).transact({"from": self.owner})
        )
        assert receipt.status == 1
        GAS[name + "_deployment"] = receipt.gasUsed
        return self.w3.eth.contract(address=receipt.contractAddress, abi=item["abi"])

    def send(self, fn, sender=None, label=None):
        receipt = self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender or self.owner}))
        assert receipt.status == 1
        if label:
            GAS[label] = max(receipt.gasUsed, GAS.get(label, 0))
        return receipt

    def activate(self):
        self.send(self.vrf.functions.register(self.contract.address))
        self.send(self.vrf.functions.configure(10**18, True))
        self.send(self.contract.functions.activate())

    def start(self, seed=12345):
        self.activate()
        self.send(self.contract.functions.request(), label="request")
        self.send(self.vrf.functions.deliverLimited(1, [seed]), label="mock_callback_outer_call")
        assert self.contract.functions.rounds(1).call()[6] == 2

    def travel(self, timestamp):
        self.tester.time_travel(timestamp)
        self.tester.mine_blocks(8)

    def phase(self, index):
        self.travel(self.contract.functions.rounds(1).call()[index] + 1)

    def candidate(self, problem=0, seed=12345):
        proof = json.loads((ROOT / "artifacts/solution-operations/native-build.json").read_text())
        (result,) = NativePipeline(proof["executable"], proof["sha256"]).search(
            [
                {
                    "seed": seed,
                    "problem": problem,
                    "budget": 100000,
                    "algorithm": "integer_anneal",
                    "search_seed": 42,
                }
            ]
        )
        return result

    def commit(self, bits, problem=0, sender=None, salt=b"s" * 32):
        sender = sender or self.miner
        digest = self.contract.functions.commitmentFor(1, problem, sender, bits, salt).call()
        self.send(self.contract.functions.commit(1, problem, digest), sender, "commit")
        return salt

    def reader(self):
        fixture = self

        class Adapter:
            def __init__(self, identity):
                self.identity = identity

            def __call__(self, method, params):
                w3 = fixture.w3
                if method == "eth_chainId":
                    return hex(42161)
                if method == "eth_getBlockByNumber":
                    b = w3.eth.get_block(
                        "latest" if params[0] in {"latest", "finalized"} else int(params[0], 16)
                    )
                    return {"number": hex(b.number), "timestamp": hex(b.timestamp), "hash": hx(b.hash)}
                if method == "eth_getCode":
                    return hx(w3.eth.get_code(params[0], int(params[1], 16)))
                if method == "eth_call":
                    return hx(
                        w3.eth.call(params[0] | {"from": fixture.owner}, block_identifier=int(params[1], 16))
                    )
                if method == "eth_getTransactionByHash":
                    try:
                        t = w3.eth.get_transaction(params[0])
                    except TransactionNotFound:
                        return None
                    return {
                        "hash": hx(t.hash),
                        "blockHash": hx(t.blockHash),
                        "to": t.to,
                        "from": t["from"],
                        "input": hx(t.input),
                        "value": hex(t.value),
                        "chainId": hex(42161),
                    }
                if method == "eth_getTransactionReceipt":
                    try:
                        r = w3.eth.get_transaction_receipt(params[0])
                    except TransactionNotFound:
                        return None
                    return {
                        "transactionHash": hx(r.transactionHash),
                        "blockHash": hx(r.blockHash),
                        "blockNumber": hex(r.blockNumber),
                        "status": hex(r.status),
                        "logs": [
                            {"address": l.address, "topics": [hx(t) for t in l.topics], "data": hx(l.data)}
                            for l in r.logs
                        ],
                    }
                raise AssertionError(method)

        return FinalizedChain(
            Adapter("fixture-one"),
            Adapter("fixture-two"),
            self.contract.address,
            hashlib.sha256(self.w3.eth.get_code(self.contract.address)).hexdigest(),
            chain_id=42161,
        )


class MainnetContract(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def rejected(self, fn, sender=None):
        with self.assertRaises(TransactionFailed):
            fn.transact({"from": sender or self.f.owner})

    def test_launch_lock_and_subscription_reserve_consumer_gate(self):
        f = self.f
        self.assertTrue(f.contract.functions.admissionPaused().call())
        self.assertFalse(f.contract.functions.activated().call())
        self.assertEqual(f.token.functions.totalSupply().call(), 0)
        self.rejected(f.contract.functions.request())
        self.rejected(f.contract.functions.activate())
        f.send(f.vrf.functions.register(f.contract.address))
        f.send(f.vrf.functions.configure(1, True))
        self.rejected(f.contract.functions.activate())
        f.send(f.vrf.functions.configure(10**18, True))
        self.rejected(f.contract.functions.activate(), f.other)
        f.send(f.contract.functions.activate())
        self.rejected(f.contract.functions.activate())
        f.send(f.vrf.functions.configure(0, True))
        self.rejected(f.contract.functions.request())
        self.assertEqual(f.contract.functions.nextRound().call(), 1)

    def test_governance_two_step_revokes_every_old_keeper(self):
        f = self.f
        f.activate()
        f.send(f.contract.functions.setKeeper(f.miner, True))
        self.rejected(f.contract.functions.setKeeper(f.other, True), f.other)
        f.send(f.contract.functions.proposeGovernor(f.other))
        self.rejected(f.contract.functions.acceptGovernor(), f.miner)
        self.assertEqual(f.contract.functions.governor().call(), f.owner)
        f.send(f.contract.functions.acceptGovernor(), f.other)
        self.rejected(f.contract.functions.request(), f.owner)
        self.rejected(f.contract.functions.request(), f.miner)
        f.send(f.contract.functions.setKeeper(f.miner, True), f.other)
        f.send(f.contract.functions.setKeeper(f.miner, False), f.other)
        self.rejected(f.contract.functions.request(), f.miner)
        f.send(f.contract.functions.request(), f.other)

    def test_delayed_vrf_never_cancels_or_rerolls_and_250k_callback(self):
        f = self.f
        f.activate()
        f.send(f.contract.functions.request())
        f.travel(f.w3.eth.get_block("latest").timestamp + 86400)
        self.rejected(f.contract.functions.request())
        self.rejected(f.contract.functions.rawFulfillRandomWords(1, [1]), f.other)
        f.send(f.vrf.functions.deliver(1, []))
        self.assertEqual(f.contract.functions.rounds(1).call()[6], 1)
        f.send(f.vrf.functions.deliverLimited(1, [12345]), label="mock_callback_outer_call")
        r = f.contract.functions.rounds(1).call()
        self.assertEqual(r[1], 12345)
        self.assertEqual(r[4] - r[3], 600)
        self.assertGreater(r[3], f.w3.eth.get_block("latest").timestamp)
        f.send(f.vrf.functions.deliver(1, [99]))
        self.assertEqual(f.contract.functions.rounds(1).call()[1], 12345)
        self.assertFalse({"abort", "cancel", "reroll"} & {x.get("name") for x in f.contract.abi})

    def test_pause_preserves_existing_reveal_finalize_claim(self):
        f = self.f
        f.start()
        bits = int(f.candidate()["bits"])
        salt = f.commit(bits)
        f.send(f.contract.functions.pauseAdmission(True))
        self.rejected(f.contract.functions.commit(1, 1, b"x" * 32), f.other)
        f.phase(3)
        f.send(f.contract.functions.reveal(1, 0, bits, salt), f.miner, "reveal")
        f.phase(4)
        f.send(f.contract.functions.finalize(1), f.other, "finalize")
        f.send(f.contract.functions.claim(1, 0), f.miner, "claim")
        self.assertEqual(f.token.functions.balanceOf(f.miner).call(), 10**18)
        self.rejected(f.contract.functions.request())

    def test_copied_commitment_salt_phase_and_canonical_defenses(self):
        f = self.f
        f.start()
        bits = int(f.candidate()["bits"])
        salt = f.commit(bits)
        digest = f.contract.functions.commitmentFor(1, 0, f.miner, bits, salt).call()
        f.send(f.contract.functions.commit(1, 0, digest), f.other)
        self.rejected(f.contract.functions.commit(1, 0, digest), f.miner)
        self.rejected(f.contract.functions.reveal(1, 0, bits, salt), f.miner)
        self.rejected(f.contract.functions.score(1, 0, bits | 1))
        f.phase(3)
        self.rejected(f.contract.functions.reveal(1, 0, bits, salt), f.other)
        self.rejected(f.contract.functions.reveal(1, 0, bits, b"t" * 32), f.miner)
        f.send(f.contract.functions.reveal(1, 0, bits, salt), f.miner)
        self.rejected(f.contract.functions.reveal(1, 0, bits, salt), f.miner)
        f.phase(4)
        self.rejected(f.contract.functions.reveal(1, 0, bits, salt), f.other)

    def test_tie_order_bound_to_commit_not_reveal(self):
        f = self.f
        f.start()
        bits = int(f.candidate()["bits"])
        f.commit(bits, sender=f.miner)
        f.commit(bits, sender=f.other)
        f.phase(3)
        f.send(f.contract.functions.reveal(1, 0, bits, b"s" * 32), f.other)
        f.send(f.contract.functions.reveal(1, 0, bits, b"s" * 32), f.miner)
        self.assertEqual(f.contract.functions.best(1, 0).call()[0], f.miner)

    def test_batch_atomicity_cap_no_premine_and_erc20(self):
        f = self.f
        f.start()
        candidates = [int(f.candidate(p)["bits"]) for p in range(16)]
        for p, bits in enumerate(candidates):
            f.commit(bits, p)
        f.phase(3)
        for p, bits in enumerate(candidates):
            f.send(f.contract.functions.reveal(1, p, bits, b"s" * 32), f.miner, "reveal")
        f.phase(4)
        f.send(f.contract.functions.finalize(1), f.other)
        self.rejected(f.contract.functions.claimMany([1, 1], [0, 0]), f.miner)
        self.assertFalse(f.contract.functions.best(1, 0).call()[4])
        self.assertEqual(f.token.functions.totalSupply().call(), 0)
        self.rejected(f.contract.functions.claimMany([1], [0]), f.other)
        self.rejected(f.contract.functions.claimMany([], []), f.miner)
        self.rejected(f.contract.functions.claimMany([1] * 17, [0] * 17), f.miner)
        f.send(f.contract.functions.claimMany([1] * 16, list(range(16))), f.miner, "claim_16")
        self.assertEqual(f.token.functions.totalSupply().call(), 16 * 10**18)
        self.assertEqual(f.token.functions.cap().call(), 160000 * 10**18)
        self.assertEqual(f.token.functions.mining().call(), f.contract.address)
        self.rejected(f.token.functions.mint(f.owner, 1))
        self.rejected(f.contract.functions.claim(1, 0), f.miner)
        maximum = 2**256 - 1
        f.send(f.token.functions.approve(f.other, maximum), f.miner)
        f.send(f.token.functions.transferFrom(f.miner, f.owner, 1), f.other)
        self.assertEqual(f.token.functions.allowance(f.miner, f.other).call(), maximum)
        f.send(f.token.functions.approve(f.other, 7), f.miner)
        f.send(f.token.functions.transferFrom(f.miner, f.owner, 2), f.other)
        self.assertEqual(f.token.functions.allowance(f.miner, f.other).call(), 5)
        self.rejected(f.token.functions.transferFrom(f.miner, f.owner, 6), f.other)
        self.rejected(f.token.functions.transfer("0x" + "0" * 40, 1), f.miner)
        self.assertEqual(
            f.token.functions.balanceOf(f.miner).call() + f.token.functions.balanceOf(f.owner).call(),
            f.token.functions.totalSupply().call(),
        )

    def test_difficulty_changes_only_future_round_and_no_repeated_seed_reroll(self):
        f = self.f
        f.start()
        original = f.contract.functions.rounds(1).call()[5]
        f.phase(4)
        f.send(f.contract.functions.finalize(1), f.other)
        self.assertEqual(f.contract.functions.rounds(1).call()[5], original)
        self.assertEqual(f.contract.functions.nextThreshold().call(), original - 100)
        self.rejected(f.contract.functions.finalize(1))
        f.travel(f.contract.functions.rounds(1).call()[2] + 1801)
        f.send(f.contract.functions.request())
        f.send(f.vrf.functions.deliver(2, [12345]))
        self.assertEqual(f.contract.functions.rounds(2).call()[5], original - 100)

    def test_additional_seed_cpp_solidity_parity(self):
        f = self.f
        f.start(seed=987654321)
        for p in [0, 7, 15]:
            c = f.candidate(p, 987654321)
            score, total = f.contract.functions.score(1, p, int(c["bits"])).call()
            self.assertEqual((score, total), (int(c["score"]), int(c["total_weight"])))

    @classmethod
    def tearDownClass(cls):
        assert GAS["reveal"] < 2000000
        assert GAS["claim_16"] < 1500000
        (ROOT / "artifacts/solution-mainnet/gas-fixture.json").write_text(
            json.dumps(
                {
                    "gas_used": GAS,
                    "environment": "DISPOSABLE_PYEVM_CHAIN_42161",
                    "arbitrum_l1_fee_measured": False,
                    "public_transactions": 0,
                    "vrf_callback_limit": 250000,
                },
                indent=2,
            )
            + "\n"
        )


class MainnetOperator(unittest.TestCase):
    def test_network_scope_legacy_unchanged(self):
        addresses = ["0x" + "1" * 40, "0x" + "2" * 40]
        binding(addresses[0], 42161, 1, addresses[1])
        for network in [1, True, "42161", 10]:
            with self.subTest(network=network), self.assertRaises(MachineError):
                binding(addresses[0], network, 1, addresses[1])
        with self.assertRaises(MachineError):
            legacy_binding(addresses[0], 42161, 1, addresses[1])

    def test_cpp_mainnet_binding_to_finalized_token_balance(self):
        f = Fixture()
        f.start()
        f.tester.mine_blocks(8)
        proof = json.loads((ROOT / "artifacts/solution-operations/native-build.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            journal = MiningJournal(Path(temp) / "miner")
            reader = f.reader()
            miner = LocalMiner(
                journal,
                reader,
                NativePipeline(proof["executable"], proof["sha256"]),
                f.miner,
                daily_limit=1600000,
            )
            self.assertEqual(miner.tick(now=f.w3.eth.get_block("latest").timestamp)["jobs"], 16)
            (job,) = journal.db.execute("SELECT id FROM jobs WHERE state='SOLVED' LIMIT 1").fetchone()

            def execute(action):
                intent = miner.prepare(job, action, now=f.w3.eth.get_block("latest").timestamp)
                payload = journal.handoff(intent["intent_id"])
                self.assertEqual(payload["transaction"]["chainId"], 42161)
                tx = payload["transaction"]
                r = f.w3.eth.wait_for_transaction_receipt(
                    f.w3.eth.send_transaction(
                        {"from": f.miner, "to": tx["to"], "data": tx["data"], "value": 0}
                    )
                )
                self.assertEqual(r.status, 1)
                journal.record_hash(intent["intent_id"], hx(r.transactionHash))
                return journal.reconcile(intent["intent_id"], reader)

            self.assertEqual(execute("commit")["state"], "CONFIRMED")
            f.phase(3)
            self.assertEqual(execute("reveal")["state"], "CONFIRMED")
            f.phase(4)
            f.send(f.contract.functions.finalize(1))
            f.tester.mine_blocks(8)
            reward = execute("claim")
            self.assertEqual(reward["state"], "CONFIRMED")
            self.assertEqual(reward["minted_reward"], str(10**18))
            self.assertEqual(reward["reward_state_at_finalized_anchor"]["balance"], str(10**18))
            journal.close()

    def test_private_secret_cannot_replay_across_networks(self):
        f = Fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secret.json"
            payload = seal(
                {
                    "seed": "12345",
                    "problem": "0",
                    "bits": "42",
                    "algorithm": "integer_anneal",
                    "budget": "100000",
                    "search_seed": "42",
                },
                contract=f.contract.address,
                round_id=1,
                miner=f.miner,
                secret_file=path,
                chain_id=42161,
            )
            self.assertEqual(payload["transaction"]["chainId"], 42161)
            self.assertEqual(reveal(path)["transaction"]["chainId"], 42161)
            raw = json.loads(path.read_text())
            raw["chain"] = 421614
            path.write_text(json.dumps(raw))
            with self.assertRaises(MachineError):
                reveal(path)


if __name__ == "__main__":
    unittest.main()
