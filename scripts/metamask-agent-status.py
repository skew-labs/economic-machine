"""Run as the CLI owner; publish an allowlisted observation, never credentials.

CLI authentication and initialization are performed separately by the operator.
This observer only runs doctor, init show, and wallet address.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


def read_cli(node, cli, args):
    process = subprocess.run([node, cli, *args, '--json'], capture_output=True,
                             text=True, timeout=30, check=False)
    if process.returncode:
        raise ValueError('CLI read failed')
    # Some versions prepend progress to the JSON result; never print raw output.
    for index, char in enumerate(process.stdout):
        if char != '{':
            continue
        try:
            result, _ = json.JSONDecoder().raw_decode(process.stdout[index:])
        except ValueError:
            continue
        if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('data'), dict):
            return result['data']
    raise ValueError('CLI response invalid')


def observe(node, cli, *, reader=read_cli, clock=time.time):
    snapshot = {'authenticated': False, 'initialized': False, 'mode': None,
                'address': None, 'observed_at': int(clock())}
    try:
        doctor = reader(node, cli, ['doctor'])
        if doctor.get('authenticated') is not True or doctor.get('initialized') is not True:
            return snapshot
        mode = reader(node, cli, ['init', 'show'])
        if mode.get('walletMode') != 'server-wallet' or mode.get('tradingMode') != 'guard':
            return snapshot
        wallet = reader(node, cli, ['wallet', 'address', '--chain-namespace', 'evm'])
        address = wallet.get('address')
        if not isinstance(address, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', address) or int(address, 16) == 0:
            return snapshot
        return {'authenticated': True, 'initialized': True, 'mode': 'guard',
                'address': address, 'observed_at': int(clock())}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    result = observe(options.node, options.cli)
    fd, tmp = tempfile.mkstemp(prefix='.agent-status-', dir=options.output.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o640)
            json.dump(result, handle); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, options.output)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print(json.dumps({'authenticated': result['authenticated'], 'observed_at': result['observed_at']}))


if __name__ == '__main__':
    main()
