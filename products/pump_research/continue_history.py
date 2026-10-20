"""Record the owner's continuation instruction without rewriting earlier grants."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from economic_machine.pump_research.research_continuation import (
    FOLDER, PARENTS, authority, build_grant,
)
from economic_machine.pump_research.store import canonical


def exclusive(path,raw):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as f:
        f.write(raw);f.flush();os.fsync(f.fileno())


def activate(base):
    base=Path(base);folder=base/FOLDER;folder.mkdir(parents=True,exist_ok=True)
    path=folder/'grant.json';pin=path.with_suffix('.sha256')
    if path.exists() or pin.exists():
        loaded=authority(base)
        if loaded is None:raise ValueError('CONTINUATION_INCOMPLETE_AUTHORITY_PRESERVED')
        grant,digest=loaded
        return dict(existing=True,grant_sha256=digest,inventory_epochs=len(grant['inventory_epochs']))
    original={name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in PARENTS.values()}
    grant=build_grant(base);raw=(canonical(grant)+'\n').encode();digest=hashlib.sha256(raw).hexdigest()
    # A crash between the two exclusive writes leaves an explicit incomplete
    # authority, never an instruction to manufacture a new IO allowance.
    exclusive(path,raw);exclusive(pin,(digest+'\n').encode())
    authority(base)
    after={name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in PARENTS.values()}
    if original!=after:raise ValueError('CONTINUATION_PARENT_CHANGED_DURING_ACTIVATION')
    receipt=dict(schema='economic-machine.continuation-activation.v1',at_ms=time.time_ns()//1000000,
        grant_sha256=digest,original_grants=original,original_grants_unchanged=True,
        original_deadline_ms=grant['original_deadline_ms'],effective_deadline_ms=None,
        inventory_epochs=len(grant['inventory_epochs']),additional_io_cap=None,
        total_storage_bytes=grant['total_storage_bytes'],minimum_host_free_bytes=grant['minimum_host_free_bytes'],
        model=grant['model'],parallel_models=grant['parallel_models'],public_deployment=False,
        whole_history_acquired=False,services_started=False)
    exclusive(folder/'activation.json',(canonical(receipt)+'\n').encode())
    return receipt


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True)
    p.add_argument('--activate',action='store_true');a=p.parse_args()
    print(json.dumps(activate(a.base) if a.activate else build_grant(a.base)))


if __name__=='__main__':main()
