"""Atomic secondary license sales; executed only in remote local Py-EVM."""

import unittest
import test_datapass_contract as primary
from eth_tester.exceptions import TransactionFailed


class DataPassResale(unittest.TestCase):
    setUpClass = classmethod(primary.DataPassContract.setUpClass.__func__)
    deploy = primary.DataPassContract.deploy
    send = primary.DataPassContract.send
    buy = primary.DataPassContract.buy

    def setUp(self):
        primary.DataPassContract.setUp(self)
        self.buy()
        self.send(self.token.functions.mint(self.other, 100000), self.seller)
        self.send(self.token.functions.approve(self.passport.address, 100000), self.other)
        self.initial_expiry = self.passport.functions.licenses(1).call()[1]
        self.sale_end = self.initial_expiry - 60
        self.resale_id = self.web3.keccak(text="resale-order")

    def list(self, price=15000):
        self.send(self.passport.functions.listSale(1, price, self.sale_end), self.buyer)
        return self.passport.functions.sales(1).call()[3]

    def resale(self, nonce, price=15000, who=None, content=None, terms=None, purchase_id=None):
        return self.send(self.passport.functions.purchaseResale(1, self.buyer, price, nonce,
            content or self.root, terms or self.terms, purchase_id or self.resale_id), who or self.other)

    def test_exact_secondary_payment_and_transfer_preserve_original_expiry(self):
        nonce = self.list()
        self.resale(nonce)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), self.other)
        self.assertEqual(self.token.functions.balanceOf(self.buyer).call(), 105000)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 85000)
        self.assertEqual(self.passport.functions.licenses(1).call()[1], self.initial_expiry)
        self.assertFalse(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())
        self.assertTrue(self.passport.functions.entitlement(1, self.other, self.root, self.terms).call())
        self.assertEqual(self.passport.functions.sales(1).call()[0], "0x" + "0" * 40)
        self.assertEqual(self.passport.functions.purchaseIds(self.other, self.resale_id).call(), 1)

    def test_cancel_and_same_price_relist_cannot_replay_old_quote(self):
        old = self.list()
        self.send(self.passport.functions.cancelSale(1), self.buyer)
        new = self.list()
        self.assertGreater(new, old)
        with self.assertRaises(TransactionFailed): self.resale(old)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 100000)
        self.resale(new)

    def test_price_roots_and_self_purchase_cannot_mutate_accepted_sale(self):
        nonce = self.list()
        for kwargs in [{"price": 14999}, {"content": self.terms}, {"terms": self.root}, {"who": self.buyer}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(TransactionFailed): self.resale(nonce, **kwargs)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 100000)
        self.assertEqual(self.passport.functions.ownerOf(1).call(), self.buyer)

    def test_transfer_invalidates_listing_and_operator_cannot_set_price(self):
        self.send(self.passport.functions.approve(self.other, 1), self.buyer)
        with self.assertRaises(TransactionFailed):
            self.send(self.passport.functions.listSale(1, 15000, self.sale_end), self.other)
        nonce = self.list()
        self.send(self.passport.functions.transferFrom(self.buyer, self.next_owner, 1), self.other)
        with self.assertRaises(TransactionFailed): self.resale(nonce)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 100000)

    def test_paused_revoked_payment_asset_and_expired_sale_never_debit_buyer(self):
        nonce = self.list()
        self.send(self.passport.functions.setPaused(True), self.seller)
        with self.assertRaises(TransactionFailed): self.resale(nonce)
        self.send(self.passport.functions.setPaused(False), self.seller)
        self.send(self.passport.functions.setPaymentAsset(self.token.address, False), self.seller)
        with self.assertRaises(TransactionFailed): self.resale(nonce)
        self.send(self.passport.functions.setPaymentAsset(self.token.address, True), self.seller)
        self.tester.time_travel(self.sale_end); self.tester.mine_block()
        with self.assertRaises(TransactionFailed): self.resale(nonce)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 100000)
        self.assertTrue(self.passport.functions.entitlement(1, self.buyer, self.root, self.terms).call())

    def test_stopping_primary_sales_does_not_revoke_paid_transfer_rights(self):
        nonce = self.list()
        self.send(self.passport.functions.stopRelease(self.release), self.seller)
        self.send(self.passport.functions.setPublisher(self.seller, False), self.seller)
        self.resale(nonce)
        self.assertTrue(self.passport.functions.entitlement(1, self.other, self.root, self.terms).call())

    def test_same_purchase_id_cannot_be_spent_again_after_relist(self):
        nonce = self.list(); self.resale(nonce)
        self.send(self.passport.functions.transferFrom(self.other, self.buyer, 1), self.other)
        new = self.list()
        with self.assertRaises(TransactionFailed): self.resale(new)
        self.assertEqual(self.token.functions.balanceOf(self.other).call(), 85000)

    def test_listing_cannot_extend_token_expiry_or_price_zero(self):
        for price, expires in [(0, self.sale_end), (15000, self.initial_expiry + 1),
                               (15000, self.web3.eth.get_block("latest")["timestamp"])]:
            with self.subTest(price=price, expires=expires), self.assertRaises(TransactionFailed):
                self.send(self.passport.functions.listSale(1, price, expires), self.buyer)

    def test_hostile_secondary_payment_rolls_back_sale_ownership_and_balances(self):
        hostile = self.deploy("DataPassHostileToken")
        self.send(self.passport.functions.setPaymentAsset(hostile.address, True), self.seller)
        value = list(self.release_data); value[1] = hostile.address
        self.send(self.passport.functions.registerRelease(self.terms, tuple(value)), self.seller)
        self.send(hostile.functions.mint(self.buyer, 100000), self.seller)
        self.send(hostile.functions.approve(self.passport.address, 10000), self.buyer)
        self.buy(release=self.terms, purchase_id=self.root)
        self.send(self.passport.functions.listSale(2, 15000, self.sale_end), self.buyer)
        nonce = self.passport.functions.sales(2).call()[3]
        self.send(hostile.functions.mint(self.other, 100000), self.seller)
        self.send(hostile.functions.approve(self.passport.address, 100000), self.other)
        call = self.passport.functions.purchaseResale(2, self.buyer, 15000, nonce,
            self.root, self.terms, self.resale_id)
        callback = bytes.fromhex(call._encode_transaction_data()[2:])
        for mode in [1, 2, 3, 4]:
            self.send(hostile.functions.configure(mode, self.passport.address, callback), self.seller)
            with self.subTest(mode=mode), self.assertRaises(TransactionFailed): self.send(call, self.other)
            self.assertEqual(hostile.functions.balanceOf(self.other).call(), 100000)
            self.assertEqual(hostile.functions.balanceOf(self.buyer).call(), 90000)
            self.assertEqual(self.passport.functions.ownerOf(2).call(), self.buyer)
            self.assertEqual(self.passport.functions.sales(2).call()[3], nonce)
            self.assertEqual(self.passport.functions.purchaseIds(self.other, self.resale_id).call(), 0)
