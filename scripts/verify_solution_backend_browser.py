"""Focused public console -> sandboxed native process -> exact receipt integration."""
import json
import tempfile
from pathlib import Path
from eth_account import Account
from playwright.sync_api import sync_playwright
from verify_tasks_browser import login

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote browser only')
    base='https://machine.148-113-153-116.nip.io/commerce';errors=[]
    build=json.loads((ROOT/'artifacts/solution-operations/native-build.json').read_text())
    with tempfile.TemporaryDirectory() as temp,sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell')
        context=browser.new_context(accept_downloads=True);login(context,base,Account.create())
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+'/console#mining');page.get_by_role('button',name='Solution research',exact=True).click()
        with page.expect_response(lambda response:response.url.endswith('/solution/evaluate')) as response:
            page.get_by_role('button',name='Search and verify',exact=True).click()
        assert response.value.status==200,response.value.text()
        result=response.value.json()
        assert result['execution_backend']=='SANDBOXED_CPP_PIPELINE' and result['pipeline_sha256']==build['sha256']
        assert not result['global_optimum_proven'] and result['confirmed_reward']=='0' and result['chain_transaction'] is None
        page.get_by_text('Verified candidate',exact=True).wait_for()
        with page.expect_download() as download:page.get_by_role('button',name='Download solution receipt',exact=True).click()
        path=Path(temp)/'receipt.json';download.value.save_as(str(path));assert json.loads(path.read_text())==result
        assert not errors;browser.close()
    proof={'scope':'PUBLIC_CHANGED_BACKEND_CONNECTION_ONLY_NOT_REPEATED_RESPONSIVE_SUITE',
        'checks':['console_calls_sandboxed_cpp','installed_executable_hash_matches_receipt','download_exact_receipt_without_issuance'],
        'javascript_errors':errors,'pipeline_sha256':build['sha256'],'chain_transactions':0,'language_model_calls':0}
    (ROOT/'artifacts/solution-operations/browser-backend.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))


if __name__=='__main__':main()
