"""Browser regression of visual navigation; no customer auth, AI calls or trades."""
import argparse
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',required=True)
    parser.add_argument('--landing',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not str(Path(__file__).resolve()).startswith('/srv/skew/'):
        raise SystemExit('Run browser verification on the authorized remote host')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    rows=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
        for width in [1440,768,390,320]:
            page=await browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
            errors=[];assets=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('response',lambda r:assets.append((r.status,r.url)) if r.request.resource_type in {'script','stylesheet','image'} and r.status>=400 else None)
            await page.goto(args.base+'/console',wait_until='networkidle')
            await page.locator('.work-launcher').wait_for()
            await page.screenshot(path=str(out/f'console-{width}.png'),full_page=True)
            assert await page.locator('.work-tool').count()==4
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('.work-tool[data-tool="fuel"]').click()
            await page.get_by_label('USDC to swap',exact=True).wait_for()
            assert '/console' in page.url
            await page.goto(args.base+'/console#mining',wait_until='networkidle')
            await page.locator('.mining-hub').wait_for()
            await page.locator('.market-status').filter(has_text='No market yet').wait_for()
            assert await page.locator('a.market-contract').first.get_attribute('href')=='https://arbiscan.io/token/0x6cee6a99af671900001469a41da46e2921d18678'
            assert '160,000 SKEW' in await page.locator('.market-issuance-heading strong').inner_text()
            await page.get_by_role('button',name='Publish DataPass',exact=True).click()
            assert await page.locator('.process-explanation h3').inner_text()=='Publish DataPass'
            assert await page.locator('.market-price').inner_text()=='—'
            assert await page.locator('.market-line').count()==0
            await page.screenshot(path=str(out/f'mining-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.get_by_role('button',name='Open search worker',exact=True).click()
            await page.get_by_role('heading',name='Run your first search',exact=True).wait_for()
            await page.locator('#ops-mining').get_by_role('button',name='Connect wallet',exact=True).click()
            await page.locator('#login-dialog[open]').wait_for()
            await page.keyboard.press('Escape')
            await page.goto(args.base+'/console#data',wait_until='networkidle')
            await page.locator('.data-ticket').first.wait_for()
            await page.screenshot(path=str(out/f'data-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.goto(args.landing,wait_until='networkidle')
            await page.locator('.market-status').filter(has_text='No market yet').wait_for()
            assert await page.locator('.tool-row').count()==6
            for tool in ['engine','atlas','datapass','sitelens','mining','fuel']:
                await page.locator('.tool-row[data-product="'+tool+'"]').click()
                await page.wait_for_function('()=>{const n=document.querySelector("#profile-icon");return n&&n.complete&&n.naturalWidth>0}')
            await page.locator('.tool-row[data-product="engine"]').click()
            assert await page.locator('img.inception-badge').evaluate('(n)=>n.complete&&n.naturalWidth>0')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(out/f'landing-{width}.png'),full_page=True)
            assert not errors,errors
            assert not assets,assets
            rows.append({'width':width,'javascript_errors':errors,'failed_assets':assets,'horizontal_overflow':False,'tools':6,'same_console_fuel':True,'interactive_mining_steps':True,'wallet_dialog':True,'data_licenses':True,'mainnet_contract_no_fabricated_prices':True})
            await page.close()
        # Synthetic market data is used only inside this isolated browser test.
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        now=1791110000
        fixture={'symbol':'SKEW','chain_id':42161,'status':'AVAILABLE','price_usd':'.014','volume_24h_usd':'1234','market_cap_usd':None,'observed_at':now,'pool_address':'0x'+'b'*40,'source':'DEX Screener','candles':[{'timestamp':now-3600*i,'price_usd':str(.01+i*.0001)} for i in range(160,0,-1)]}
        await page.route('**/market/skew',lambda route:route.fulfill(json=fixture))
        await page.goto(args.landing,wait_until='networkidle')
        await page.locator('.market-line').wait_for()
        short=await page.locator('.market-line').get_attribute('d')
        await page.get_by_role('button',name='7D',exact=True).click()
        long=await page.locator('.market-line').get_attribute('d')
        assert len(long)>len(short)
        svg=page.locator('.market-plot svg');await svg.focus();await page.keyboard.press('ArrowLeft')
        assert 'hourly close' in await page.locator('.market-chart-note').inner_text()
        assert await page.locator('.market-stats dd').last.inner_text()=='—'
        rows.append({'fixture_only':True,'chart_range_updates':True,'keyboard_chart_inspection':True,'missing_market_cap_not_fdv':True})
        await browser.close()
    (out/'browser.json').write_text(json.dumps(rows,indent=2))
    print(json.dumps(rows))

asyncio.run(main())
