"""Import the already authorized, downloaded epoch without more network IO."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time

from economic_machine.pump_research.gsfa_index import Budget, GsfaIndex, inspect_download
from economic_machine.pump_research.history_core import Scope
from economic_machine.pump_research.research_extension import extension
from economic_machine.pump_research.store import atomic, address


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',required=True)
    parser.add_argument('--inspection',required=True)
    parser.add_argument('--targets',required=True)
    args=parser.parse_args()
    base=Path(args.base);folder=base/'evidence/address-epoch-v1'
    scope=Scope(base,base/'evidence/core-trace-v1/grant.json')
    lock=(folder/'import.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    status=dict(state='CHECKING_EXISTING_SCOPE',started_ms=time.time_ns()//1000000,
        deadline_ms=scope.grant['deadline_ms'],network_requests=0,index_query_verified=False,
        address_membership_verified=False,independent_consensus_verified=False)
    status_path=folder/'import-status.json'
    try:
        grant=extension(base,scope.hash,scope.grant['deadline_ms'])
        if not grant or grant['epochs']!=[685]:raise ValueError('GSFA_IMPORT_EPOCH_SCOPE')
        manifest=folder/'download-manifest.json';download=json.loads(manifest.read_bytes())
        inspected=json.loads(Path(args.inspection).read_bytes())
        if (not inspected.get('format_inspected') or not inspected.get('downloaded_chunk_hashes_verified')
            or inspected.get('compressed_sha256')!=download['sha256']):
            raise ValueError('GSFA_IMPORT_INSPECTION_REQUIRED')
        allowance=inspected['decompressed_tar_bytes']+1024**2
        if type(allowance) is not int:raise ValueError('GSFA_IMPORT_EXPANSION_BOUND')
        scope.guard(inspected['member_bytes']+128*1024**2,force=True)
        budget=Budget(scope.grant['deadline_ms'],allowance,scope.guard)
        destination=folder/'gsfa-index-685'
        status.update(state='EXTRACTING',member_bytes=inspected['member_bytes'],
            destination=destination.name,source_sha256=download['sha256'])
        atomic(status_path,status)
        report=inspect_download(manifest,budget,destination)
        if report['files']!=inspected['files']:raise ValueError('GSFA_IMPORT_MEMBER_IDENTITY_CHANGED')
        atomic(folder/'extraction-receipt.json',dict(report,at_ms=time.time_ns()//1000000))
        status.update(state='QUERYING_ACTUAL_INDEX',expanded_bytes=budget.expanded)
        atomic(status_path,status)
        target=json.loads(Path(args.targets).read_bytes())
        wallets=list(dict.fromkeys([target['root'],*[r['address'] for r in target['targets'] if address(r['address'])]]))
        if len(wallets)>32:raise ValueError('GSFA_IMPORT_QUERY_BOUND')
        started=time.monotonic();queries=[]
        with GsfaIndex(destination,685,budget) as index:
            identity=index.identity;root_cid=index.root_cid
            for wallet in wallets:
                page=index.page(wallet,limit=8)
                queries.append(page)
        result=dict(schema='economic-machine.gsfa-import.v1',at_ms=time.time_ns()//1000000,
            epoch=685,index_identity=identity,root_cid=root_cid,directory=destination.name,
            source_sha256=download['sha256'],extraction=report,queries=queries,
            query_elapsed_ms=(time.monotonic()-started)*1000,index_query_verified=True,
            address_membership_verified=False,independent_consensus_verified=False,
            complete_address_history=False,network_requests=0)
        atomic(folder/'import-receipt.json',result)
        status.update(state='ADDRESS_CANDIDATE_INDEX_READY',index_query_verified=True,
            index_identity=identity,queried_addresses=len(queries),candidate_locations=sum(len(p['candidates']) for p in queries))
    except BaseException as error:
        status.update(state='IMPORT_BLOCKED',error_type=type(error).__name__,error=str(error)[:160],
            partial_extraction_preserved=True)
        raise
    finally:
        status['updated_ms']=time.time_ns()//1000000
        atomic(status_path,status);scope.close();lock.close()


if __name__=='__main__':main()
