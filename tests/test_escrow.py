import json
import unittest
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from web3 import EthereumTesterProvider, Web3
from web3.logs import DISCARD

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/contracts.json"


class EscrowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ARTIFACTS.exists():
            raise RuntimeError("compile contracts on the remote host before running escrow tests")
        cls.compiled = json.loads(ARTIFACTS.read_text())

    def setUp(self):
        self.tester = EthereumTester(PyEVMBackend())
        self.w3 = Web3(EthereumTesterProvider(self.tester))
        self.buyer, self.agent, self.seller, self.verifier, self.platform, self.stranger = self.w3.eth.accounts[:6]
        self.token = self.deploy("TestCommerceToken")
        self.escrow = self.deploy("MachineCommerceEscrow", self.platform, 100)
        self.send(self.token.functions.mint(self.buyer, 10_000_000), self.buyer)
        self.send(self.token.functions.approve(self.escrow.address, 2_000_000), self.buyer)
        self.bid = self.w3.keccak(text="budget-one")
        self.oid = self.w3.keccak(text="order-one")
        self.artifact = self.w3.keccak(text="delivered-json")
        self.proof = self.w3.keccak(text="verified-json")
        self.expiry = self.w3.eth.get_block("latest")["timestamp"] + 3600
        self.send(self.escrow.functions.registerBudget(self.bid, self.agent, self.seller, self.verifier,
            self.token.address, 2_000_000, 250_000, self.expiry), self.buyer)

    def deploy(self, name, *args):
        artifact = self.compiled[name]
        contract = self.w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])
        tx = contract.constructor(*args).transact({"from": self.w3.eth.accounts[0]})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.assertEqual(receipt.status, 1)
        return self.w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    def send(self, function, who):
        tx = function.transact({"from": who})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.assertEqual(receipt.status, 1)
        return receipt

    def funding(self, amount=120000, oid=None, deadline=None):
        return self.escrow.functions.fundOrder(oid or self.oid, self.bid,
            self.w3.keccak(text="csv-normalize"), self.w3.keccak(text="input"),
            self.w3.keccak(text="terms"), amount, deadline or self.expiry - 10)

    def test_agent_funds_seller_delivers_verifier_settles_exact_token_amounts(self):
        self.send(self.funding(), self.agent)
        self.assertEqual(self.token.functions.balanceOf(self.escrow.address).call(), 120000)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        receipt = self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)
        self.assertEqual(self.token.functions.balanceOf(self.seller).call(), 118800)
        self.assertEqual(self.token.functions.balanceOf(self.platform).call(), 1200)
        self.assertEqual(self.token.functions.balanceOf(self.escrow.address).call(), 0)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 9880000)
        self.assertEqual(self.escrow.functions.liabilities(self.token.address).call(), 0)
        event = self.escrow.events.PaymentSettled().process_receipt(receipt, errors=DISCARD)[0]["args"]
        self.assertEqual(event["artifactHash"], self.artifact)
        self.assertEqual(event["sellerAmount"], 118800)

    def test_duplicate_payment_and_changed_artifact_rejected(self):
        self.send(self.funding(), self.agent)
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(), self.agent)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.settle(self.oid, self.proof, self.proof), self.verifier)
        self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)
        self.assertEqual(self.token.functions.balanceOf(self.seller).call(), 118800)

    def test_unauthorized_funding_delivery_and_settlement_rejected(self):
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(), self.stranger)
        self.send(self.funding(), self.agent)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.agent)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        for actor in [self.seller, self.agent, self.stranger]:
            with self.assertRaises(TransactionFailed):
                self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), actor)

    def test_budget_per_order_total_and_revocation_are_enforced(self):
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(amount=250001), self.agent)
        for i in range(8):
            self.send(self.funding(amount=250000, oid=self.w3.keccak(text=str(i))), self.agent)
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(amount=1, oid=self.w3.keccak(text="over")), self.agent)
        self.send(self.escrow.functions.revokeBudget(self.bid), self.buyer)
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(amount=1), self.agent)

    def test_timeout_refunds_to_fixed_buyer_and_releases_budget(self):
        deadline = self.w3.eth.get_block("latest")["timestamp"] + 30
        self.send(self.funding(deadline=deadline), self.agent)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.refundExpired(self.oid), self.stranger)
        self.tester.time_travel(deadline + 1)
        self.tester.mine_blocks(1)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)
        self.send(self.escrow.functions.refundExpired(self.oid), self.stranger)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 10000000)
        self.assertEqual(self.escrow.functions.budgets(self.bid).call()[7], 0)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.refundExpired(self.oid), self.stranger)

    def test_bad_delivery_rejection_refunds_without_seller_income(self):
        self.send(self.funding(), self.agent)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        self.send(self.escrow.functions.rejectDelivery(self.oid, self.artifact), self.verifier)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 10000000)
        self.assertEqual(self.token.functions.balanceOf(self.seller).call(), 0)

    def test_false_return_and_fee_on_transfer_tokens_roll_back_reservation(self):
        self.send(self.token.functions.setFailTransfers(True), self.buyer)
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(), self.agent)
        self.assertEqual(self.escrow.functions.liabilities(self.token.address).call(), 0)
        self.send(self.token.functions.setFailTransfers(False), self.buyer)
        self.send(self.token.functions.setChargeTransferFee(True), self.buyer)
        with self.assertRaises(TransactionFailed):
            self.send(self.funding(), self.agent)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 10000000)
        self.assertEqual(self.escrow.functions.budgets(self.bid).call()[7], 0)

    def test_failed_payout_preserves_delivered_order_and_escrow(self):
        self.send(self.funding(), self.agent)
        self.send(self.escrow.functions.deliver(self.oid, self.artifact), self.seller)
        self.send(self.token.functions.setFailTransfers(True), self.buyer)
        with self.assertRaises(TransactionFailed):
            self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)
        self.assertEqual(self.escrow.functions.orders(self.oid).call()[-1], 2)
        self.assertEqual(self.token.functions.balanceOf(self.escrow.address).call(), 120000)
        self.send(self.token.functions.setFailTransfers(False), self.buyer)
        self.send(self.escrow.functions.settle(self.oid, self.artifact, self.proof), self.verifier)


if __name__ == "__main__":
    unittest.main()
