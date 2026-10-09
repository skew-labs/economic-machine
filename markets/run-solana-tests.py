"""Offline version/layout suites; requires a matching MP_ELF, never sends RPC."""
import argparse, os, sys, unittest
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--version', choices=['v6', 'v7'], required=True)
p.add_argument('--mode', choices=['wide256', 'production'], default='wide256')
args = p.parse_args()
root = Path(__file__).resolve().parent / 'solana' / args.version
if args.mode == 'production' and args.version != 'v7':
    p.error('production layout exists only in v7')
os.environ['MP_LAYOUT'] = 'production256' if args.mode == 'production' else args.mode
os.environ.setdefault('MP_ELF', str(root / 'build/sbf/machine_perps.so'))
sys.path[:0] = [str(root / x) for x in ['tests', 'agents', 'client', 'muse', 'evolution', 'research']]
suite = unittest.TestSuite()
for file in sorted((root / 'tests').glob('test_*.py')):
    production_only = file.name == 'test_production_init.py'
    if production_only != (args.mode == 'production'):
        continue
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName(file.stem))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
