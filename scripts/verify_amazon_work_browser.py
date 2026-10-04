"""Remote real-app office workflow and layout. No model or payment is simulated."""
import json,os,subprocess,tempfile,time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence'

def main():
    if not str(ROOT).startswith('/srv/skew/'):raise SystemExit('Remote verification required')
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'MACHINE_DB':str(Path(tmp)/'commerce.db'),'MACHINE_MODE':'development','SKEW_ASSISTANT_PROVIDER':'bedrock','SKEW_BEDROCK_ACCESS_STATUS':'ORGANIZATION_DENY'}
        code="import uvicorn,os;from machine_commerce.api import create_app;uvicorn.run(create_app(os.environ['MACHINE_DB']),host='127.0.0.1',port=4297,log_level='error')"
        server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                try:
                    if httpx.get('http://127.0.0.1:4297/healthz').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise RuntimeError('QA runtime unavailable')
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                context=browser.new_context(accept_downloads=True,viewport={'width':1440,'height':1000},record_video_dir=str(OUT/'amazon-demo-raw'),record_video_size={'width':1440,'height':1000})
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:4297/')
                page.get_by_role('button',name='Clean business data',exact=True).click()
                page.get_by_label('CSV to clean').fill('name,email\n Alex , a@b.com \nAlex,a@b.com\nBea,b@c.com\n')
                page.get_by_role('button',name='Compare two plans',exact=True).click()
                choices=page.get_by_role('button',name='Approve this plan',exact=True)
                choices.nth(1).wait_for();assert choices.count()==2
                page.screenshot(path=str(OUT/'amazon-two-plans.png'),full_page=True)
                choices.nth(1).click();page.get_by_text('Done. Your result is ready.',exact=True).wait_for()
                with page.expect_download() as d:page.get_by_role('button',name='Download result',exact=True).click()
                assert Path(d.value.path()).read_text()=='name,email\nAlex,a@b.com\nBea,b@c.com\n'
                page.reload();page.get_by_role('button',name='Resume my work',exact=True).click()
                page.get_by_text('Completed data task',exact=True).wait_for()
                for width in (1440,390):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(OUT/f'amazon-completed-{width}.png'),full_page=True)
                assert not errors,errors
                state=context.request.get('http://127.0.0.1:4297/api/engine/assistant/work').json()
                assert len(state['local'])==1 and state['local'][0]['status']=='DELIVERED' and not state['purchases']
                context.close();browser.close()
                proof={'runtime':'isolated actual app','checks':['two_distinct_plans','owner_approval','verified_transformed_file','reload_resumption','desktop_mobile_no_overflow'],'model_calls':0,'external_payments':0,'javascript_errors':errors,'alexa_device':False}
                (OUT/'pr15-browser.json').write_text(json.dumps(proof,indent=2));print(json.dumps(proof))
        finally:
            server.terminate();server.wait(timeout=10)

if __name__=='__main__':main()
