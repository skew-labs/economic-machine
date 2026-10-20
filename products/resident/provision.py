"""Operator-only provisioning. Does not deploy services or buy infrastructure."""
import argparse
import os
import secrets
from pathlib import Path
from economic_machine.resident.store import Store

ap=argparse.ArgumentParser()
ap.add_argument('--state',required=True)
args=ap.parse_args()
os.umask(0o077)
root=Path(args.state); root.mkdir(parents=True,exist_ok=True)
for name in ('parent.token','model.token'):
    token=root/name
    if not token.exists():
        with token.open('x') as f: f.write(secrets.token_urlsafe(32))
store=Store(root/'runtime.sqlite')
store.provision([{'id':f'agent-{i:03d}','venue':f'venue-{i%10:02d}',
                  'tools':['triage','note','remind','signal']} for i in range(100)])
print('Provisioned 100 logical agents; no live venue credentials or trading authority.')
