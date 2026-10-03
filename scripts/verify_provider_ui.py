"""Remote-only browser check for changed provider/subscription console cards."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://machine.148-113-153-116.nip.io/commerce'
OUT = ROOT / 'artifacts/landing-20261003'


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Browser verification runs on the remote host')
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscriptions-only', action='store_true')
    args = parser.parse_args()
    errors, checks = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
        for width in [1440, 390]:
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE + '/console#market')
            for view in ['subscriptions'] if args.subscriptions_only else ['market', 'subscriptions']:
                if view == 'subscriptions':
                    if page.locator('#navigation-toggle').is_visible():
                        page.locator('#navigation-toggle').click()
                    page.locator('[data-view="subscriptions"]').click()
                if view == 'market':
                    page.get_by_text('Gate402 · AI compute', exact=True).wait_for()
                    page.get_by_role('button', name='Check provider price', exact=True).wait_for()
                    page.get_by_text('Inference API · GPU capacity unverified · purchasing not open yet', exact=True).wait_for()
                else:
                    page.get_by_role('button', name='Review purchase', exact=True).wait_for()
                    page.locator('.commerce-product').filter(has=page.get_by_role('heading', name='Atlas Monthly', exact=True)).get_by_text('10 USDC / month', exact=True).wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
                broken = page.locator('img').evaluate_all('(imgs)=>imgs.filter(i=>!i.complete || i.naturalWidth===0).length')
                assert broken == 0
                prefix = 'subscription-active' if args.subscriptions_only else 'provider'
                page.screenshot(path=str(OUT / f'{prefix}-{view}-{width}.jpg'), type='jpeg', quality=85, full_page=True)
                checks.append({'view': view, 'width': width, 'no_horizontal_overflow': True, 'broken_images': 0})
            page.close()
        browser.close()
    assert not errors, errors
    result = {'accepted': True, 'checks': checks, 'javascript_errors': errors,
              'customer_signatures': 0, 'payments_submitted': 0, 'gpu_workloads_started': 0}
    name = 'subscription-active-browser-check.json' if args.subscriptions_only else 'provider-browser-check.json'
    (OUT / name).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
