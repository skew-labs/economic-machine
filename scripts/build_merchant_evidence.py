"""Combine focused test results without hiding failed attempts or repeating suites."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Evidence generation runs on the trusted host')
    results, logs = {}, []
    for name in ['merchant-compute-tests.log', 'subscription-preflight-tests.log', 'compute-provider-tests.log']:
        path = ROOT / 'artifacts/atlas-release' / name
        content = path.read_text()
        matches = re.findall(r'^(test_\S+) \(([^)]+)\) \.\.\. (ok|FAIL|ERROR|skipped.*)$', content, re.M)
        if not matches:
            raise RuntimeError('Focused test results missing: ' + name)
        for method, owner, status in matches:
            results[owner] = {'status': status, 'latest_log': name}
        logs.append({'path': str(path.relative_to(ROOT)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'contains_failed_attempt': bool(re.search(r'^(FAIL|ERROR):|^FAILED ', content, re.M))})
    if any(row['status'] != 'ok' for row in results.values()):
        raise RuntimeError('Latest focused result has unresolved failures')
    live = json.loads((ROOT / 'artifacts/atlas-release/merchant-compute-live.json').read_text())
    browser = json.loads((ROOT / 'artifacts/landing-20261003/provider-browser-check.json').read_text())
    if not live['accepted'] or not browser['accepted']:
        raise RuntimeError('Live or browser connection evidence not accepted')
    body = {'accepted': True, 'latest_unique_passed': len(results), 'results': results, 'logs': logs,
        'live_compute_status': live['compute_connection']['status'], 'subscription_status': live['subscription_status'],
        'browser_views_checked': len(browser['checks']), 'customer_signatures': 0, 'payments_submitted': 0,
        'gpu_workloads_started': 0, 'assurance': 'CONTROLLED_TESTS_AND_UNSIGNED_CONNECTION_CHECKS_NOT_PAID_SERVICE_ACCEPTANCE'}
    (ROOT / 'artifacts/atlas-release/merchant-validation.json').write_text(json.dumps(body, indent=2) + '\n')
    print(json.dumps({'accepted': True, 'unique_tests': len(results), 'browser_views': len(browser['checks'])}))


if __name__ == '__main__':
    main()
