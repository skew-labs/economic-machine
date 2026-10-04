"""Render the brand kit and exercise the public product directory on a Linux QA host."""
import argparse
import json
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--export-assets', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with sync_playwright() as play:
        browser = play.chromium.launch(headless=True)
        if args.export_assets:
            page = browser.new_page()
            for file in sorted((ROOT / 'site/assets/brand').glob('*.svg')):
                width, height = (720, 192) if 'wordmark' in file.name else (544, 480)
                page.set_viewport_size({'width': width, 'height': height})
                svg = file.read_text().replace('<svg ', f'<svg width="{width}" height="{height}" ', 1)
                page.set_content('<style>html,body{margin:0;background:transparent}</style>' + svg)
                page.screenshot(path=str(file.with_suffix('.png')), omit_background=True)
            page.close()
            with zipfile.ZipFile(ROOT / 'site/assets/brand/skew-brand-kit.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in sorted((ROOT / 'site/assets/brand').iterdir()):
                    if file.suffix in {'.svg', '.png'}:
                        archive.write(file, file.name)
                for file in sorted((ROOT / 'site/assets').glob('app-*.svg')):
                    archive.write(file, 'products/' + file.name)
        for width in (1440, 390, 320):
            page = browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
            page.on('pageerror', lambda error: errors.append(str(error)))
            response = page.goto(args.base + '/', wait_until='networkidle')
            assert response.status == 200
            for tool in ('engine', 'atlas', 'datapass', 'sitelens', 'mining', 'fuel'):
                page.locator(f'.tool-row[data-product="{tool}"]').click()
                assert page.locator('.profile-art').get_attribute('data-product') == tool
                assert page.locator('#profile-source').get_attribute('href').endswith('/products/' + tool)
            page.locator('#tool-search').fill('fuel')
            assert page.locator('.tool-row:visible').count() == 1
            page.locator('#tool-search').fill('')
            page.locator('.tool-row[data-product="engine"]').click()
            page.evaluate('window.scrollTo(0, 0)')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('img').evaluate_all('(images)=>images.every(i=>i.complete && i.naturalWidth>0)')
            page.screenshot(path=str(args.output / f'landing-{width}.png'), full_page=True)
            checks.append({'page': 'landing', 'width': width, 'products': 6, 'overflow': False})
            response = page.goto(args.base + '/brand.html', wait_until='networkidle')
            assert response.status == 200
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('img').evaluate_all('(images)=>images.every(i=>i.complete && i.naturalWidth>0)')
            page.screenshot(path=str(args.output / f'brand-{width}.png'), full_page=True)
            checks.append({'page': 'brand', 'width': width, 'overflow': False})
            page.close()
        browser.close()
    result = {'checks': checks, 'page_errors': errors, 'financial_actions': 0}
    (args.output / 'browser.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    assert not errors


if __name__ == '__main__':
    main()
