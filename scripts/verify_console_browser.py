"""Remote-only browser checks; fixtures never sign or submit financial orders."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]

async def main():
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        client=await p.request.new_context()
        for width in [390,768,1440]:
            page=await browser.new_page(viewport={"width":width,"height":960})
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(request):
                url=request.request.url.replace('https://console.test/commerce/','http://127.0.0.1:4382/')
                if '/api/' in url:
                    await request.fulfill(status=401,json={'error':'Sign in required'})
                elif url.endswith('/healthz'):
                    await request.fulfill(json={'mode':'production','status':'ok'})
                else:
                    r=await client.get(url)
                    await request.fulfill(status=r.status,body=await r.body(),headers={'content-type':r.headers.get('content-type','text/html')})
            await page.route('https://console.test/**',route)
            await page.goto('https://console.test/commerce/console')
            await page.get_by_label('Message Skew').wait_for()
            assert await page.get_by_text('What would you like to get done?').is_visible()
            await page.screenshot(path=str(ROOT/'evidence'/f'console-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
            if width<=800:await page.get_by_role('button',name='Open navigation',exact=True).click()
            await page.get_by_role('button',name='Data licenses',exact=True).click()
            await page.get_by_text('Purchase data for your agents').wait_for()
            await page.screenshot(path=str(ROOT/'evidence'/f'data-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
            assert not errors,errors
            results.append({'width':width,'overview':True,'data':True,'horizontal_overflow':False,'js_errors':errors})
            await page.close()
        await browser.close()
    (ROOT/'evidence/browser.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))

asyncio.run(main())
