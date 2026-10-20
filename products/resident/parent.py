"""Adapter for the user's large model / agent framework (standard library only).

Supply a task JSON on stdin. The supervising application owns the local bearer
file; do not place it or model keys in prompts. No cloud provider is assumed.
"""
import argparse
import json
import sys
from pathlib import Path
import urllib.request
import urllib.parse

ap=argparse.ArgumentParser()
ap.add_argument('operation',choices=['delegate','status','results'])
ap.add_argument('--token-file',required=True)
ap.add_argument('--port',type=int,default=18433)
ap.add_argument('--agent',default='agent-000')
ap.add_argument('--after',type=int,default=0)
args=ap.parse_args()
if not 1024<=args.port<=65535: raise SystemExit('port')
body=None
if args.operation=='delegate':
    raw=sys.stdin.buffer.read(4097)
    if len(raw)>4096: raise SystemExit('input too large')
    body=json.dumps(json.loads(raw)).encode()
path={'delegate':'/v1/delegate','status':'/v1/status',
      'results':'/v1/results?agent='+urllib.parse.quote(args.agent)+'&after='+str(args.after)}[args.operation]
token=Path(args.token_file).read_text().strip()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
request=urllib.request.Request(f'http://127.0.0.1:{args.port}'+path,body,
    {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
with opener.open(request,timeout=5) as response:
    print(response.read(65536).decode())
