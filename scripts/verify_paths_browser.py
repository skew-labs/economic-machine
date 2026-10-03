"""Actual public console -> price API and market read -> native decision.

Uses a disposable unfunded wallet for login. No payment signature or paid job.
"""

import json
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://machine.148-113-153-116.nip.io/commerce'
OUT = ROOT / 'artifacts/production-paths'


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Trusted remote browser operation required')
    wallet = Account.create()  # Unfunded, in memory, login only. Never printed or saved.
    checks, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
        context = browser.new_context()
        headers = {'Origin': 'https://machine.148-113-153-116.nip.io'}
        challenged = context.request.post(BASE + '/api/auth/challenge', data={'address': wallet.address, 'chain_id': 42161}, headers=headers)
        assert challenged.status == 200
        challenge = challenged.json()
        proof = wallet.sign_message(encode_defunct(text=challenge['message']))
        login = context.request.post(BASE + '/api/auth/verify', data={'challenge_id': challenge['challenge_id'],
            'signature': '0x' + proof.signature.hex()}, headers=headers)
        assert login.status == 200, 'Disposable wallet login failed'
        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(BASE + '/console#market')
        page.get_by_role('button', name='Review inference request', exact=True).wait_for()
        page.locator('#connection-dot.connected').wait_for()
        page.get_by_role('button', name='Review inference request', exact=True).click()
        page.get_by_label('Maximum output tokens', exact=True).fill('8')
        with page.expect_response(lambda r: r.url.endswith('/api/commerce/compute/jobs') and r.request.method == 'POST') as result:
            page.get_by_role('button', name='Get exact price', exact=True).click()
        response = result.value
        assert response.status == 200, 'Compute quote API failed'
        quoted = response.json()
        assert quoted['kind'] == 'INFERENCE_API' and quoted['agreement']['terms']['total_price'] == '0.001'
        assert quoted.get('payment') is None and quoted.get('payment_id') is None
        page.get_by_role('button', name='Create purchase limit', exact=True).wait_for()
        page.screenshot(path=str(OUT / 'compute-review.jpg'), full_page=True, type='jpeg', quality=85)
        page.locator('#commerce-dialog').get_by_role('button', name='Close', exact=True).click()
        page.locator('[data-view="connections"]').click()
        page.get_by_label('Connector', exact=True).select_option('binance-public-market')
        page.get_by_label('Account name', exact=True).fill('Public BTC market')
        page.get_by_label('symbol', exact=True).fill('BTCUSDT')
        with page.expect_response(lambda r: r.url.endswith('/api/engine/connections') and r.request.method == 'POST') as result:
            page.get_by_role('button', name='Add connection', exact=True).click()
        configured = result.value
        assert configured.status == 200
        cid = configured.json()['id']
        row = page.get_by_role('row').filter(has_text='Public BTC market').last
        row.get_by_role('button', name='Sync', exact=True).click()
        row.get_by_text('CONNECTED', exact=True).wait_for()
        overview = context.request.get(BASE + '/api/engine/overview').json()
        source = next(c['snapshot'] for c in overview['connections'] if c['id'] == cid)
        request = {'operation': 'RETURN_STATISTICS', 'input': {'samples': source['samples'], 'count': len(source['samples']),
            'expected_interval_ns': 60000000000, 'interval_tolerance_ns': 0}}
        measured = context.request.post(BASE + '/api/engine/economics/evaluate', data=request)
        assert measured.status == 200 and measured.json()['computed']
        native = measured.json()
        assert native['language_model_calls'] == 0 and native['execution_authority'] == 'NONE'
        page.locator('[data-view="execution"]').click()
        page.get_by_role('heading', name='Live market rules', exact=True).wait_for()
        for view in ['execution', 'market', 'subscriptions']:
            for width in [1440, 390]:
                page.set_viewport_size({'width': width, 'height': 1000})
                if page.locator('#navigation-toggle').is_visible(): page.locator('#navigation-toggle').click()
                page.locator(f'[data-view="{view}"]').click()
                marker = 'Live market rules' if view == 'execution' else 'Atlas Monthly' if view == 'subscriptions' else 'Gate402 · AI compute'
                page.get_by_role('heading', name=marker, exact=True).wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
                assert page.locator('img').evaluate_all('(imgs)=>imgs.filter(i=>!i.complete || !i.naturalWidth).length') == 0
                page.screenshot(path=str(OUT / f'{view}-{width}.jpg'), full_page=True, type='jpeg', quality=85)
                checks.append({'view': view, 'width': width, 'no_horizontal_overflow': True})
        assert context.request.post(BASE + f'/api/engine/connections/{cid}/disconnect', data={}).status == 200
        browser.close()
    assert not errors, 'Browser script error'
    report = {'accepted': True, 'checks': checks, 'javascript_errors': errors,
        'compute_quote': {'checkout_id': quoted['id'], 'price_usdc': quoted['agreement']['terms']['total_price'],
            'request_sha256': quoted['request_sha256'], 'payment_created': False},
        'market': {'symbol': source['symbol'], 'observed_at': source['observed_at'], 'window_sha256': source['window_sha256'],
            'assurance': source['assurance'], 'closed_samples': len(source['samples'])},
        'native_decision': native, 'disposable_unfunded_login_signatures': 1, 'customer_signatures': 0, 'payments_submitted': 0,
        'venue_orders_submitted': 0, 'scope': 'ACTUAL_UI_QUOTE_AND_PUBLIC_MARKET_NATIVE_CALCULATION_NOT_PAID_DELIVERY_OR_FILL'}
    (OUT / 'browser-live-paths.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'accepted': True, 'checks': len(checks), 'actual_public_market_samples': len(source['samples']),
                      'actual_native_cpp': native['computed'], 'payments_submitted': 0}))


if __name__ == '__main__':
    main()
