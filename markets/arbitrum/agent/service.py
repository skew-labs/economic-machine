"""Bounded quote service: immutable mandate, restart reconciliation, external keys.

Checking configuration performs reads only. Execution requires --run and an
already authorized signer socket. This service never opens sessions, deposits,
withdraws, raises budgets, changes profiles, or issues recovery fences.
"""
import argparse,fcntl,hashlib,json,os,pathlib,signal,stat,time
from batch_journal import BatchJournal,hexword
from coordinator import Coordinator,NativeLane,Profile
from observer import Observer
from transport import RPC,SocketSigner,Transport,verify_signed

def private_json(path):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor) as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>65536:raise ValueError('private configuration file')
        return json.load(f)

def mandate(value):
    required={'chain','market','codehash','sender','budget_wei','fee_cap_wei','initial_sender_nonce','window','deadline_unix','poll_ms','max_gas','priority_fee_wei','library','library_sha256','journal','status','signer_socket','profiles'}
    if set(value)!=required:raise ValueError('mandate fields')
    for key in ('chain','initial_sender_nonce','window','deadline_unix','poll_ms','max_gas'):
        if type(value[key])!=int:raise ValueError('mandate integer')
    for key in ('budget_wei','fee_cap_wei','priority_fee_wei'):
        if not isinstance(value[key],str) or not value[key].isdigit() or len(value[key])>40:raise ValueError('fee integer string')
    if value['chain'] not in (42161,421614) or not 0<=value['initial_sender_nonce']<2**63 or not 1<=value['window']<=65536:raise ValueError('execution domain')
    if not 20<=value['poll_ms']<=10000 or not 21000<=value['max_gas']<=32000000 or not 0<value['deadline_unix']<2**40:raise ValueError('service bounds')
    if not 0<int(value['fee_cap_wei'])<=int(value['budget_wei']):raise ValueError('aggregate fees')
    for key,size in (('market',20),('sender',20),('codehash',32)):value[key]=hexword(value[key],size)
    if not isinstance(value['library_sha256'],str) or len(value['library_sha256'])!=64:raise ValueError('native identity')
    bytes.fromhex(value['library_sha256'])
    for key in ('library','journal','status','signer_socket'):
        if not isinstance(value[key],str) or not pathlib.Path(value[key]).is_absolute():raise ValueError('absolute service path')
    if len({value[k] for k in ('library','journal','status','signer_socket')})!=4:raise ValueError('distinct service paths')
    profiles=value['profiles']
    if not isinstance(profiles,list) or not 1<=len(profiles)<=32:raise ValueError('profile capacity')
    fields={'account','version','max_position','clip','spread','inventory_weight','ttl_seconds','session_epoch'}
    for p in profiles:
        if set(p)!=fields or any(type(v)!=int for v in p.values()):raise ValueError('profile fields')
        if not 0<p['account']<2**31-1 or not 0<p['version']<2**64 or not 0<p['clip']<=p['max_position']<=10**9 or not 1<=p['spread']<=64 or not 0<=p['inventory_weight']<=16 or not 1<=p['ttl_seconds']<=60 or not 0<p['session_epoch']<2**32:raise ValueError('delegated profile bounds')
    if len({p['account'] for p in profiles})!=len(profiles):raise ValueError('duplicate profile')
    return value

def bind(journal,config):
    fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with journal.transaction():
        journal.db.execute('create table if not exists service_binding(id integer primary key check(id=1), fingerprint text not null)')
        old=journal.db.execute('select fingerprint from service_binding where id=1').fetchone()
        if old is None:
            if journal.db.execute('select count(*) from batches').fetchone()[0]:raise ValueError('unbound historical journal')
            journal.db.execute('insert into service_binding values(1,?)',(fingerprint,))
        elif old!=(fingerprint,):raise ValueError('service mandate changed')

def bootstrap(transport):
    """Resolve known old bytes before any fresh native instance is installed."""
    j=transport.journal
    if transport.audit()=='halted_reorg' or j.db.execute('select count(*) from accounts where halted=1').fetchone()[0]:return 'halted'
    rows=j.db.execute("select id,state from batches where state in ('prepared','signing','signed','broadcast','included_revert','reorg') order by sender_nonce").fetchall()
    for identity,state in rows:
        if state=='prepared':j.abort_unsigned(identity)
        elif state=='signing':j.halt_for_fence();return 'halted_unknown_signer'
        elif state=='reorg':return 'halted'
        else:
            if state=='signed':
                r=j.get(identity);verify_signed(r['raw'],r,j,transport.max_gas);transport.send(identity)
            outcome=transport.reconcile(identity)
            if outcome in ('halted_reorg','reorg'):return 'halted'
            if outcome not in ('included','finalized','reverted'):return 'reconciling'
    return 'ready'

class Status:
    def __init__(self,path):self.path=pathlib.Path(path)
    def write(self,state,**fields):
        # A consumer can never observe a half-written JSON document.
        temp=self.path.with_name(self.path.name+'.tmp')
        descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as f:
            json.dump({'state':state,'updated_unix':int(time.time()),**fields},f);f.flush();os.fsync(f.fileno())
        os.replace(temp,self.path)

def profiles(config,nonces):
    now=time.monotonic_ns();remaining=config['deadline_unix']-time.time()
    if remaining<=0:raise ValueError('mandate expired')
    expiry=now+int(remaining*10**9)
    return [Profile(config['chain'],p['account'],p['version'],expiry,nonces[p['account']],p['max_position'],p['clip'],p['spread'],p['inventory_weight'],p['ttl_seconds'],p['session_epoch']) for p in config['profiles']],now

def run(config,rpc,execute=False):
    library=pathlib.Path(config['library'])
    if hashlib.sha256(library.read_bytes()).hexdigest()!=config['library_sha256']:raise ValueError('native binary changed')
    initial={p['account']:0 for p in config['profiles']};ps,_=profiles(config,initial)
    observer=Observer(rpc,config['chain'],config['market'],config['codehash'],config['sender'],ps)
    frames,anchor=observer.read()
    if not execute:return {'state':'checked','chain':config['chain'],'agents':len(frames),'canonical_head':anchor['number'],'signing_requests':0}
    directory=pathlib.Path(config['journal']).parent
    info=os.stat(directory,follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('private journal directory')
    if pathlib.Path(config['status']).parent!=directory:raise ValueError('private status directory')
    lock=os.open(config['journal']+'.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    j=None;lane=None;stop=False;status=Status(config['status'])
    def stopping(*_):
        nonlocal stop
        stop=True
    old_handlers={s:signal.signal(s,stopping) for s in (signal.SIGTERM,signal.SIGINT)}
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if pathlib.Path(config['journal']).is_symlink():raise ValueError('journal symlink')
        j=BatchJournal(config['journal'],config['chain'],config['market'],config['codehash'],config['sender'],int(config['budget_wei']),config['initial_sender_nonce'],config['window'])
        bind(j,config)
        registered={a for a, in j.db.execute('select account from accounts')}
        if registered and registered!=set(frames):raise ValueError('journal profile set')
        if not registered:
            if int(rpc('eth_getTransactionCount',[config['sender'],'latest']),16)!=config['initial_sender_nonce']:raise ValueError('initial sender nonce mismatch')
            with j.transaction():
                for account,frame in frames.items():j.register(account,frame.account_nonce)
        transport=Transport(rpc,j,SocketSigner(config['signer_socket']),config['max_gas'],int(config['priority_fee_wei']))
        while not stop and time.time()<config['deadline_unix']-1:
            state=bootstrap(transport);status.write(state,reserved_and_spent_wei=str(j.used()))
            if state.startswith('halted'):return {'state':state}
            if state=='ready':break
            time.sleep(config['poll_ms']/1000)
        if stop or time.time()>=config['deadline_unix']-1:return {'state':'stopped_before_new_commands'}
        frames,_=observer.read();nonces={a:n for a,n in j.db.execute('select account,nonce from accounts')}
        if any(frames[a].account_nonce!=n for a,n in nonces.items()):raise ValueError('restart account nonce mismatch')
        ps,now=profiles(config,nonces);observer.profiles=ps;lane=NativeLane(library,ps,now);coordinator=Coordinator(lane,transport)
        while not stop and time.time()<config['deadline_unix']-1:
            coordinator.poll()
            if coordinator.halted:return {'state':'halted_reorg'}
            if not coordinator.pending:
                if j.used()+int(config['fee_cap_wei'])>j.budget:return {'state':'fee_budget_exhausted'}
                frames,anchor=observer.read()
                if stop or time.time()>=config['deadline_unix']-1:break
                result=coordinator.submit(frames,int(config['fee_cap_wei']))
                status.write(result['state'],agents=len(frames),canonical_head=anchor['number'],reserved_and_spent_wei=str(j.used()),suppressed=lane.suppressed)
            time.sleep(config['poll_ms']/1000)
        # Never create a new transaction after stop/deadline. Existing quotes
        # expire; owners retain independent cancellation and withdrawal control.
        coordinator.poll()
        return {'state':'stopped_with_pending' if coordinator.pending else 'stopped','reserved_and_spent_wei':str(j.used()),'provisional_batches':j.db.execute("select count(*) from batches where state in ('included','included_revert')").fetchone()[0]}
    except Exception as error:
        status.write('failed',error_type=type(error).__name__)
        raise
    finally:
        if lane:lane.close()
        if j:j.close()
        os.close(lock)
        for s,h in old_handlers.items():signal.signal(s,h)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mandate',required=True);parser.add_argument('--rpc-file',required=True);parser.add_argument('--run',action='store_true')
    args=parser.parse_args();rpc=None
    try:
        config=mandate(private_json(args.mandate))
        if time.time()>=config['deadline_unix']:
            result={'state':'mandate_expired','signing_requests':0}
            if args.run:Status(config['status']).write(result['state'],signing_requests=0)
            print(json.dumps(result));return 0
        endpoint=private_json(args.rpc_file)
        if set(endpoint)!={'url'}:raise ValueError('RPC configuration')
        rpc=RPC(endpoint['url']);result=run(config,rpc,args.run)
        if args.run:Status(config['status']).write(result['state'],**{k:v for k,v in result.items() if k!='state'})
        print(json.dumps(result));return 0
    except Exception as error:
        # Endpoint, signed bytes and signer details never enter error output.
        print(json.dumps({'state':'failed','error_type':type(error).__name__}));return 1
    finally:
        if rpc:rpc.close()
if __name__=='__main__':raise SystemExit(main())
