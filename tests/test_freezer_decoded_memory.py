import hashlib
import unittest

from economic_machine.arbitrum_research.freezer_decoded_memory import decoded_digest, enabled, missing_is_memory_derived
from economic_machine.arbitrum_research.freezer_cloud_index import derived_expectations
from economic_machine.pump_research.store import canonical


class MemoryTests(unittest.TestCase):
    def test_exact_original_canonical_bytes(self):
        value = {'blocks': [{'number': 1, 'value': '한글', 'text': 'line\nquote"', 'nested': [None, False, 1.5]}], 'root_checks': True}
        raw = (canonical(value) + '\n').encode()
        self.assertEqual(decoded_digest(value), (len(raw), hashlib.sha256(raw).hexdigest()))

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError):
            decoded_digest({'v': float('nan')})

    def test_opt_in_and_legacy_policy(self):
        self.assertTrue(enabled(42161, True, '1'))
        self.assertFalse(enabled(42161, False, '1'))
        self.assertFalse(enabled(4663, True, '1'))
        self.assertFalse(enabled(42161, True, '1', {}))
        self.assertTrue(enabled(42161, True, '0', {'decoded_materialization': 'memory_v1'}))
        with self.assertRaisesRegex(ValueError, 'POLICY'):
            enabled(42161, True, '1', {'decoded_materialization': 'unknown'})
        with self.assertRaisesRegex(ValueError, 'SCOPE'):
            enabled(4663, True, '1', {'decoded_materialization': 'memory_v1'})

    def report(self):
        raw = b'input'; sha = hashlib.sha256(raw).hexdigest(); binary = '1' * 64
        report = dict(blocks=1, graph={'chain_id': 42161}, source_proof=dict(raw_sha256=sha, raw_bytes=len(raw), complete_cloud_readback=True, source={'binary_sha256': binary}),
                      derived_reconstruction=dict(schema='economic-machine.freezer-derived-provenance.v1', binary_sha256=binary, native_input_sha256=sha,
                      native_root_checks=True, blocks=1, chain_id=42161, decoded_bytes=12, decoded_sha256='2' * 64, decoded_materialization='memory_v1'))
        return report, binary, raw

    def test_missing_permission_only_declared_derived_not_raw(self):
        report, binary, raw = self.report()
        self.assertIsNotNone(derived_expectations(report, binary, raw))
        self.assertTrue(missing_is_memory_derived(report, 'candidate-decoded.json'))
        self.assertFalse(missing_is_memory_derived(report, 'candidate-records.json'))
        report['derived_reconstruction'].pop('decoded_materialization')
        self.assertFalse(missing_is_memory_derived(report, 'candidate-decoded.json'))

    def test_memory_still_requires_all_original_provenance(self):
        for field, value in [('native_root_checks', False), ('binary_sha256', '0' * 64), ('native_input_sha256', '0' * 64), ('decoded_bytes', 0), ('decoded_sha256', 'not-a-hash'), ('decoded_materialization', 'unknown')]:
            report, binary, raw = self.report()
            report['derived_reconstruction'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'PROVENANCE'):
                derived_expectations(report, binary, raw)

    def test_raw_cloud_proof_required_before_memory_admission(self):
        report, binary, raw = self.report()
        report['source_proof']['complete_cloud_readback'] = False
        with self.assertRaisesRegex(ValueError, 'PROVENANCE'):
            derived_expectations(report, binary, raw)


if __name__ == '__main__':
    unittest.main()
