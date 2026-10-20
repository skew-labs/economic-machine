from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from economic_machine.muse_receipts import Receipts, decimal

T = 1792000000000


def fill():
    return {"history_id": 1, "order_id": 2, "client_order_id": None, "symbol": "SOL",
            "amount": "1", "price": "100", "fee": "-0.005", "pnl": "0.005",
            "event_type": "fulfill_maker", "side": "open_long", "created_at": T, "cause": "normal"}


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"receipts.sqlite"
        self.book = Receipts(self.path, "public-account", T, T+48*3600*1000)

    def tearDown(self):
        self.book.close(); self.tmp.cleanup()

    def test_rebate_not_double_added(self):
        self.book.trade(fill()); r = self.book.report()
        self.assertEqual(r["actual_maker_rebate_usd"], "0.005")
        self.assertEqual(r["reported_trade_pnl_after_fee_usd"], "0.005")
        self.assertEqual(r["gross_trade_pnl_usd"], "0.000")
        self.assertEqual(r["fee_rate_mismatches"], 0)

    def test_receipt_dedup_and_conflict(self):
        f = fill(); self.assertTrue(self.book.trade(f)); self.assertFalse(self.book.trade(f))
        f["fee"] = "-0.006"
        with self.assertRaises(ValueError): self.book.trade(f)

    def test_taker_close_fee_once(self):
        f = fill(); f.update(amount="2.59", price="108.27", fee="0.112168", pnl="1.079232", event_type="fulfill_taker", side="close_long")
        self.book.trade(f); r = self.book.report()
        self.assertEqual(r["reported_trade_pnl_after_fee_usd"], "1.079232")
        self.assertEqual(r["gross_trade_pnl_usd"], "1.191400")
        self.assertEqual(r["actual_maker_rebate_usd"], "0")

    def test_partial_fills_have_distinct_history_id(self):
        f = fill(); self.book.trade(f); f["history_id"] = 3; self.book.trade(f)
        self.assertEqual(self.book.report()["fills"], 2)

    def test_old_trade_not_new_session_profit(self):
        f = fill(); f["created_at"] = T-1
        self.assertFalse(self.book.trade(f)); self.assertEqual(self.book.report()["fills"], 0)

    def test_restart_bound_to_original_scope(self):
        self.book.trade(fill()); self.book.close()
        self.book = Receipts(self.path, "public-account", T, T+48*3600*1000)
        self.assertFalse(self.book.trade(fill()))
        with self.assertRaises(ValueError): Receipts(self.path, "public-account", T+1, T+48*3600*1000)

    def test_changed_account_rejected(self):
        with self.assertRaises(ValueError): Receipts(self.path, "another", T, T+48*3600*1000)

    def test_wrong_rate_detected(self):
        f = fill(); f["fee"] = "0.01"; self.book.trade(f)
        self.assertEqual(self.book.report()["fee_rate_mismatches"], 1)

    def test_decimal_validation(self):
        for x in (None, True, 1, 1.1, "NaN", "Infinity", "-Infinity", "1e999", "1e-999", "no"):
            with self.subTest(x=x), self.assertRaises(ValueError): decimal(x)

    def test_wrong_scope_and_size(self):
        for k, v in (("symbol", "ETH"), ("side", "bid"), ("event_type", "create"), ("amount", "0"), ("price", "-1"), ("history_id", True), ("cause", "unrecognized")):
            f = fill(); f[k] = v
            with self.subTest(k=k), self.assertRaises(ValueError): self.book.trade(f)

    def test_future_markout_exposes_adverse_selection(self):
        self.book.trade(fill())
        self.book.mark(1, 1000, market_ms=T+1000, received_ms=T+1001, bid="99.97", ask="99.99")
        r = self.book.report()["markouts"]["1000"]
        self.assertEqual(r["price_markout_usd"], "-0.02")
        self.assertEqual(r["markout_after_fee_usd"], "-0.015")

    def test_no_hindsight_or_stale_markout(self):
        self.book.trade(fill())
        for market, received in ((T+999, T+1000), (T+1251, T+1252), (T+1000, T+1300), (T+1000, T+999)):
            with self.assertRaises(ValueError): self.book.mark(1, 1000, market_ms=market, received_ms=received, bid="99.99", ask="100.01")

    def test_first_observation_frozen(self):
        self.book.trade(fill())
        self.assertTrue(self.book.mark(1, 100, market_ms=T+100, received_ms=T+101, bid="99.99", ask="100.01"))
        self.assertFalse(self.book.mark(1, 100, market_ms=T+101, received_ms=T+102, bid="100.99", ask="101.01"))
        self.assertEqual(self.book.report()["markouts"]["100"]["price_markout_usd"], "0.00")

    def test_missing_marks_not_zero_performance(self):
        self.book.trade(fill()); r = self.book.report()["markouts"]["5000"]
        self.assertEqual((r["samples"], r["missing"]), (0, 1))

    def test_short_markout_direction(self):
        f = fill(); f["side"] = "open_short"; self.book.trade(f)
        self.book.mark(1, 5000, market_ms=T+5000, received_ms=T+5001, bid="99.97", ask="99.99")
        self.assertEqual(self.book.report()["markouts"]["5000"]["price_markout_usd"], "0.02")

    def test_funding_deduplicated_and_not_guessed(self):
        f = {"history_id": 7, "symbol": "SOL", "side": "bid", "amount": "1", "payout": "-0.10", "rate": "0.001", "created_at": T}
        self.assertTrue(self.book.funding(f)); self.assertFalse(self.book.funding(f))
        r = self.book.report()
        self.assertEqual(r["funding_payout_reported_separately"], "-0.10")
        self.assertEqual(r["reported_trade_pnl_after_fee_usd"], "0")
        self.assertFalse(r["funding_balance_sign_reconciled"])


if __name__ == "__main__": unittest.main()
