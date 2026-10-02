import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_settlement import SettlementFixture
from compare_qwen import cheapest, expected_policy, supplier_book


class BenchmarkSettlementTests(unittest.TestCase):
    def test_long_inference_wait_cannot_expire_the_fixture_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = SettlementFixture(Path(directory))
            now = fixture.w3.eth.get_block("latest").timestamp
            policy = expected_policy(fixture.asset)
            book = supplier_book(now, fixture.asset)
            choice = cheapest(policy, book, now)
            # Regression: emulate a wall clock 300s beyond the last fixture block,
            # without sleeping or adding latency to any comparison arm.
            fixture.wall_clock = lambda: now + 300
            with patch("time.time", return_value=now + 300):
                done = fixture.settle("machine", 0, policy, choice["raw"])
            self.assertTrue(done["verified"])
            self.assertEqual(done["signed_submissions"], 1)
            self.assertEqual(fixture.balances()["recipient_atoms"], 400000)
            self.assertEqual(fixture.balances()["payer_atoms"], 99600000)
            self.assertGreaterEqual(fixture.w3.eth.get_block("latest").timestamp, now + 300)
