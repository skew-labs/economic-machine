"""Real bytecode in remote Py-EVM; no public network signing or deployment."""

import json
import unittest
from pathlib import Path

from eth_tester import EthereumTester, PyEVMBackend
from eth_tester.exceptions import TransactionFailed
from web3 import Web3, EthereumTesterProvider

ROOT = Path(__file__).resolve().parents[1]


class DataPassContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiled = json.loads((ROOT / "artifacts/contracts.json").read_text())

    def setUp(self):
        self.tester = EthereumTester(PyEVMBackend())
        self.web3 = Web3(EthereumTesterProvider(self.tester))
        self.seller, self.buyer, self.other, self.next_owner = self.web3.eth.accounts[:4]
        self.token = self.deploy("TestCommerceToken")
        self.passport = self.deploy("SkewDataPass", self.seller, self.token.address)
        self.root = self.web3.keccak(text="original-report")
        self.terms = self.web3.keccak(text="internal-use-license")
        self.provenance = self.web3.keccak(text="observed-source-root")
        self.release = self.web3.keccak(text="release-one")
        self.purchase_id = self.web3.keccak(text="buyer-order-one")
        self.ends = self.web3.eth.get_block("latest")["timestamp"] + 86400
        self.release_data = (self.seller, self.token.address, self.root, self.terms, self.provenance,
                             10000, 3600, self.ends, True, True, "https://example.test/releases/version-one.json")
        self.send(self.token.functions.mint(self.buyer, 100000), self.seller)
        self.send(self.token.functions.approve(self.passport.address, 10000), self.buyer)
        self.send(self.passport.functions.registerRelease(self.release, self.release_data), self.seller)

    def deploy(self, name, *args):
        value = self.compiled[name]
        factory = self.web3.eth.contract(abi=value["abi"], bytecode=value["bytecode"])
        receipt = self.web3.eth.wait_for_transaction_receipt(factory.constructor(*args).transact({"from": self.seller}))
        self.assertEqual(receipt.status, 1)
        return self.web3.eth.contract(address=receipt.contractAddress, abi=value["abi"])

    def send(self, call, actor):
        receipt = self.web3.eth.wait_for_transaction_receipt(call.transact({"from": actor}))
        self.assertEqual(receipt.status, 1)
        return receipt

    def buy(self, root=None, terms=None, price=10000, purchase_id=None, who=None, release=None):
        return self.send(self.passport.functions.purchase(release or self.release, root or self.root,
            terms or self.terms, price, purchase_id or self.purchase_id), who or self.buyer)

    def test_payment_mints_version_bound_license_and_exact_balances(self):
        receipt = self.buy()
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 90000)
        self.assertEqual(self.token.functions.balanceOf(self.seller).call(), 10000)
        self.assertEqual(self.token.functions.balanceOf(self.passport.address).call(), 0)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), self.buyer)
        self.assertEqual(self.passport.functions.balanceOf(self.buyer).call(), 1)
        self.assertTrue(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())
        event = self.passport.events.LicensePurchased().process_receipt(receipt, errors=__import__('web3').logs.DISCARD)[0]["args"]
        self.assertEqual(event["amount"], 10000)
        self.assertEqual(event["purchaseId"], self.purchase_id)

    def test_changed_roots_price_and_duplicate_id_rejected_without_debit(self):
        for kwargs in ({"root": self.terms}, {"terms": self.root}, {"price": 9999}):
            with self.assertRaises(TransactionFailed):
                self.buy(**kwargs)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 100000)
        self.buy()
        with self.assertRaises(TransactionFailed):
            self.buy()
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 90000)

    def test_transfer_changes_delivery_entitlement_and_clears_approval(self):
        self.buy()
        self.send(self.passport.functions.approve(self.other, 1), self.buyer)
        self.send(self.passport.functions.transferFrom(self.buyer, self.next_owner, 1), self.other)
        self.assertFalse(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())
        self.assertTrue(self.passport.functions.entitlement(1, self.next_owner, self.root, self.terms).call())
        self.assertEqual(self.passport.functions.getApproved(1).call(), "0x0000000000000000000000000000000000000000")
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.transferFrom(self.next_owner, self.other, 1), self.other)

    def test_expiry_blocks_access_and_transfer(self):
        self.buy()
        expires = self.passport.functions.licenses(1).call()[1]
        self.tester.time_travel(expires)
        self.tester.mine_blocks(1)
        self.assertFalse(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.transferFrom(self.buyer, self.other, 1), self.buyer)

    def test_sales_stop_and_global_pause_preserve_paid_access(self):
        self.buy()
        self.send(self.passport.functions.stopRelease(self.release), self.seller)
        self.send(self.passport.functions.setPaused(True), self.seller)
        self.assertTrue(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())
        with self.assertRaises(TransactionFailed):
            self.buy(purchase_id=self.terms)
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.transferFrom(self.buyer, self.other, 1), self.buyer)

    def test_unapproved_publisher_assets_and_release_mutation_rejected(self):
        for actor in (self.buyer, self.other):
            with self.assertRaises(TransactionFailed):
                self.send(self.passport.functions.registerRelease(self.terms, self.release_data), actor)
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.registerRelease(self.release, self.release_data), self.seller)
        alternate = self.deploy("TestCommerceToken")
        value = list(self.release_data)
        value[1] = alternate.address
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.registerRelease(self.terms, tuple(value)), self.seller)

    def test_nontransferable_license_and_wrong_content_access(self):
        value = list(self.release_data)
        value[8] = False
        self.send(self.passport.functions.registerRelease(self.terms, tuple(value)), self.seller)
        self.buy(release=self.terms)
        self.assertFalse(self.passport.functions.entitlement(1, self.buyer, self.terms, self.terms).call())
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.transferFrom(self.buyer, self.other, 1), self.buyer)

    def test_two_step_admin_transfer_does_not_grant_publication(self):
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.startOwnershipTransfer(self.other), self.buyer)
        self.send(self.passport.functions.startOwnershipTransfer(self.next_owner), self.seller)
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.acceptOwnership(), self.other)
        self.send(self.passport.functions.acceptOwnership(), self.next_owner)
        self.assertEqual(self.passport.functions.owner().call(), self.next_owner)
        self.assertFalse(self.passport.functions.publishers(self.next_owner).call())

    def test_unsafe_receiver_rolls_back_transfer_and_accepting_receiver_works(self):
        receiver = self.deploy("DataPassHostileReceiver")
        self.buy()
        function = self.passport.get_function_by_signature("safeTransferFrom(address,address,uint256)")
        with self.assertRaises(TransactionFailed):
            self.send(function(self.buyer, receiver.address, 1), self.buyer)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), self.buyer)
        self.send(receiver.functions.configure(True, "0x0000000000000000000000000000000000000000", b""), self.seller)
        self.send(function(self.buyer, receiver.address, 1), self.buyer)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), receiver.address)

    def test_fee_false_return_fake_payment_and_reentrancy_roll_back(self):
        hostile = self.deploy("DataPassHostileToken")
        self.send(self.passport.functions.setPaymentAsset(hostile.address, True), self.seller)
        value = list(self.release_data)
        value[1] = hostile.address
        self.send(self.passport.functions.registerRelease(self.terms, tuple(value)), self.seller)
        self.send(hostile.functions.mint(self.buyer, 100000), self.seller)
        self.send(hostile.functions.approve(self.passport.address, 10000), self.buyer)
        reentry = bytes.fromhex(self.passport.functions.purchase(self.terms, self.root, self.terms, 10000, self.root)._encode_transaction_data()[2:])
        for mode in (1, 2, 3, 4):
            self.send(hostile.functions.configure(mode, self.passport.address, reentry), self.seller)
            with self.assertRaises(TransactionFailed):
                self.buy(release=self.terms)
            self.assertEqual(hostile.functions.balanceOf(self.buyer).call(), 100000)
            self.assertEqual(self.passport.functions.nextTokenId().call(), 1)

    def test_erc721_interface_and_nonexistent_token(self):
        for interface in ("01ffc9a7", "80ac58cd", "5b5e139f"):
            self.assertTrue(self.passport.functions.supportsInterface(bytes.fromhex(interface)).call())
        self.assertFalse(self.passport.functions.supportsInterface(bytes.fromhex("ffffffff")).call())
        for function in (self.passport.functions.ownerOf(99), self.passport.functions.getApproved(99), self.passport.functions.tokenURI(99)):
            with self.assertRaises(TransactionFailed):
                function.call()


if __name__ == "__main__":
    unittest.main()
