"""Only new solution-research browser/API connections; no chain transactions or paid inference."""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from eth_account import Account
from playwright.sync_api import sync_playwright
from verify_tasks_browser import login

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote browser required')
    p=argparse.ArgumentParser();p.add_argument('--public',action='store_true');args=p.parse_args();out=ROOT/'artifacts/solution';out.mkdir(exist_ok=True)
    errors=[];checks=[]
    with tempfile.TemporaryDirectory() as temp:
        server=None;base='https://machine.148-113-153-116.nip.io/commerce' if args.public else 'http://127.0.0.1:4296'
        if not args.public:
            env=os.environ|{'PYTHONPATH':str(ROOT/'src'),'SOLUTION_QA_DB':str(Path(temp)/'workspace.db')}
            code="import os,uvicorn;from machine_commerce.api import create_app;uvicorn.run(create_app(os.environ['SOLUTION_QA_DB']),host='127.0.0.1',port=4296,log_level='error')"
            server=subprocess.Popen(['/srv/skew/economic-machine-commerce-20261002/.venv/bin/python','-c',code],env=env)
            for _ in range(100):
                try:
                    if httpx.get(base+'/healthz').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(0.1)
            else:raise RuntimeError('Isolated runtime startup failed')
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
                context=browser.new_context(accept_downloads=True)
                if args.public:login(context,base,Account.create())
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+('/console' if args.public else '/')+'#mining')
                page.get_by_role('button',name='Solution research',exact=True).click()
                page.get_by_role('button',name='Search and verify',exact=True).wait_for();checks.append('research_mode_same_console')
                with page.expect_response(lambda r:r.url.endswith('/solution/evaluate')) as response:
                    page.get_by_role('button',name='Search and verify',exact=True).click()
                assert response.value.status==200,response.value.text();result=response.value.json()
                assert result['confirmed_reward']=='0' and result['language_model_calls']==0 and result['randomness_assurance']=='CALLER_SEED_RESEARCH_FIXTURE_NOT_VRF';checks.append('native_search_no_emission_or_paid_ai')
                page.get_by_text('Verified candidate',exact=True).wait_for()
                with page.expect_download() as download:page.get_by_role('button',name='Download solution receipt',exact=True).click()
                download.value.save_as(str(Path(temp)/'receipt.json'));assert json.loads((Path(temp)/'receipt.json').read_text())==result;checks.append('download_exact_candidate_receipt')
                page.locator('#solution-algorithm').select_option('greedy');page.locator('#solution-problem').fill('1')
                with page.expect_response(lambda r:r.url.endswith('/solution/evaluate')) as response:page.get_by_role('button',name='Search and verify',exact=True).click()
                assert response.value.status==200 and response.value.json()['problem']=='1';checks.append('switch_worker_and_problem')
                page.get_by_text('Verified candidate',exact=True).wait_for()
                assert page.locator('#solution-problem').input_value()=='1' and page.locator('#solution-algorithm').input_value()=='greedy';checks.append('submitted_conditions_preserved_after_result')
                for width in [1440,390]:
                    page.set_viewport_size({'width':width,'height':1000});assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth')
                    page.screenshot(path=str(out/f"solution-{width}-{'public' if args.public else 'isolated'}.jpg"),full_page=True);checks.append(f'responsive_{width}')
                assert not errors,errors;browser.close()
        finally:
            if server:server.terminate();server.wait(timeout=10)
    proof={'scope':'PUBLIC_RESEARCH_CPP_UI_NO_CHAIN_TX' if args.public else 'ISOLATED_RESEARCH_CPP_UI','checks':checks,'javascript_errors':errors,'live_issuance':False,'financial_transactions':0,'vrf':'NOT_CONFIGURED'}
    (out/('browser-public.json' if args.public else 'browser-isolated.json')).write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))


if __name__=='__main__':main()
