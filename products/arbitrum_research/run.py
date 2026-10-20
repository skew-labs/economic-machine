import argparse
import json
from pathlib import Path
import signal

from economic_machine.arbitrum_research.authority import activate
from economic_machine.arbitrum_research.runtime import Runtime

def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    p.add_argument('--shared-base',required=True);p.add_argument('--model-key-file')
    p.add_argument('--activate',action='store_true');p.add_argument('--apply-resource-policy',action='store_true')
    p.add_argument('--max-pages',type=int);args=p.parse_args()
    if args.activate:
        value,digest=activate(args.base,args.shared_base)
        print(json.dumps(dict(grant_sha256=digest,model=value['model'],investigators=value['investigators'],activated=True)))
        return
    if args.max_pages is not None and args.max_pages<1:p.error('max-pages must be positive')
    if not args.model_key_file:p.error('model key file required')
    runtime=Runtime(args.base,args.shared_base,args.model_key_file,resource_apply=args.apply_resource_policy)
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:setattr(runtime,'stop',True))
    try:runtime.run(args.max_pages)
    finally:runtime.close()

if __name__=='__main__':main()
