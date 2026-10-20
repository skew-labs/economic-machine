"""Bounded two-consumer pipeline with a single, ordered search publisher.

Acquisition retains the original source writer, ledger and provider gate.
Workers never register shards: only the coordinator can publish a verified
contiguous prefix. The measured clock includes acquisition, startup and drain.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import multiprocessing
from pathlib import Path
import queue
import subprocess
import sys
import time

from freezer_pipeline import pages, produce
from economic_machine.arbitrum_research.freezer_source import DEADLINE
from economic_machine.arbitrum_research.freezer_search import register
from economic_machine.pump_research.store import atomic, canonical


def consumer_count(value):
    if type(value) is not int or value not in (2, 3):
        raise ValueError('PIPELINE_CONSUMER_BOUND')
    return value


def produce_pipelined(config,q,slots,stop):
    from economic_machine.arbitrum_research.freezer_adaptive import produce
    produce(config,q,slots,stop)


class OrderedResults:
    def __init__(self,out,start,end,bound=4):
        if bound!=4:raise ValueError('PIPELINE_INFLIGHT_BOUND')
        self.out=Path(out);self.end=end;self.next_admit=start;self.next_commit=start
        self.bound=bound;self.admitted=self.committed=0;self.pending=[]
    def admit(self,item,launch):
        n=item.get('start');count=item.get('count')
        if len(self.pending)>=self.bound or n!=self.next_admit or type(count) is not int or not 1<=count<=16384 or n+count-1>self.end:
            raise ValueError('PIPELINE_ADMISSION_BOUND')
        folder=self.out/f'window-{n}-{n+count-1}'
        if item!=dict(start=n,count=count,folder=str(folder)):raise ValueError('PIPELINE_OUT_OF_ORDER')
        future=launch(item);self.pending.append((item,future));self.admitted+=1;self.next_admit+=count
    def ready(self):
        for _,future in self.pending:
            if future.done() and future.exception() is not None:future.result()
        if self.pending and self.pending[0][1].done():return self.pending[0][0],self.pending[0][1].result()
        return None
    def commit(self,registration):
        if not self.pending or not self.pending[0][1].done():raise ValueError('PIPELINE_UNVERIFIED_COMMIT')
        item,future=self.pending[0];future.result()
        if item['start']!=self.next_commit or registration['first']!=item['start'] or registration['blocks']!=item['count']:
            raise ValueError('PIPELINE_REGISTRATION_SCOPE')
        self.pending.pop(0);self.next_commit+=item['count'];self.committed+=1
    def complete(self):return self.next_commit==self.end+1
    def admissions_complete(self):return self.next_admit==self.end+1


def consume(config, item):
    began = time.monotonic()
    folder = Path(item['folder'])
    if time.time_ns() // 1000000 >= DEADLINE:
        raise ValueError('PIPELINE_DEADLINE')
    if not (folder / 'verification.json').exists():
        command = [sys.executable, str(Path(__file__).with_name('freezer_probe.py')),
                   '--base', config['base'], '--out', str(folder),
                   '--chain', str(config['chain']), '--start', str(item['start']),
                   '--count', str(item['count']), '--binary', config['binary'],
                   '--source-cache', config['source_cache'], '--read-ahead', '--prepared']
        if config.get('source_cap_authorization'):
            auth=config['source_cap_authorization']
            command.extend(['--source-authorization',auth['path'],'--source-authorization-sha',auth['sha256']])
        timeout = min(900, max(1, (DEADLINE - time.time_ns() // 1000000) / 1000))
        run = subprocess.run(command, capture_output=True, timeout=timeout)
        atomic(folder / f'consumer-attempt-{time.time_ns() // 1000000}.json',
               dict(returncode=run.returncode, stderr_tail=run.stderr.decode(errors='replace')[-1200:]))
        if run.returncode:
            raise ValueError('PIPELINE_CONSUMER_FAILED')
    return dict(consumer_seconds=time.monotonic() - began,
                proof=json.loads((folder / 'verification.json').read_bytes()))


def main():
    parser = argparse.ArgumentParser()
    for key in ('base', 'out', 'binary', 'source-cache'):
        parser.add_argument('--' + key, required=True)
    for key in ('chain', 'start', 'end'):
        parser.add_argument('--' + key, type=int, required=True)
    parser.add_argument('--source-pipeline', action='store_true')
    parser.add_argument('--source-authorization')
    parser.add_argument('--source-authorization-sha')
    parser.add_argument('--consumers',type=int,default=2)
    args = parser.parse_args()
    consumer_count(args.consumers)
    pages(args.start, args.end)
    if args.chain not in (42161, 4663) or time.time_ns() // 1000000 >= DEADLINE:
        raise ValueError('PIPELINE_CHAIN_OR_DEADLINE')
    if not args.source_pipeline or args.chain!=42161:raise ValueError('ADAPTIVE_PIPELINE_SCOPE')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'worker.lock').open('a+b')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    policy = dict(version=2, chain=args.chain, start=args.start, end=args.end,
                  page_blocks=16384, deadline_ms=DEADLINE,
                  source_cache=str(Path(args.source_cache).resolve()), source_cap_bytes=1024**3,
                  binary_sha256=hashlib.sha256(Path(args.binary).read_bytes()).hexdigest(),
                  max_inflight_windows=4, source_writers=1, search_consumers=2,
                  ordered_search_publishers=1)
    if args.source_pipeline:
        policy.update(version=3, source_blob_workers=2, index_read_ahead_bytes=1024**2)
    if args.source_authorization or args.source_authorization_sha:
        if not args.source_pipeline or args.chain!=42161 or not args.source_authorization or not args.source_authorization_sha:
            raise ValueError('PIPELINE_SOURCE_AUTHORIZATION_ARGUMENT')
        import sqlite3
        from economic_machine.arbitrum_research.freezer_authority import source_authority
        auth=dict(path=str(Path(args.source_authorization).resolve()),sha256=args.source_authorization_sha)
        db=sqlite3.connect('file:'+str(Path(args.source_cache)/'source.sqlite3')+'?mode=ro',uri=True)
        try:source_authority(auth,args.source_cache,db.execute('select body from identity').fetchone()[0])
        finally:db.close()
        policy.update(version=4,source_cap_bytes=None,source_cap_authorization=auth)
    if args.consumers != 2:
        policy.update(version=5,search_consumers=args.consumers)
    policy.update(version=6,page_mode='byte_bounded_v1',max_native_input_bytes=60*1024**2)
    if (out / 'policy.json').exists():
        if json.loads((out / 'policy.json').read_bytes()) != policy:
            raise ValueError('PIPELINE_POLICY_CHANGED')
    else:
        atomic(out / 'policy.json', policy)
    sha = hashlib.sha256(canonical(policy).encode()).hexdigest()
    begin = time.monotonic()
    started = time.time_ns() // 1000000
    status = dict(state='RUNNING', started_at_ms=started, next_block=args.start,
                  indexed_blocks=0, windows=[], resumed=False, full_mapping_complete=False)
    if (out / 'status.json').exists():
        status = json.loads((out / 'status.json').read_bytes())
        if status['state'] == 'COMPLETE_BOUNDED_WINDOW':
            raise ValueError('PIPELINE_ALREADY_COMPLETE')
        status['resumed'] = True
    atomic(out / 'status.json', status)
    ctx = multiprocessing.get_context('spawn')
    q = ctx.Queue(maxsize=2)
    slots = ctx.BoundedSemaphore(4)
    stop = ctx.Event()
    config = dict(base=args.base, out=str(out), policy=policy, policy_sha256=sha,
                  next_block=status['next_block'])
    consumer_config = dict(base=args.base, chain=args.chain, binary=args.binary,
                           source_cache=args.source_cache,source_cap_authorization=policy.get('source_cap_authorization'))
    ordered = OrderedResults(out, status['next_block'], args.end)
    producer = ctx.Process(target=produce_pipelined if args.source_pipeline else produce,
                           args=(config, q, slots, stop))
    executor = ThreadPoolExecutor(max_workers=args.consumers)
    producer.start()
    source_done = False
    try:
        while not ordered.complete() or not source_done:
            if time.time_ns() // 1000000 >= DEADLINE:
                raise ValueError('PIPELINE_DEADLINE')
            ready = ordered.ready()
            if ready:
                item, result = ready
                n, count = item['start'], item['count']
                before_registration = time.monotonic()
                registration = register(args.base, item['folder'])
                ordered.commit(registration)
                proof = result['proof']
                status['windows'].append(dict(start=n, end=n + count - 1, blocks=count,
                    consumer_seconds=result['consumer_seconds'], source_seconds=proof['source_seconds'],
                    registration_seconds=time.monotonic() - before_registration,
                    network_bytes=proof['network_bytes'], network_requests=proof['network_requests'],
                    index_sha256=registration['index_sha256'], registered_at_ms=time.time_ns() // 1000000))
                status.update(next_block=n + count, indexed_blocks=status['indexed_blocks'] + count,
                              at_ms=time.time_ns() // 1000000, elapsed_seconds=time.monotonic() - begin)
                denominator = (status['at_ms'] - status['started_at_ms']) / 1000 if status['resumed'] else status['elapsed_seconds']
                status['blocks_per_second'] = status['indexed_blocks'] / denominator
                atomic(out / 'status.json', status)
                slots.release()
                continue
            if not source_done and len(ordered.pending) < ordered.bound:
                try:
                    item = q.get(timeout=0.05)
                except queue.Empty:
                    if not producer.is_alive():
                        raise ValueError('PIPELINE_PRODUCER_EXITED')
                else:
                    if 'error_type' in item:
                        raise ValueError('PIPELINE_INCOMPLETE_SOURCE')
                    if item.get('done'):
                        if not ordered.admissions_complete():
                            raise ValueError('PIPELINE_INCOMPLETE_SOURCE')
                        source_done = True
                    else:
                        ordered.admit(item, lambda value: executor.submit(consume, consumer_config, value))
            else:
                time.sleep(0.01)
        producer.join(timeout=10)
        if producer.is_alive() or producer.exitcode != 0:
            raise ValueError('PIPELINE_SOURCE_DRAIN_FAILED')
        executor.shutdown(wait=True)
        status.update(state='COMPLETE_BOUNDED_WINDOW', at_ms=time.time_ns() // 1000000,
                      elapsed_seconds=time.monotonic() - begin)
        denominator = (status['at_ms'] - status['started_at_ms']) / 1000 if status['resumed'] else status['elapsed_seconds']
        status['blocks_per_second'] = status['indexed_blocks'] / denominator
        status['measurement_scope'] = 'source_acquisition_through_verified_blob_graph_registered_search_including_startup_and_drain'
        atomic(out / 'status.json', status)
    except Exception as error:
        status.update(state='STOPPED_REVIEW_REQUIRED', at_ms=time.time_ns() // 1000000,
                      error_type=type(error).__name__, error=str(error)[:200])
        atomic(out / 'status.json', status)
        raise
    finally:
        stop.set()
        for _, future in ordered.pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        producer.join(timeout=30)
        if producer.is_alive():
            producer.terminate()
            producer.join(timeout=10)
        q.close()


if __name__ == '__main__':
    main()
