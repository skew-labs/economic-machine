"""Public-domain layout check; no login, model call, signature or transaction."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    rows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for width in [390, 768, 1440]:
            page = await browser.new_page(viewport={'width': width, 'height': 960})
            errors, assets = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('response', lambda r: assets.append({'status': r.status, 'url': r.url})
                    if r.request.resource_type in {'script', 'stylesheet'} else None)
            reply = await page.goto('https://skew.deals/commerce/console')
            assert reply.status == 200
            await page.get_by_label('Message Skew').wait_for()
            await page.get_by_text('Wallet login', exact=True).wait_for(state='attached')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(ROOT / 'evidence' / f'public-console-{width}.png'), full_page=True)
            if width <= 800:
                await page.get_by_role('button', name='Open navigation', exact=True).click()
            await page.get_by_role('button', name='Data licenses', exact=True).click()
            await page.get_by_text('Purchase data for your agents', exact=True).wait_for()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(ROOT / 'evidence' / f'public-data-{width}.png'), full_page=True)
            assert not errors, errors
            assert assets and all(r['status'] == 200 for r in assets), assets
            rows.append({'width': width, 'public_origin': 'https://skew.deals',
                         'console': True, 'data_licenses': True, 'horizontal_overflow': False,
                         'javascript_errors': errors, 'asset_responses_ok': len(assets)})
            await page.close()
        await browser.close()
    (ROOT / 'evidence/public-layout.json').write_text(json.dumps(rows, indent=2))
    print(json.dumps(rows))


asyncio.run(main())
