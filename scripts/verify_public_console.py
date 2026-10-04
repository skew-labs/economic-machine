"""Remote browser acceptance using an ephemeral, unfunded authentication account.

No financial signature, order, purchase, wallet key persistence or chain write.
One bounded model call creates an owner-reviewed task, then checks reload recovery.
"""
import asyncio
import json
from pathlib import Path
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE='https://skew.deals'

async def main():
    account=Account.create()
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context(viewport={'width':1440,'height':960})
        client=context.request
        headers={'Origin':BASE}
        challenge=await client.post(BASE+'/commerce/api/auth/challenge',data={'address':account.address,'chain_id':42161},headers=headers)
        assert challenge.status==200,await challenge.text()
        body=await challenge.json()
        assert body['message'].startswith('skew.deals wants you to sign in')
        signature=account.sign_message(encode_defunct(text=body['message'])).signature.hex()
        verified=await client.post(BASE+'/commerce/api/auth/verify',data={'challenge_id':body['challenge_id'],'signature':'0x'+signature.removeprefix('0x')},headers=headers)
        assert verified.status==200,await verified.text()
        page=await context.new_page();errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        response=await page.goto(BASE+'/commerce/console')
        assert response.status==200
        await page.get_by_label('Message Skew').fill('Compare three CRM vendors for a sales team. Budget 10 USD. English table, compare pricing and integrations.')
        await page.get_by_role('button',name='Send',exact=True).click()
        await page.get_by_role('button',name='Create this task',exact=True).wait_for(timeout=40000)
        await page.get_by_role('button',name='Create this task',exact=True).click()
        await page.get_by_text('Task created',exact=True).wait_for()
        await page.screenshot(path=str(ROOT/'evidence/public-task.png'),full_page=True)
        history=await client.get(BASE+'/commerce/api/engine/assistant')
        history=await history.json();assert len(history['turns'])==1
        turn=history['turns'][0]
        assert turn['result']['trace']['model']=='qwen3-32b'
        assert turn['action_result']['payment_authority']=='NONE'
        await page.reload()
        await page.get_by_role('button',name='Open saved task',exact=True).wait_for()
        await page.get_by_role('button',name='Data licenses',exact=True).click()
        await page.get_by_text('Purchase data for your agents',exact=True).wait_for()
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.screenshot(path=str(ROOT/'evidence/public-data.png'),full_page=True)
        await page.goto(BASE+'/commerce/evidence.html')
        await page.get_by_text('Recorded settlement and delivered artifact. Read-only; no new payment.',exact=True).wait_for()
        await page.screenshot(path=str(ROOT/'evidence/public-evidence.png'),full_page=True)
        assert not errors,errors
        await client.post(BASE+'/commerce/api/auth/logout',data={},headers=headers)
        assert (await client.get(BASE+'/commerce/api/engine/assistant')).status==401
        evidence={'origin':BASE,'account_kind':'EPHEMERAL_UNFUNDED_AUTH_ONLY','authentication':True,
            'live_model_trace':turn['result']['trace'],'task_status':turn['action_result']['status'],
            'payment_authority':'NONE','reload_recovery':True,'data_layout':True,'evidence_page':True,
            'logout_revokes_session':True,'chain_writes':0,'javascript_errors':errors}
        (ROOT/'evidence/public-browser.json').write_text(json.dumps(evidence,indent=2))
        print(json.dumps(evidence))
        await browser.close()

asyncio.run(main())
