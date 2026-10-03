"""Remote bounded loopback HTTP + actual C++ + journal soak. No live trade."""

import concurrent.futures
import json
import os
import secrets
import socket
import statistics
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from economic_machine.journal import verify_journal
from machine_engine.api import create_engine_app
from machine_engine.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Remote verification required')
    duration = int(os.environ.get('MACHINE_SOAK_SECONDS', '600'))
    if not 30 <= duration <= 3600:
        raise SystemExit('Bounded duration required')
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as t, socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); sock.listen(128)
        port, token = sock.getsockname()[1], secrets.token_urlsafe(48)
        origin = f'http://127.0.0.1:{port}'
        work = Workspace(Path(t) / 'soak.sqlite3', live_enabled=False)
        app = create_engine_app(work.runtime.db_path, workspace=work, admin_token=token, origin=origin)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline: raise SystemExit('Isolated server startup failed')
            time.sleep(.05)
        payload = {'operation': 'RETURN_STATISTICS', 'input': {'samples': [
            {'timestamp_ns': 60000000000, 'price': 100000000}, {'timestamp_ns': 120000000000, 'price': 110000000},
            {'timestamp_ns': 180000000000, 'price': 90000000}], 'count': 3,
            'expected_interval_ns': 60000000000, 'interval_tolerance_ns': 0}}
        elapsed, failures, codes, fingerprints = [], 0, {}, set()
        local, clients = threading.local(), []
        client_lock = threading.Lock()
        def call(_):
            if not hasattr(local, 'client'):
                local.client = httpx.Client(base_url=origin, timeout=5, trust_env=False)
                with client_lock: clients.append(local.client)
            at = time.perf_counter()
            try:
                result = local.client.post('/api/engine/economics/evaluate', json=payload, headers={'Authorization': 'Bearer ' + token})
                value = result.json()
                valid = result.status_code == 200 and value.get('computed') is True and value.get('language_model_calls') == 0
                return (time.perf_counter() - at) * 1000, result.status_code, valid, value.get('result')
            except Exception:  # noqa: BLE001 - bounded measurement failure, no auth token in report.
                return (time.perf_counter() - at) * 1000, 0, False, None
        measured = time.monotonic()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                while time.monotonic() - measured < duration:
                    batch = time.monotonic()
                    for latency, code, valid, result in pool.map(call, range(8)):
                        elapsed.append(latency); failures += not valid
                        codes[str(code)] = codes.get(str(code), 0) + 1
                        if result is not None: fingerprints.add(json.dumps(result, sort_keys=True))
                    time.sleep(max(0, 1 - (time.monotonic() - batch)))
        finally:
            for client in clients: client.close()
            server.should_exit = True; thread.join(timeout=10)
        elapsed.sort()
        with work.runtime.connect() as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and verify_journal(db)
            financial_rows = db.execute('SELECT COUNT(*) FROM engine_trade_orders').fetchone()[0]
        report = {'accepted': not failures and integrity and len(fingerprints) == 1 and financial_rows == 0,
            'duration_seconds': round(time.monotonic() - measured, 2), 'requests': len(elapsed), 'concurrent_clients': 8,
            'connection_mode': 'ONE_PERSISTENT_CLIENT_PER_WORKER_AFTER_INITIALIZATION', 'client_initialization_in_latency': False,
            'failures': failures, 'http_statuses': codes, 'p50_ms': statistics.median(elapsed),
            'p95_ms': elapsed[min(len(elapsed) - 1, int(len(elapsed) * .95))],
            'p99_ms': elapsed[min(len(elapsed) - 1, int(len(elapsed) * .99))],
            'max_ms': max(elapsed), 'deterministic_result_count': len(fingerprints), 'journal_verified': integrity,
            'native_sha256': work.economics.sha256, 'transactions_submitted': financial_rows, 'language_model_calls': 0,
            'scope': 'ISOLATED_LOOPBACK_HTTP_NATIVE_JOURNAL_NOT_CHAIN_FINALITY_OR_EXTERNAL_PROVIDER_LATENCY',
            'external_compute_charges_usdc': '0', 'external_llm_charges_usdc': '0',
            'host_cost_measured': False, 'process_elapsed_seconds': round(time.monotonic() - started, 2)}
        (ROOT / 'artifacts/production-paths/soak.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        if not report['accepted']: raise SystemExit('Soak acceptance failed')


if __name__ == '__main__':
    main()
