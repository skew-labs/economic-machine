#!/usr/bin/env python3
"""Human-run replacement of this same Pacifica session; --check is read-only.

--apply may stop a financial process and start its replacement. Assistant tools
must not invoke it. No wallet/provider secret is read by this helper itself.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import sys
import time

BASE=Path('/srv/economic-machine/receiver')
LIVE=BASE/'muse-maker-live-20261015-a'
RELEASE=BASE/'muse-maker-release-20261015-b'
OLD_RELEASE=BASE/'muse-maker-release-20261015-a'
OBSERVER=BASE/'ticking-sol-mm-runtime'


def classify(argv, root=LIVE):
    if not argv:return None
    def option(name):
        if argv.count(name)!=1:return None
        i=argv.index(name);return argv[i+1] if i+1<len(argv) else None
    if Path(argv[0]).name=='muse-maker':
        return 'native' if len(argv)==6 and argv[1]=='--live' and argv[2]==str(root) else None
    if option('--root')!=str(root):return None
    if len(argv)>2 and Path(argv[0]).name.startswith('python'):
        if argv[1]=='-m' and argv[2]=='economic_machine.muse_policy_worker':return 'worker'
        scripts={'products/pacifica_muse/owner_start.py',str(OLD_RELEASE/'source/products/pacifica_muse/owner_start.py'),str(RELEASE/'source/products/pacifica_muse/owner_start.py')}
        if argv[1] in scripts and '--live' in argv:return 'supervisor'
    return None


def processes():
    result=[]
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid!=os.geteuid():continue
            argv=[x.decode() for x in (proc/'cmdline').read_bytes().split(b'\0') if x]
            kind=classify(argv)
            if kind:result.append({'pid':int(proc.name),'kind':kind,'start_tick':int((proc/'stat').read_text().rpartition(')')[2].split()[19])})
        except (OSError,UnicodeError):continue
    return result


def scope(root, now):
    path=root/'session-start.json';raw=path.read_bytes();lease=json.loads(raw)
    if lease.get('live') is not True or lease['deadline_ms']-lease['start_ms']!=172800000 or not lease['start_ms']<=now<lease['deadline_ms']:
        raise ValueError('original live session is not current; no new session will be created')
    db=sqlite3.connect((root/'control/model.sqlite').as_uri()+'?mode=ro',uri=True)
    try:
        body=db.execute('SELECT body FROM scope WHERE id=1').fetchone()[0];budget=json.loads(body)
        if budget['session_id']!=lease['session_id'] or budget['deadline_ms']!=lease['deadline_ms']:
            raise ValueError('model and trading session differ')
        calls=db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
        if calls>=budget['max_calls']:raise ValueError('existing model call cap exhausted; it will not be reset')
    finally:db.close()
    return {'lease_sha256':hashlib.sha256(raw).hexdigest(),'budget_scope_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'session_id':lease['session_id'],'deadline_ms':lease['deadline_ms'],'calls_reserved':calls,
            'max_calls':budget['max_calls'],'model_budget_usd':budget['maximum_usd']}


def verify_release(root):
    manifest=json.loads((root/'evidence/release.json').read_text())
    if manifest.get('per_side_usd')!=600 or manifest.get('aggregate_usd')!=1200 or manifest.get('ttl_ms')!=60000:
        raise ValueError('release settings differ from owner request')
    files=manifest['sha256']
    required={'bin/muse-maker','source/products/pacifica_muse/owner_start.py','source/products/pacifica_muse/owner_replace.py',
              'source/src/economic_machine/muse_maker.py','source/src/economic_machine/muse_policy_worker.py'}
    if not required<=set(files):raise ValueError('incomplete release manifest')
    for name,want in files.items():
        path=(root/name).resolve()
        if root.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=want:
            raise ValueError('release hash mismatch: '+name)
    return manifest


def wait_for_exit(handles, timeout=120):
    poll=select.poll()
    for fd in handles:poll.register(fd,select.POLLIN)
    pending=set(handles);end=time.monotonic()+timeout
    while pending and time.monotonic()<end:
        for fd,_ in poll.poll(500):
            if fd in pending:pending.remove(fd);poll.unregister(fd)
    if pending:raise ValueError('previous processes did not finish cleanly; replacement not started')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True);group.add_argument('--apply',action='store_true');group.add_argument('--check',action='store_true')
    args=p.parse_args()
    if sys.platform!='linux':p.error('Japan Linux host required')
    if args.apply and not sys.stdin.isatty():p.error('operator TTY required for financial process replacement')
    manifest=verify_release(RELEASE);before=scope(LIVE,time.time_ns()//1000000);found=processes()
    owners=[x for x in found if x['kind']=='supervisor']
    if len(owners)>1 or (found and not owners):raise ValueError('ambiguous/orphan live processes; no automatic stop')
    print(json.dumps({'release_verified':True,'scope':before,'processes':found,'per_side_usd':600,'aggregate_usd':1200,'ttl_ms':60000,'read_only':args.check}),flush=True)
    if args.check:return
    os.umask(0o077)
    lock=os.open(LIVE/'operator-replacement.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    handles={}
    try:
        # pidfds bind signals/waits to the observed process, avoiding PID reuse.
        for item in found:
            try:
                handles[item['pid']]=os.pidfd_open(item['pid'])
                tick=int((Path('/proc')/str(item['pid'])/'stat').read_text().rpartition(')')[2].split()[19])
                if tick!=item['start_tick']:raise ValueError('PID identity changed; no stop performed')
            except ProcessLookupError:pass
        current=processes()
        if any(x not in found for x in current):raise ValueError('process set changed; no stop performed')
        if owners and owners[0]['pid'] in handles:
            signal.pidfd_send_signal(handles[owners[0]['pid']],signal.SIGTERM)
            print('Waiting for the previous supervisor, worker and owned-order cleanup.',flush=True)
        wait_for_exit(list(handles.values()))
        if processes():raise ValueError('a previous writer remains; replacement not started')
        after=scope(LIVE,time.time_ns()//1000000)
        for key in ('lease_sha256','budget_scope_sha256','session_id','deadline_ms'):
            if before[key]!=after[key]:raise ValueError('original session scope changed during replacement')
        if after['calls_reserved']<before['calls_reserved']:raise ValueError('model ledger rewound')
    finally:
        for fd in handles.values():os.close(fd)
        os.close(lock)
    # The existing launcher owns key handling, account reconciliation and the
    # single-writer lock. It restores this exact journal and fixed end time.
    source=RELEASE/'source';os.chdir(source)
    command=[sys.executable,str(source/'products/pacifica_muse/owner_start.py'),'--live',
             '--binary',str(RELEASE/'bin/muse-maker'),'--root',str(LIVE),'--observer',str(OBSERVER),
             '--model-key-file',str(BASE/'muse-maker-config-20261015-a/openrouter.key'),
             '--agent-encrypted-file',str(BASE/'pacifica-ticking-release-20261014-b/credentials/agent.key.cred'),
             '--model-budget-usd',after['model_budget_usd']]
    os.execv(sys.executable,command)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,sqlite3.Error) as exc:
        print('Replacement not completed: '+str(exc),file=sys.stderr);raise SystemExit(1)
