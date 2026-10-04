"""Public deployment check with an unfunded disposable login wallet, no chain writes."""
import asyncio,json,hashlib
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1];BASE='https://skew.deals';API=BASE+'/commerce/api/engine'

async def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote verification required')
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        client=context.request;headers={'Origin':BASE};wallet=Account.create()
        r=await client.post(BASE+'/commerce/api/auth/challenge',data={'address':wallet.address,'chain_id':42161},headers=headers)
        assert r.status==200;challenge=await r.json();assert challenge['message'].startswith('skew.deals wants you to sign in')
        sig=wallet.sign_message(encode_defunct(text=challenge['message'])).signature.hex()
        r=await client.post(BASE+'/commerce/api/auth/verify',data={'challenge_id':challenge['challenge_id'],'signature':'0x'+sig.removeprefix('0x')},headers=headers);assert r.status==200
        provider=await (await client.get(API+'/assistant/provider')).json()
        assert provider['provider']=='bedrock' and provider['status']=='ORGANIZATION_DENY' and provider['fallback'] is False
        denied=await client.post(API+'/assistant',data={'request_id':'public-bedrock-boundary-00001','message':'Help me clean a customer list.'},headers=headers)
        assert denied.status==409 and (await denied.json())['error']=='ASSISTANT_BEDROCK_ORGANIZATION_DENY'
        mcp=await client.post(API+'/mcp',data={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'release-verifier','version':'1'}}},headers={**headers,'Accept':'application/json, text/event-stream'})
        assert mcp.status==200 and (await mcp.json())['result']['protocolVersion']=='2025-11-25'
        page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(BASE+'/commerce/console')
        await page.get_by_role('button',name='Clean business data',exact=True).click()
        await page.get_by_label('CSV to clean').fill('name,email\n Alex , a@b.com \nAlex,a@b.com\nBea,b@c.com\n')
        await page.get_by_role('button',name='Compare two plans',exact=True).click()
        try:await page.get_by_role('button',name='Approve this plan',exact=True).nth(1).click()
        except Exception:
            await page.screenshot(path=str(ROOT/'evidence/public-workflow-failure.png'),full_page=True)
            (ROOT/'evidence/public-workflow-failure.txt').write_text((await page.locator('body').inner_text())[-8000:])
            raise
        await page.get_by_text('Done. Your result is ready.',exact=True).wait_for()
        async with page.expect_download() as download:await page.get_by_role('button',name='Download result',exact=True).click()
        downloaded=await download.value
        content=Path(await downloaded.path()).read_bytes();assert content==b'name,email\nAlex,a@b.com\nBea,b@c.com\n'
        await page.reload();await page.get_by_role('button',name='Resume my work',exact=True).click()
        await page.get_by_text('Completed data task',exact=True).wait_for()
        for width in (1440,390):
            await page.set_viewport_size({'width':width,'height':1000})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(ROOT/'evidence'/f'amazon-public-{width}.png'),full_page=True)
        assert not errors,errors
        result={'origin':BASE,'authentication':'disposable unfunded wallet, login signature only','public_workflow':'DELIVERED',
                'result_sha256':hashlib.sha256(content).hexdigest(),'mcp_version':'2025-11-25','provider':provider,
                'bedrock_live_response':False,'organization_gate_verified':True,'qwen_fallback':False,
                'reload_resumption':True,'mobile_desktop_layout':True,'chain_writes':0,'external_payments':0,'javascript_errors':errors}
        await client.post(BASE+'/commerce/api/auth/logout',data={},headers=headers)
        assert (await client.get(API+'/assistant/work')).status==401
        result['logout_revocation']=True
        (ROOT/'evidence/pr16-public.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
        await browser.close()

asyncio.run(main())
