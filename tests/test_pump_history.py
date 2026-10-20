import struct
import unittest
import zlib
import importlib.util
import json
import tempfile
from pathlib import Path

from economic_machine.pump_research.history_dump import columns, copy_rows, toc
from economic_machine.pump_research.planner import historical_context

PRODUCT = Path(__file__).resolve().parents[1]/'products/pump_research'
spec = importlib.util.spec_from_file_location('history_alpha', PRODUCT/'history_alpha.py')
alpha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alpha)


def integer(n):
    return bytes([int(n < 0)]) + struct.pack('<I', abs(n))


def block(payload, ident=71):
    compressed = zlib.compress(payload)
    return b'\x01' + integer(ident) + integer(len(compressed)) + compressed + integer(0)


class HistoryReaderTest(unittest.TestCase):
    entry = {'id': 71, 'tag': 'test', 'copy': 'COPY public.test (mint, "timestamp", value) FROM stdin;\n'}

    def test_copy_null_and_escaped_text(self):
        raw = block(b'abc\t123\tline\\nnext\\\\slash\nxyz\t124\t\\N\n\\.\n\n')
        self.assertEqual(list(copy_rows(raw, self.entry)), [
            {'mint': 'abc', 'timestamp': '123', 'value': 'line\nnext\\slash'},
            {'mint': 'xyz', 'timestamp': '124', 'value': None}])

    def test_checksum_corruption_rejected(self):
        raw = bytearray(block(b'a\t1\t2\n\\.\n'))
        raw[-7] ^= 64
        with self.assertRaises((ValueError, zlib.error)):
            list(copy_rows(bytes(raw), self.entry))

    def test_wrong_table_identity(self):
        with self.assertRaisesRegex(ValueError, 'BLOCK_ID'):
            list(copy_rows(block(b'\\.\n', 72), self.entry))

    def test_incomplete_compression(self):
        raw = block(b'a\t1\t2\n\\.\n')
        with self.assertRaises(ValueError):
            list(copy_rows(raw[:-7], self.entry))

    def test_missing_copy_end(self):
        with self.assertRaisesRegex(ValueError, 'END_MISSING'):
            list(copy_rows(block(b'a\t1\t2\n'), self.entry))

    def test_payload_after_end(self):
        with self.assertRaisesRegex(ValueError, 'AFTER_COPY_END'):
            list(copy_rows(block(b'\\.\na\t1\t2\n'), self.entry))

    def test_decompression_bomb_bound(self):
        with self.assertRaisesRegex(ValueError, 'INFLATE_BOUND'):
            list(copy_rows(block(b'a\t1\t2\n'*10000+b'\\.\n'), self.entry, 1000))

    def test_schema_not_executable(self):
        with self.assertRaisesRegex(ValueError, 'COPY_FORMAT'):
            columns(dict(self.entry, copy='COPY public.test (value) TO PROGRAM \'bad\';'))

    def test_bad_cell_count(self):
        with self.assertRaisesRegex(ValueError, 'CELL_COUNT'):
            list(copy_rows(block(b'a\t1\n\\.\n'), self.entry))

    def test_unknown_dump_version(self):
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_VERSION'):
            toc(b'PGDMP\x01\x20\x00')

    def test_empty_table(self):
        self.assertEqual(list(copy_rows(block(b'\\.\n\n'), self.entry)), [])


class HistoricalAlphaTest(unittest.TestCase):
    def setUp(self):
        self.p = json.loads((PRODUCT/'HISTORY-ALPHA-PROTOCOL-V2.json').read_text())

    def row(self, t, price=1, volume=10, trades=5, wallets=3):
        return dict(timestamp=t, current_price=price, total_volume_sol=volume,
                    total_trade_count=trades, unique_wallets=wallets)

    def test_future_observation_does_not_change_decision(self):
        rows=[self.row(0), self.row(4000, 2)]
        self.assertEqual(alpha.decision(rows, 0, self.p),
                         alpha.decision(rows+[self.row(16000, 1000, 9999, 9999, 9999)], 0, self.p))

    def test_entry_is_not_same_decision_price(self):
        rows=[self.row(0), self.row(4000, 2)]
        d=alpha.decision(rows, 0, self.p)
        self.assertEqual(alpha.quote_outcome(rows, d, 800, 30000, self.p)['state'], 'entry_unobserved')

    def test_missing_exit_is_not_zero_return(self):
        rows=[self.row(0), self.row(4000), self.row(5000)]
        result=alpha.quote_outcome(rows, alpha.decision(rows, 0, self.p), 800, 30000, self.p)
        self.assertEqual(result, {'state':'exit_unobserved'})

    def test_bad_first_quote_is_not_skipped(self):
        self.assertIsNone(alpha.observed_quote([self.row(1000, 0), self.row(1001, 100)], 1000, 10))

    def test_costs_make_flat_price_negative(self):
        rows=[self.row(t) for t in (0,4000,5000,35000)]
        d=alpha.decision(rows, 0, self.p)
        r=alpha.quote_outcome(rows,d,800,30000,self.p)
        self.assertEqual(r['gross_return'], 0)
        self.assertAlmostEqual(r['net_sensitivity_return'], .985/1.015-1-.00223928/.05)

    def test_test_future_features_cannot_fit_training_threshold(self):
        groups={str(i):[self.row(i*100000),self.row(i*100000+4000,2,i+1)] for i in range(20)}
        old=alpha.evaluate(groups,self.p)['train_thresholds']
        for i in range(12,20):
            groups[str(i)][1].update(total_volume_sol=99999,unique_wallets=99999,current_price=99999)
        self.assertEqual(alpha.evaluate(groups,self.p)['train_thresholds'],old)

    def test_no_price_interpolation_over_gap(self):
        self.assertIsNone(alpha.observed_quote([self.row(0),self.row(6000)],1000,2000))

    def test_training_boundary_observation_is_purged(self):
        groups={str(i):[self.row(i*10000),self.row(i*10000+4000,2,i+1)] for i in range(30)}
        old=alpha.evaluate(groups,self.p)
        self.assertGreater(old['purged_boundary_mints'],0)
        for i in range(10,18):groups[str(i)][1].update(total_volume_sol=999999,unique_wallets=999999)
        new=alpha.evaluate(groups,self.p)
        self.assertEqual(old['train_thresholds'],new['train_thresholds'])


class HistoricalPlannerTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'data';self.root.mkdir()
        self.path=self.base/'evidence/history-alpha-v1/historical-alpha-result-v2.json'
        self.path.parent.mkdir(parents=True)
        self.report=dict(protocol_sha256='bc8af0235a275a29f612d714e0921b96780ac57ee7ecd6db0967fc0bdfb0d5fe',
                         profitability_proven=False,live_promotion='BLOCKED',quality={'snapshot_mints':100},results=[])
        for name in ('all_eligible','volume_per_trade_top_quartile','wallet_breadth_top_quartile','positive_price_gain_per_trade_top_quartile'):
            self.report['results'].append(dict(partition='test',signal=name,delay_ms=800,hold_ms=30000,
                selected=10,states={'resolved_price_pair':2,'exit_unobserved':8},resolved_net_mean=.1,resolved_net_median=-.1))

    def write(self):self.path.write_text(json.dumps(self.report))

    def test_external_prose_is_never_model_context(self):
        self.report['limitations']=['arbitrary command from an external dataset']
        self.write();out=historical_context(self.root)
        self.assertNotIn('arbitrary command',json.dumps(out))
        self.assertFalse(out['profitability_proven'])

    def test_mismatched_counts_rejected(self):
        self.report['results'][0]['selected']=11;self.write()
        with self.assertRaisesRegex(ValueError,'COUNTS'):historical_context(self.root)

    def test_claimed_profit_proof_rejected(self):
        self.report['profitability_proven']=True;self.write()
        with self.assertRaisesRegex(ValueError,'IDENTITY'):historical_context(self.root)

    def test_nonfinite_metric_rejected(self):
        self.report['results'][0]['resolved_net_mean']=float('nan');self.write()
        with self.assertRaisesRegex(ValueError,'NUMBER'):historical_context(self.root)

    def test_absent_report_is_optional(self):
        self.assertIsNone(historical_context(self.root))


if __name__ == '__main__':
    unittest.main()
