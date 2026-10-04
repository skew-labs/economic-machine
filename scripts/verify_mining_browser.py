"""Changed mining workflow only, real browser + API on the authorized remote host."""
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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/mining'


def main():
    if not str(ROOT).startswith('/srv/skew/'):
        raise SystemExit('Browser checks run remotely')
    parser=argparse.ArgumentParser();parser.add_argument('--public',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True);errors=[];checks=[]
    with tempfile.TemporaryDirectory() as temp:
        server=None;base='https://machine.148-113-153-116.nip.io/commerce' if args.public else 'http://127.0.0.1:4295'
        if not args.public:
            env=os.environ|{'PYTHONPATH':str(ROOT/'src'),'MINING_QA_DB':str(Path(temp)/'commerce.db')}
            code="import os,uvicorn;from machine_commerce.api import create_app;uvicorn.run(create_app(os.environ['MINING_QA_DB']),host='127.0.0.1',port=4295,log_level='error')"
            server=subprocess.Popen(['/srv/skew/economic-machine-commerce-20261002/.venv/bin/python','-c',code],env=env)
            for _ in range(100):
                try:
                    if httpx.get(base+'/healthz').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(0.1)
            else:raise RuntimeError('Isolated runtime unavailable')
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
                context=browser.new_context(accept_downloads=True)
                if args.public:login(context,base,Account.create())  # Ephemeral unfunded test identity; auth signature only.
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+('/console' if args.public else '/')+'#mining')
                page.get_by_role('button',name='Freeze work request',exact=True).wait_for()
                page.locator('#mining-title').fill('Execution route verification demo')
                with page.expect_response(lambda r:r.url.endswith('/api/engine/mining/jobs') and r.request.method=='POST') as response:
                    page.get_by_role('button',name='Freeze work request',exact=True).click()
                assert response.value.status==200,response.value.text()
                job=response.value.json();assert job['status']=='UNPUBLISHED_DRAFT' and job['confirmed_reward']=='0';checks.append('ui_to_owner_scoped_frozen_job')
                with page.expect_response(lambda r:r.url.endswith('/solve')) as response:
                    page.get_by_role('button',name='Run native solver',exact=True).click()
                result=response.value.json();assert response.value.status==200 and result['valid'] and result['path']==[1,2] and result['search_complete']
                page.get_by_text('Verified native candidate',exact=True).wait_for();checks.append('ui_to_cpp_route_search')
                with page.expect_download() as downloaded:page.get_by_role('button',name='Download candidate receipt',exact=True).click()
                downloaded.value.save_as(str(Path(temp)/'candidate.json'))
                assert json.loads((Path(temp)/'candidate.json').read_text())==result;checks.append('receipt_download_matches_api')
                page.reload();page.get_by_role('button',name='New work request',exact=True).wait_for()
                page.locator('#ops-mining .task-row').filter(has_text='Execution route verification demo').click()
                page.get_by_text('Verified native candidate',exact=True).wait_for();checks.append('owner_job_persists_after_reload')
                for width in [1440,390]:
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth')
                    page.screenshot(path=str(OUT/f"mining-{width}-{'public' if args.public else 'isolated'}.jpg"),full_page=True)
                    checks.append(f'responsive_{width}')
                assert not errors,errors
                browser.close()
        finally:
            if server:server.terminate();server.wait(timeout=10)
    report={'scope':'PUBLIC_OWNER_DRAFT_AND_CPP_NO_CHAIN_TX' if args.public else 'ISOLATED_REAL_UI_API_CPP_NO_CHAIN_TX','checks':checks,'javascript_errors':errors,'financial_transactions':0,'new_mining_deployment':None}
    (OUT/('browser-public.json' if args.public else 'browser-isolated.json')).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
