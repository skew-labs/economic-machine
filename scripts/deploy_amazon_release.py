"""Remote-only source release. Does not sign, pay, mint, or modify Fuel service."""
import hashlib,json,os,shutil,subprocess,time
import urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LIVE=Path('/srv/skew/economic-machine-commerce-20261002')

def main():
    if not str(ROOT).startswith(str(LIVE/'build/')):raise SystemExit('Authorized isolated remote build required')
    manifest=json.loads((ROOT/'evidence/release-source.json').read_text())
    for name,sha in manifest['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith(('src/','web/','contracts/','scripts/')):raise RuntimeError('Unexpected source path')
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha:raise RuntimeError('Untested source drift: '+name)
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
    backup=LIVE/'releases'/('amazon-backup-'+stamp)
    for name in manifest['files']:
        old=LIVE/name
        if old.is_file():
            dest=backup/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,dest)
    # Private configuration is never included in release artifacts or logs.
    envpath=Path('/etc/machine-commerce/bedrock.env')
    if not envpath.is_file():raise RuntimeError('Private Bedrock configuration missing')
    lines=[line for line in envpath.read_text().splitlines() if not line.startswith('SKEW_BEDROCK_ACCESS_STATUS=')]
    lines.append('SKEW_BEDROCK_ACCESS_STATUS=ORGANIZATION_DENY')
    envpath.write_text('\n'.join(lines)+'\n');envpath.chmod(0o600)
    runtime=LIVE/'releases/amazon-20261004/venv'
    if not runtime.exists():shutil.copytree(ROOT/'.venv',runtime,symlinks=True)
    subprocess.run([str(runtime/'bin/python'),'-c','import machine_commerce.api'],env={**os.environ,'PYTHONPATH':str(LIVE/'src')},check=True)
    drop=Path('/etc/systemd/system/machine-commerce-sepolia-runtime.service.d/zz-amazon.conf')
    drop.parent.mkdir(parents=True,exist_ok=True)
    drop.write_text('[Service]\nEnvironment=PYTHONPATH=/srv/skew/economic-machine-commerce-20261002/src\nEnvironmentFile=/etc/machine-commerce/bedrock.env\nUnsetEnvironment=SKEW_ASSISTANT_API_KEY SKEW_ASSISTANT_BASE_URL SKEW_ASSISTANT_MODEL\nExecStart=\nExecStart='+str(runtime/'bin/python')+' -m uvicorn machine_commerce.api:app_factory --factory --host 127.0.0.1 --port 4261 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log\n')
    subprocess.run(['systemctl','stop','machine-commerce-sepolia-runtime','machine-commerce-portal'],check=True)
    try:
        for name in manifest['files']:
            target=LIVE/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
        (LIVE/'artifacts/datapass-mining').mkdir(parents=True,exist_ok=True)
        for name in ('contracts.json','build.json'):
            shutil.copy2(ROOT/'artifacts/datapass-mining'/name,LIVE/'artifacts/datapass-mining'/name)
    finally:
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','start','machine-commerce-sepolia-runtime','machine-commerce-portal'],check=True)
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:4261/healthz',timeout=2) as response:
                if response.status==200:break
        except OSError:pass
        time.sleep(1)
    else:raise RuntimeError('Release health check failed; restore the saved source/unit before retrying')
    proof={'source_commit':manifest['commit'],'files':manifest['files'],'backup':str(backup),
           'provider':'bedrock','model_access':'ORGANIZATION_DENY','qwen_runtime_credentials_unset':True,
           'mainnet_deployed':False,'signed_transactions':0,'fuel_service_modified':False}
    (ROOT/'evidence/deployment.json').write_text(json.dumps(proof,indent=2))
    print(json.dumps({k:v for k,v in proof.items() if k!='files'}))

if __name__=='__main__':main()
