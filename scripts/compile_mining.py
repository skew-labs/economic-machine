"""Compile only Machine Mining and its test token using the pinned official compiler."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Compile only on the authorized remote host')
    compiler = ROOT / 'tools/solc-0.8.24'
    prior = json.loads((ROOT / 'artifacts/contract-build.json').read_text())
    sha = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if sha != prior['compiler_sha256']:
        raise RuntimeError('Official compiler hash changed')
    sources = {name: {'content': (ROOT / 'contracts' / name).read_text()}
               for name in ['MachineMining.sol', 'TestCommerceToken.sol', 'MiningAdversary.sol']}
    settings = {'optimizer': {'enabled': True, 'runs': 200}, 'viaIR': True, 'evmVersion': 'shanghai',
                'outputSelection': {'*': {'*': ['abi', 'evm.bytecode.object', 'evm.deployedBytecode.object']}}}
    run = subprocess.run([str(compiler), '--standard-json'], input=json.dumps({'language': 'Solidity', 'sources': sources, 'settings': settings}),
                         text=True, capture_output=True, check=True, timeout=90)
    compiled = json.loads(run.stdout)
    errors = [e for e in compiled.get('errors', []) if e['severity'] == 'error']
    if errors:
        raise RuntimeError(json.dumps(errors))
    artifacts = {name: {'abi': value['abi'], 'bytecode': value['evm']['bytecode']['object'], 'runtime': value['evm']['deployedBytecode']['object']}
                 for contracts in compiled['contracts'].values() for name, value in contracts.items() if value['evm']['bytecode']['object']}
    folder = ROOT / 'artifacts/mining'; folder.mkdir(parents=True, exist_ok=True)
    (folder / 'contracts.json').write_text(json.dumps(artifacts, sort_keys=True) + '\n')
    proof = {'compiler_sha256': sha, 'settings': settings,
             'sources': {name: hashlib.sha256(v['content'].encode()).hexdigest() for name, v in sources.items()},
             'contracts': {name: {'runtime_bytes': len(v['runtime']) // 2,
                                 'creation_sha256': hashlib.sha256(bytes.fromhex(v['bytecode'])).hexdigest()}
                           for name, v in artifacts.items()},
             'warnings': [e['formattedMessage'] for e in compiled.get('errors', [])],
             'deployment': 'NONE', 'assurance': 'COMPILED_ON_REMOTE_NOT_PUBLIC_TESTNET'}
    (folder / 'contract-build.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof))


if __name__ == '__main__':
    main()
