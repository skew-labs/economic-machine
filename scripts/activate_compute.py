"""Pin one observed inference seller. Does not create mandates or payments."""

import json
import os
import tempfile
import time
from pathlib import Path

from machine_commerce.compute import payment_option
from machine_commerce.compute_registry import NETWORK, REQUEST, URL, USDC, ComputeRegistry
from machine_commerce.payments import validate_profiles
from machine_commerce.store import Store
from machine_commerce.transport import HTTPS

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('/var/lib/machine-commerce-sepolia')
SERVICE = 'machine-commerce-sepolia-runtime.service'


def private_json(path, value):
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)


def main():
    if os.geteuid() != 0 or not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Trusted remote root required')
    with tempfile.TemporaryDirectory() as t:
        registry = ComputeRegistry(Store(Path(t) / 'discovery.sqlite3', time.time))
        observed = registry.probe('gate402-inference')
    if observed['status'] != 'QUOTE_OBSERVED':
        raise SystemExit('Provider is not available for admission')
    profiles = json.loads((CONFIG / 'resources.json').read_bytes())
    profile = {'url': URL, 'network': NETWORK, 'asset': USDC, 'pay_to': observed['quote']['pay_to'],
        'token_name': 'USD Coin', 'token_version': '2', 'max_timeout_seconds': 120,
        'seller_owner': 'compute-gate402-inference', 'data_type': 'compute.inference',
        'data_version': 'gate402-v1', 'rpc_url': 'https://arb1.arbitrum.io/rpc', 'finality': 'finalized'}
    if 'gate402-inference' in profiles and profiles['gate402-inference'] != profile:
        raise SystemExit('An admitted compute profile cannot be silently replaced')
    status, headers, _ = HTTPS({URL}).call(URL, REQUEST, {'x-payment-network': NETWORK})
    if status != 402:
        raise SystemExit('Unsigned quote unavailable')
    payment_option(headers, profile, 5000)
    validate_profiles(profiles | {'gate402-inference': profile})
    private_json(CONFIG / 'resources.json', profiles | {'gate402-inference': profile})
    private_json(CONFIG / 'compute.json', {'gate402-inference': True})
    drop = Path('/etc/systemd/system') / (SERVICE + '.d')
    drop.mkdir(exist_ok=True)
    (drop / 'compute.conf').write_text('[Service]\nLoadCredential=compute.json:' + str(CONFIG / 'compute.json') +
        '\nEnvironment=MACHINE_COMPUTE_FILE=/run/credentials/' + SERVICE + '/compute.json\n')
    report = {'provider': 'gate402-inference', 'profile': profile, 'unsigned_quote': observed['quote'],
        'workloads_started': 0, 'signatures_generated': 0, 'payments_submitted': 0,
        'next_step': 'DAEMON_RELOAD_AND_RUNTIME_RESTART'}
    (ROOT / 'artifacts/production-paths/compute-admission.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'admitted': True, 'unsigned_price_atoms': observed['quote']['amount_atoms'], 'payments_submitted': 0}))


if __name__ == '__main__':
    main()
