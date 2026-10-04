"""Self-hosted read-only miner. Owner exports unsigned intents to their separate signer."""
import argparse
import json
import os
import signal
import time
from pathlib import Path

from economic_machine.values import MachineError
from machine_engine.solution_chain import ReadRPC, FinalizedChain
from machine_engine.solution_operations import LocalMiner, MiningJournal, NativePipeline


def load(path):
    source=Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_mode&0o077 or source.stat().st_size>16384:
        raise MachineError('SOLUTION_PRIVATE_CONFIG_REQUIRED')
    raw=json.loads(source.read_text())
    required={'rpc_a','rpc_b','contract','code_sha256','miner','directory','pipeline','pipeline_sha256','edge_budget','daily_edge_limit'}
    if set(raw)-required-{'cpus','working_confirmations','chain_id'} or required-set(raw):
        raise MachineError('SOLUTION_CONFIG_FIELDS')
    chain=FinalizedChain(ReadRPC(raw['rpc_a']),ReadRPC(raw['rpc_b']),raw['contract'],raw['code_sha256'],
                         working_confirmations=raw.get('working_confirmations',4),chain_id=raw.get('chain_id',421614))
    journal=MiningJournal(raw['directory'])
    pipeline=NativePipeline(raw['pipeline'],raw['pipeline_sha256'],cpus=raw.get('cpus'))
    return LocalMiner(journal,chain,pipeline,raw['miner'],budget=raw['edge_budget'],daily_limit=raw['daily_edge_limit'])


def export(journal, identifier, path):
    # O_EXCL protects against overwriting a previous handoff. State commits before export.
    destination=Path(path)
    descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        payload=journal.handoff(identifier)
        with os.fdopen(descriptor,'w') as out:
            descriptor=None;out.write(json.dumps(payload,sort_keys=True)+'\n');out.flush();os.fsync(out.fileno())
        parent=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if descriptor is not None:os.close(descriptor)
    return {'state':'HANDED_OFF','automatic_broadcast':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--watch',action='store_true');run.add_argument('--interval',type=int,default=30)
    sub.add_parser('status')
    draft=sub.add_parser('prepare');draft.add_argument('--job',required=True);draft.add_argument('--action',choices=['commit','reveal','claim'],required=True)
    hand=sub.add_parser('export');hand.add_argument('--intent',required=True);hand.add_argument('--output',required=True)
    observe=sub.add_parser('observe');observe.add_argument('--intent',required=True);observe.add_argument('--tx-hash',required=True)
    refresh=sub.add_parser('reconcile');refresh.add_argument('--intent',required=True)
    args=parser.parse_args();miner=load(args.config);stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        if args.command=='status':print(json.dumps(miner.journal.status()))
        elif args.command=='prepare':print(json.dumps(miner.prepare(args.job,args.action)))
        elif args.command=='export':
            intent=miner.journal.intent(args.intent)
            # Recheck working-chain bindings and deadline immediately before owner handoff.
            miner.prepare(intent['job'],intent['action'])
            print(json.dumps(export(miner.journal,args.intent,args.output)))
        elif args.command=='observe':
            miner.journal.record_hash(args.intent,args.tx_hash)
            print(json.dumps(miner.journal.reconcile(args.intent,miner.chain)))
        elif args.command=='reconcile':print(json.dumps(miner.journal.reconcile(args.intent,miner.chain)))
        elif args.command=='run':
            if not 10<=args.interval<=300:raise MachineError('SOLUTION_POLL_INTERVAL_BOUND')
            while not stopped:
                try:print(json.dumps(miner.tick()),flush=True)
                except MachineError as error:
                    # Deliberately do not log config, private graph/secret/calldata or provider bodies.
                    print(json.dumps({'state':'HELD','reason':str(error),'automatic_retry_of_transactions':False}),flush=True)
                if not args.watch:break
                until=time.monotonic()+args.interval
                while not stopped and time.monotonic()<until:time.sleep(max(0,min(1,until-time.monotonic())))
    finally:miner.journal.close()


if __name__=='__main__':main()
