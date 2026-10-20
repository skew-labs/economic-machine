"""Corroborate imported GSFA locations using only existing cached CARs."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import time

from economic_machine.pump_research.archive import BASE
from economic_machine.pump_research.address_index import AddressIndex,parse_index_block
from economic_machine.pump_research.cli import proto
from economic_machine.pump_research.gsfa_history import GsfaHistory,corroborate_candidate
from economic_machine.pump_research.gsfa_index import GsfaIndex,Budget,MIB
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.store import atomic,address
from economic_machine.pump_research.raw_history import ScopedArchive,ArchiveDeferred


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--targets',required=True)
    p.add_argument('--fetch-one',action='store_true');a=p.parse_args()
    base=Path(a.base);out=base/'evidence/wallet-map-v1';folder=base/'evidence/address-epoch-v1'
    scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    report=dict(at_ms=time.time_ns()//1000000,network_requests=0,model_calls=0,candidates_queried=0,
        verified=[],index_pages=[],complete_address_history=False,independent_consensus_verified=False)
    try:
        _,meta=proto('/srv/economic-machine/collector')
        targets=json.loads(Path(a.targets).read_bytes())
        wallets=list(dict.fromkeys(r['address'] for r in targets['targets'] if address(r.get('address'))))[:6]
        if a.fetch_one:
            before=scope.reserved();address_index=AddressIndex(out/'address-index.sqlite3')
            history=None
            try:
                with GsfaIndex(folder/'gsfa-index-685',685,Budget(scope.grant['deadline_ms'],64*MIB,scope.guard)) as index:
                    reader=ScopedArchive(SimpleNamespace(base=base,out=out),scope,None,meta,address_index,wait=False)
                    history=GsfaHistory(out/'gsfa-probe.sqlite3',index,reader,scope.guard)
                    job=history.register(wallets[0],dict(source='existing_epoch_target_queue',epoch=685))
                    try:report['step']=history.step(job,page_size=8,max_candidates=1)
                    except ArchiveDeferred as error:report['deferred_until_ms']=error.retry_at_ms
                    report['raw_results']=history.results(job,after_id=0,limit=8)
                    report['io_reserved_delta']=scope.reserved()-before
                    report.pop('network_requests')
                    report['network_policy']='Existing metered archive cache and original research IO allowance; at most one candidate.'
                atomic(folder/f'GSFA-CAR-FETCH-PROBE-{report["at_ms"]}.json',report)
                print(json.dumps(report))
                return
            finally:
                if history:history.close()
                address_index.close()
        with closing(sqlite3.connect(f'file:{out}/address-index.sqlite3?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            with GsfaIndex(folder/'gsfa-index-685',685,Budget(scope.grant['deadline_ms'],64*MIB,scope.guard)) as index:
                report['index_identity']=index.identity
                for wallet in wallets:
                    cursor=None;found=False
                    for page_number in range(32):
                        scope.guard();page=index.page(wallet,limit=32,cursor=cursor)
                        report['candidates_queried']+=len(page['candidates'])
                        report['index_pages'].append(dict(wallet=wallet,page=page_number,candidates=len(page['candidates'])))
                        for candidate in page['candidates']:
                            for row in db.execute("SELECT * FROM blocks WHERE slot=? AND state!='dead_branch'",(candidate['slot'],)):
                                receipt=json.loads(row['receipt']);start=receipt.get('car_offset');size=receipt.get('car_size')
                                if type(start) is not int or type(size) is not int or not 0<size<=16*MIB:continue
                                url=f'{BASE}/685/epoch-685.car'
                                ident=hashlib.sha256(f'{url}:{start}:{start+size-1}'.encode()).hexdigest()
                                path=out/'archive-ranges'/(ident+'.range');mpath=path.with_suffix('.json')
                                if not path.exists() or not mpath.exists():continue
                                manifest=json.loads(mpath.read_bytes())
                                if manifest.get('sha256')!=row['hash'] or manifest.get('bytes')!=size:raise ValueError('PROBE_CACHE_MANIFEST')
                                if path.is_symlink() or path.stat().st_size!=size:raise ValueError('PROBE_CACHE_SIZE')
                                raw=path.read_bytes()
                                if hashlib.sha256(raw).hexdigest()!=row['hash']:raise ValueError('PROBE_CACHE_HASH')
                                block=parse_index_block(raw,row['slot'],meta)
                                proof=corroborate_candidate(wallet,candidate,block,dict(receipt,raw=raw),meta)
                                report['verified'].append(dict(wallet=wallet,candidate=candidate,car_sha256=row['hash'],proof=proof))
                                found=True;break
                            if found:break
                        cursor=page['next_cursor']
                        if found or cursor is None:break
        report['successful_corroborations']=sum(r['proof']['state']=='verified_success' for r in report['verified'])
        report['raw_failed_transactions']=sum(r['proof']['state']=='verified_failed_transaction' for r in report['verified'])
        report['used_bytes']=scope.used;report['io_reserved_bytes']=scope.reserved()
        atomic(folder/f'GSFA-CAR-PROBE-{report["at_ms"]}.json',report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('verified','index_pages')}))
    finally:scope.close()


if __name__=='__main__':main()
