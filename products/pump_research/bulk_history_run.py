"""Private raw-history route. Uses existing spend, IO, source gate and index."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time
import urllib.error

from economic_machine.pump_research.address_index import AddressIndex,parse_index_block
from economic_machine.pump_research.archive_gate import ArchiveDeferred
from economic_machine.pump_research.bulk_history import Journal,Reader,run_window,DEADLINE_MS,status
from economic_machine.pump_research.bulk_decode import decompress as bulk_decompress
from economic_machine.pump_research.cli import proto
from economic_machine.pump_research.research_cloud import Cloud
from economic_machine.pump_research.research_continuation import ContinuationScope
from economic_machine.pump_research.store import atomic


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--policy',required=True)
    p.add_argument('--loop',action='store_true');a=p.parse_args()
    base=Path(a.base);path=Path(a.policy);raw=path.read_bytes();policy=json.loads(raw)
    if (len(raw)>65536 or path.is_symlink() or path.with_suffix('.sha256').read_text().strip()!=hashlib.sha256(raw).hexdigest()
        or policy.get('deadline_ms')!=DEADLINE_MS or policy.get('schema')!='economic-machine.bulk-history-policy.v1'
        or type(policy.get('maximum_source_bytes')) is not int or not 0<policy['maximum_source_bytes']<=8*1024**3
        or not isinstance(policy.get('windows'),list) or not 1<=len(policy['windows'])<=64):
        raise ValueError('BULK_POLICY')
    out=path.parent;lock=(out/'worker.lock').open('a+b');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scope=ContinuationScope(base);cloud=Cloud(base)
    if policy.get('continuation_sha256')!=scope.hash or policy.get('budget_sha256')!=cloud.spend.hash:
        raise ValueError('BULK_PARENT_POLICY')
    journal=Journal(out,policy);reader=Reader(journal,scope,cloud)
    index=AddressIndex(base/'evidence/wallet-map-v1/address-index.sqlite3')
    _,meta=proto('/srv/economic-machine/collector')
    def parse(raw,slot):return parse_index_block(raw,slot,meta,metadata_decompress=bulk_decompress)
    began=time.monotonic()
    try:
        while True:
            try:
                for window in policy['windows']:
                    run_window(journal,reader,index,window['first'],window['last'],parse)
                result=status(journal,reader);result.update(state='COMPLETE_APPROVED_WINDOWS',elapsed_seconds=time.monotonic()-began)
                atomic(out/'status.json',result);print(json.dumps(result));return
            except ArchiveDeferred as e:
                result=status(journal,reader);result.update(state='SOURCE_BACKOFF',retry_at_ms=e.retry_at_ms)
                atomic(out/'status.json',result)
                if not a.loop:print(json.dumps(result));return
                while time.time_ns()//1000000<e.retry_at_ms:
                    journal.guard();time.sleep(min(1,max(0,(e.retry_at_ms-time.time_ns()//1000000)/1000)))
            except urllib.error.URLError as e:
                transient=not isinstance(e,urllib.error.HTTPError) or e.code==429 or 500<=e.code<=599
                if not a.loop or not transient:raise
                # ArchiveGate has already persisted Retry-After. The next pass
                # re-enters that same gate and same job, without clearing it.
                atomic(out/('source-error-'+str(time.time_ns())+'.json'),dict(
                    error_type=type(e).__name__,http_status=getattr(e,'code',None),
                    at_ms=time.time_ns()//1000000,reservations_preserved=True))
    except BaseException as error:
        result=status(journal,reader);result.update(state='STOPPED',error_type=type(error).__name__,error=str(error)[:160])
        # Errors from our bounded source contain only public archive URLs.
        atomic(out/'status.json',result);raise
    finally:
        reader.http.close();index.close();journal.close();scope.close();cloud.close();lock.close()


if __name__=='__main__':main()
