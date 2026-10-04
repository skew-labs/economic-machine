"""Browser regression of old-card and duplicate-submit guards with fixture wallets."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':756},reduced_motion='reduce')
        await page.route('https://fixture.test/**',lambda route:route.fulfill(body='<header style="height:68px"></header><main class="content"><div id="ops-overview"></div></main>',content_type='text/html'))
        await page.goto('https://fixture.test')
        await page.add_style_tag(path=str(ROOT/'web/assistant.css'))
        await page.add_script_tag(path=str(ROOT/'web/swap-wallet.js'))
        await page.evaluate('''() => {
          const el=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
          const button=(text,cls,fn)=>{const n=el('button',cls,text);if(fn)n.onclick=fn;return n;};
          window.calls={permit:0,sign:0,submit:0};window.watchCalls=0;let quotes=0;
          window.MachineConsole={el,button,state:{identity:{address:'0x'+'1'.repeat(40)}},API_PREFIX:'',
            api:async()=>({turns:[]}),getWallet:()=>({}),setView:v=>document.body.dataset.view=v,notify:()=>{},signIn:()=>{}};
          window.WalletBridge={accountState:async()=>({chain_id:42161,address:'0x'+'1'.repeat(40)}),connectionError:e=>e.message};
          window.SkewSwapCrypto={};window.SkewSwapWallet={...window.SkewSwapWallet,validBase:()=>{},validateOrder:()=>{},
            signPermit:async()=>{calls.permit++;return 'fixture-permit';},
            signOrder:async(p,i,o,a,s,crypto,persist)=>{calls.sign++;persist({id:i.id,status:'UNKNOWN_RECONCILE_ONLY'});return 'fixture-order';}};
          window.fetch=async(url,opts)=>{const body=JSON.parse(opts.body),step=url.split('/').at(-1);
            const value=step==='quote'?{id:'fixture-'+(++quotes),preview_buy_wei:'1000000000000000',preview_fee_atoms:'1000'}:
              step==='order'?{id:body.id,minimum_buy_wei:'900000000000000'}:step==='submit'?(calls.submit++,{status:'SUBMITTED'}):
              step==='watch'?(++watchCalls===1?{status:'AWAITING_FINALITY',chain_verified:false,tracking:{active:true}}:
                {status:'FILLED_FINALIZED',chain_verified:true,proofs:[{buy_wei:'1000000000000000'}],tracking:{active:false}}):{status:'OPEN'};
            return {ok:true,json:async()=>value};};
        }''')
        await page.add_script_tag(path=str(ROOT/'web/assistant.js'))
        await page.evaluate('AssistantConsole.fuel()')
        await page.get_by_label('USDC to swap').fill('2')
        await page.get_by_role('button',name='Get live quote',exact=True).click()
        await page.get_by_role('button',name='Get live quote',exact=True).click()
        approvals=page.get_by_role('button',name='1. Approve USDC in wallet',exact=True)
        approval_box=await approvals.nth(1).bounding_box()
        composer_box=await page.locator('.chat-composer').bounding_box()
        assert approval_box['y']+approval_box['height']<=composer_box['y'],(approval_box,composer_box)
        await approvals.nth(0).click()
        await page.get_by_text('A newer quote is open. Review that quote before signing.',exact=True).wait_for()
        assert (await page.evaluate('calls')).get('permit')==0
        await approvals.nth(1).click()
        assert not await approvals.nth(1).is_enabled()
        await page.get_by_role('button',name='2. Sign and submit swap',exact=True).click()
        await page.get_by_text('Order submitted',exact=True).wait_for()
        await approvals.nth(0).click()
        await page.get_by_text('A swap is already pending. Check the original settlement before signing another.',exact=True).wait_for()
        assert await page.evaluate('calls')=={'permit':1,'sign':1,'submit':1}
        await page.get_by_text('Swap complete',exact=True).wait_for(timeout=15000)
        assert await page.evaluate('watchCalls')==2
        assert await page.evaluate('calls')=={'permit':1,'sign':1,'submit':1}
        # New page execution restores the saved completed order and receipt.
        await page.evaluate('document.getElementById("ops-overview").replaceChildren()')
        await page.add_script_tag(path=str(ROOT/'web/assistant.js'))
        await page.evaluate('AssistantConsole.connected()')
        await page.get_by_text('Swap complete',exact=True).wait_for()
        await page.evaluate('''() => {
          MachineConsole.state.identity={address:'0x'+'2'.repeat(40)};
          MachineConsole.api=async path=>path==='/api/engine/assistant'?await new Promise(resolve=>window.finishHistory=resolve):{};
          AssistantConsole.connected();
        }''')
        await page.get_by_role('button',name='Clean business data',exact=True).click()
        await page.get_by_label('CSV to clean').fill('name,email\nAlex,a@b.com')
        await page.evaluate("finishHistory({turns:[]})")
        assert await page.get_by_label('CSV to clean').input_value()=='name,email\nAlex,a@b.com'
        print(json.dumps({'fixture_only':True,'superseded_quote_rejected':True,'permit_button_consumed':True,
            'pending_swap_blocks_other_cards':True,'approval_not_obscured':True,'automatic_completion':True,
            'reload_completion_restored':True,'late_history_preserves_new_work':True,'submit_count':1,'real_signatures':0,'chain_writes':0}))
        await browser.close()

asyncio.run(main())
