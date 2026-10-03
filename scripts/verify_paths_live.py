"""Unsigned public market/native probes; failures remain in the evidence."""

import argparse
import json
import tempfile
import time
from pathlib import Path

import httpx

from machine_engine.connections import Connectors
from machine_engine.live import MARKETS
from machine_engine.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Remote verification required')
    parser = argparse.ArgumentParser()
    parser.add_argument('--health-only', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'artifacts/production-paths/public-live-probes.json'
    probes = json.loads(output.read_text())['probes'] if args.health_only else []
    with tempfile.TemporaryDirectory() as temporary:
        work = Workspace(Path(temporary) / 'public-probes.sqlite3', live_enabled=False)
        reader = Connectors()
        for profile in ([] if args.health_only else MARKETS):
            started = time.perf_counter()
            try:
                source = reader.read({'profile': profile, 'config': {'symbol': 'BTCUSDT'}})
                value = work.economics.calculate({'operation': 'RETURN_STATISTICS', 'input': {
                    'samples': source['samples'], 'count': len(source['samples']),
                    'expected_interval_ns': 60000000000, 'interval_tolerance_ns': 0}})
                probes.append({'profile': profile, 'status': 'OBSERVED' if value['computed'] else 'REJECTED',
                    'venue_profile': source['venue_profile'], 'window_sha256': source['window_sha256'],
                    'closed_samples': len(source['samples']), 'observed_at': source['observed_at'],
                    'assurance': source['assurance'], 'native_computed': value['computed'],
                    'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)})
            except Exception as error:  # noqa: BLE001 - never serialize exception payloads.
                probes.append({'profile': profile, 'status': 'UNAVAILABLE', 'error_type': type(error).__name__,
                    'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)})
    with httpx.Client(timeout=15, trust_env=False) as client:
        response = client.get('http://127.0.0.1:4261/healthz')
        health = response.json()
    report = {'observed_at': int(time.time()), 'probes': probes, 'runtime_health_http': response.status_code,
        'runtime_health': health, 'runtime_health_scope': 'ACTUAL_LOOPBACK_PRODUCTION_SERVICE',
        'native_sha256': work.economics.sha256,
        'payments_submitted': 0, 'venue_orders_submitted': 0, 'language_model_calls': 0,
        'scope': 'PUBLIC_CLOSED_CANDLE_FETCH_AND_NATIVE_CALCULATION_NOT_PRIVATE_ACCOUNT_OR_FILL'}
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
