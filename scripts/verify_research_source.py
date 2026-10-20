"""Run the public research fixtures using explicitly built native libraries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ('test_arbitrum_', 'test_freezer_', 'test_muse_', 'test_pump_',
            'test_research_', 'test_robinhood_', 'test_history_', 'test_evm_research_')
EXACT = {'test_bulk_history', 'test_completion_gate', 'test_resident',
         'test_snapshot_ranges', 'test_solana_export', 'test_pool_workspace_status',
         'test_public_release'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-build', type=Path, default=ROOT / 'build/native')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('Run on the authorized Linux verification host.')
    libraries = {
        'MACHINE_PUMP_LIB': 'libmachine_pump_signal.so',
        'MUSE_RESEARCH_LIB': 'libmachine_muse_learning.so',
        'RESIDENT_NATIVE': 'libmachine_resident_gate.so',
    }
    for variable, filename in libraries.items():
        path = (args.native_build / filename).resolve()
        if not path.is_file():
            parser.error('Build the native targets first: ' + filename)
        os.environ[variable] = str(path)
    os.environ['RESIDENT_NATIVE_SHA256'] = hashlib.sha256(
        Path(os.environ['RESIDENT_NATIVE']).read_bytes()).hexdigest()
    for relative in ('src', 'tests', 'scripts', 'products/arbitrum_research', 'products/pump_research'):
        sys.path.insert(0, str(ROOT / relative))
    modules = sorted(path.stem for path in (ROOT / 'tests').glob('test_*.py')
                     if path.stem.startswith(PREFIXES) or path.stem in EXACT)
    suite = unittest.TestSuite()
    for name in modules:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=name + '.py'))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    report = dict(modules=len(modules), tests=result.testsRun, failures=len(result.failures),
                  errors=len(result.errors), skipped=len(result.skipped), passed=result.wasSuccessful(),
                  skip_reasons=[dict(test=str(test), reason=reason) for test, reason in result.skipped])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
