"""Durable atomic quote batches, nonce fencing, aggregate fee reservations.

Prepared intent precedes signing. Known signed hash precedes broadcast. An
unknown outcome stays reserved; timeouts never free an account or sender nonce.
Recovery requires a finalized on-chain nonce fence, preserving the old journal.
"""
import contextlib,hashlib,json,os,sqlite3
SELECTOR=bytes.fromhex('30554c7e')
def hexword(value,size):
    if not isinstance(value,str) or len(value)!=2+size*2 or not value.startswith('0x'):raise ValueError('hex identity')
    try:bytes.fromhex(value[2:])
    except ValueError:raise ValueError('hex identity')
    return value.lower()
def decode(payload):
    payload=bytes(payload)
    if len(payload)<132 or payload[:4]!=SELECTOR or int.from_bytes(payload[4:36],'big')!=32:raise ValueError('batch ABI')
    length=int.from_bytes(payload[36:68],'big')
    if not length or length%36 or length>1152 or len(payload)!=68+((length+31)//32)*32 or any(payload[68+length:]):raise ValueError('batch length')
    members=[]
    for offset in range(68,68+length,36):
        account=int.from_bytes(payload[offset:offset+4],'big');command=int.from_bytes(payload[offset+4:offset+36],'big')
        nonce=command&((1<<64)-1)
        if not 0<account<2**31-1 or not 0<nonce<2**63 or command>>232 or not (command>>64)&((1<<40)-1):raise ValueError('command domain')
        if account in [a for a,_,_ in members]:raise ValueError('duplicate account')
        members.append((account,nonce,command))
    return members

class BatchJournal:
    def __init__(self,path,chain,market,codehash,sender,budget,initial_sender_nonce,window=8192):
        if chain not in (42161,421614) or budget<=0 or not 1<=window<=65536 or not 0<=initial_sender_nonce<2**63:raise ValueError('mandate')
        market=hexword(market,20);codehash=hexword(codehash,32);sender=hexword(sender,20)
        self.db=sqlite3.connect(path,isolation_level=None);os.chmod(path,0o600)
        self.db.execute('pragma journal_mode=WAL');self.db.execute('pragma synchronous=FULL');self.db.execute('pragma foreign_keys=ON')
        self.db.executescript('''
        create table if not exists config(id integer primary key check(id=1),chain integer,market text,codehash text,sender text,budget text,initial_nonce integer,next_nonce integer,window integer,used text not null default '0',serial integer not null default 0);
        create table if not exists accounts(account integer primary key,nonce integer not null,halted integer not null default 0,provisional integer not null default 0,unresolved integer not null default 0);
        create table if not exists batches(id text primary key,sender_nonce integer,payload blob,fee_cap text,actual_fee text,txhash text unique,raw blob,blockhash text,blocknum integer,state text,sign_request text);
        create table if not exists members(batch text references batches(id),account integer references accounts(account),nonce integer,command text,primary key(batch,account));
        create table if not exists active_members(batch text references batches(id),account integer references accounts(account),primary key(batch,account));
        create index if not exists active_by_account on active_members(account,batch);
        create index if not exists batch_state on batches(state);
        create index if not exists batch_state_height on batches(state,blocknum);
        create table if not exists fences(id integer primary key,blockhash text,blocknum integer,account integer,nonce integer);
        ''')
        if 'sign_request' not in [r[1] for r in self.db.execute('pragma table_info(batches)')]:self.db.execute('alter table batches add column sign_request text')
        columns=[r[1] for r in self.db.execute('pragma table_info(config)')]
        if 'used' not in columns:
            self.db.execute("alter table config add column used text not null default '0'")
            used=sum(int(actual) if actual is not None and state in ('finalized','reverted') else int(cap) for cap,actual,state in self.db.execute('select fee_cap,actual_fee,state from batches'))
            self.db.execute('update config set used=?',(str(used),))
            self.db.execute("insert or ignore into active_members select batch,account from members join batches on members.batch=batches.id where state not in ('finalized','reverted','fenced','aborted')")
        if 'serial' not in columns:
            self.db.execute('alter table config add column serial integer not null default 0')
            self.db.execute('update config set serial=?',(self.db.execute('select count(*) from batches').fetchone()[0],))
        account_columns=[r[1] for r in self.db.execute('pragma table_info(accounts)')]
        if 'provisional' not in account_columns:
            self.db.execute('alter table accounts add column provisional integer not null default 0')
            self.db.execute('alter table accounts add column unresolved integer not null default 0')
            self.db.execute('update accounts set provisional=(select count(*) from active_members where active_members.account=accounts.account)')
            self.db.execute("update accounts set unresolved=exists(select 1 from active_members m join batches b on b.id=m.batch where m.account=accounts.account and b.state!='included')")
        requested=(chain,market,codehash,sender,str(budget),initial_sender_nonce,window)
        old=self.db.execute('select chain,market,codehash,sender,budget,initial_nonce,window from config where id=1').fetchone()
        if old is None:self.db.execute('insert into config(id,chain,market,codehash,sender,budget,initial_nonce,next_nonce,window) values(1,?,?,?,?,?,?,?,?)',(*requested[:6],initial_sender_nonce,window))
        elif old!=requested:self.db.close();raise ValueError('immutable mandate mismatch')
        self.chain,self.market,self.codehash,self.sender,self.budget,self.window=chain,market,codehash,sender,budget,window
    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('begin immediate')
        try:yield;self.db.execute('commit')
        except BaseException:self.db.execute('rollback');raise
    def register(self,account,nonce):
        if not 0<account<2**31-1 or not 0<=nonce<2**63:raise ValueError('account range')
        self.db.execute('insert into accounts(account,nonce) values(?,?)',(account,nonce))
    def used(self):
        return int(self.db.execute('select used from config where id=1').fetchone()[0])
    def _charge(self,delta):
        used=self.used()+delta
        if not 0<=used<=self.budget:raise ValueError('aggregate fee budget')
        self.db.execute('update config set used=? where id=1',(str(used),))
    def prepare(self,payload,fee_cap):
        members=decode(payload)
        if fee_cap<=0:raise ValueError('fee cap')
        with self.transaction():
            # One unresolved sender transaction. Included provisional batches may pipeline.
            if self.db.execute("select count(*) from batches where state in ('prepared','signing','signed','broadcast','reorg')").fetchone()[0]:raise ValueError('sender backpressure')
            if self.used()+fee_cap>self.budget:raise ValueError('aggregate fee budget')
            for account,nonce,_ in members:
                row=self.db.execute('select nonce,halted,provisional,unresolved from accounts where account=?',(account,)).fetchone()
                if not row or row[1] or nonce!=row[0]+1:raise ValueError('nonce or halted account')
                if row[2]>=self.window or row[3]:raise ValueError('account backpressure')
            sender_nonce=self.db.execute('select next_nonce from config where id=1').fetchone()[0]
            if sender_nonce>=2**63-1:raise ValueError('sender nonce exhausted')
            attempt=self.db.execute('select serial from config where id=1').fetchone()[0]
            if attempt>=2**63-1:raise ValueError('journal serial exhausted')
            identity=hashlib.sha256(str(self.chain).encode()+self.market.encode()+self.sender.encode()+sender_nonce.to_bytes(8,'big')+attempt.to_bytes(8,'big')+bytes(payload)).hexdigest()
            self.db.execute('insert into batches(id,sender_nonce,payload,fee_cap,state) values(?,?,?,?,?)',(identity,sender_nonce,bytes(payload),str(fee_cap),'prepared'))
            self.db.executemany('insert into members values(?,?,?,?)',[(identity,a,n,str(c)) for a,n,c in members])
            self.db.executemany('insert into active_members values(?,?)',[(identity,a) for a,_,_ in members])
            self.db.executemany('update accounts set provisional=provisional+1,unresolved=1 where account=?',[(a,) for a,_,_ in members])
            self._charge(fee_cap);self.db.execute('update config set next_nonce=next_nonce+1,serial=serial+1 where id=1')
        return identity
    def abort_unsigned(self,identity):
        with self.transaction():
            row=self.db.execute('select sender_nonce,state,fee_cap from batches where id=?',(identity,)).fetchone()
            if not row or row[1]!='prepared' or row[0]+1!=self.db.execute('select next_nonce from config').fetchone()[0]:raise ValueError('not unsigned latest intent')
            self.db.execute("update batches set state='aborted',actual_fee='0',fee_cap='0' where id=?",(identity,))
            self._charge(-int(row[2]));self.db.execute('delete from active_members where batch=?',(identity,))
            self.db.execute('update accounts set provisional=provisional-1,unresolved=0 where account in (select account from members where batch=?)',(identity,))
            self.db.execute('update config set next_nonce=next_nonce-1 where id=1')
    def signed(self,identity,txhash,raw):
        txhash=hexword(txhash,32)
        if not raw:raise ValueError('signed bytes missing')
        with self.transaction():
            row=self.db.execute('select state,txhash,raw from batches where id=?',(identity,)).fetchone()
            if row==('signed',txhash,bytes(raw)):return
            if row not in [('prepared',None,None),('signing',None,None)]:raise ValueError('not prepared')
            self.db.execute("update batches set txhash=?,raw=?,state='signed' where id=?",(txhash,bytes(raw),identity))
    def mark_signing(self,identity,request):
        with self.transaction():
            row=self.db.execute('select state from batches where id=?',(identity,)).fetchone()
            if row!=('prepared',):raise ValueError('signing already requested')
            self.db.execute("update batches set state='signing',sign_request=? where id=?",(json.dumps(request,sort_keys=True),identity))
    def broadcast(self,identity):
        with self.transaction():
            state=self.db.execute('select state from batches where id=?',(identity,)).fetchone()
            if state==('broadcast',):return
            if state!=('signed',):raise ValueError('not signed')
            self.db.execute("update batches set state='broadcast' where id=?",(identity,))
    def included(self,identity,txhash,blockhash,height,canonical_hash,observed,fee,success,quote_commands):
        txhash=hexword(txhash,32);blockhash=hexword(blockhash,32)
        if canonical_hash!=blockhash or height<0:raise ValueError('noncanonical receipt')
        with self.transaction():
            row=self.db.execute('select txhash,state,fee_cap from batches where id=?',(identity,)).fetchone()
            if not row or row[:2]!=(txhash,'broadcast') or not 0<=fee<=int(row[2]):raise ValueError('receipt mismatch')
            members=self.db.execute('select account,nonce,command from members where batch=?',(identity,)).fetchall()
            expected={a:int(c) for a,_,c in members}
            if quote_commands!=(expected if success else {}):raise ValueError('quote receipt mismatch')
            for account,nonce,_ in members:
                if observed.get(account)!=(nonce if success else nonce-1):raise ValueError('receipt/account disagreement')
                if self.db.execute('select halted from accounts where account=?',(account,)).fetchone()[0]:raise ValueError('halted account')
            self.db.execute('update batches set state=?,actual_fee=?,blockhash=?,blocknum=? where id=?',('included' if success else 'included_revert',str(fee),blockhash,height,identity))
            if success:self.db.executemany('update accounts set nonce=?,unresolved=0 where account=?',[(n,a) for a,n,_ in members])
    def canonical(self,identity,canonical_hash,finalized_height):
        with self.transaction():
            row=self.db.execute('select state,blockhash,blocknum,fee_cap,actual_fee from batches where id=?',(identity,)).fetchone()
            if not row or row[0] not in ('included','included_revert'):raise ValueError('not included')
            if canonical_hash!=row[1]:
                # One EOA domain: even unrelated account batches depend on its nonce.
                touched={a for a, in self.db.execute('select distinct account from active_members')}
                self.db.execute("update batches set state='reorg' where state not in ('finalized','reverted','fenced','aborted')")
                self.db.executemany('update accounts set halted=1 where account=?',[(a,) for a in touched]);return 'halted_reorg'
            if row[2]<=finalized_height:
                return self._finalize(identity,row)
            return row[0]
    def _finalize(self,identity,row):
        terminal='finalized' if row[0]=='included' else 'reverted'
        self._charge(int(row[4])-int(row[3]))
        self.db.execute('update accounts set provisional=provisional-1 where account in (select account from active_members where batch=?)',(identity,))
        if terminal=='reverted':self.db.execute('update accounts set unresolved=0 where account in (select account from active_members where batch=?)',(identity,))
        self.db.execute('delete from active_members where batch=?',(identity,))
        self.db.execute('update batches set state=? where id=?',(terminal,identity));return terminal
    def checkpoint(self):
        # Two index seeks, no temporary sort of the entire provisional window.
        rows=[self.db.execute('select id,blockhash,blocknum from batches where state=? order by blocknum desc limit 1',(state,)).fetchone() for state in ('included','included_revert')]
        return max((r for r in rows if r is not None),key=lambda r:r[2],default=None)
    def finalize_prefix(self,height):
        # Caller has verified the newest provisional block's canonical hash.
        # Its hash commits to its ancestors. Each terminal row is visited once.
        with self.transaction():
            rows=self.db.execute("select id,state,blockhash,blocknum,fee_cap,actual_fee from batches where state in ('included','included_revert') and blocknum<=?",(height,)).fetchall()
            for identity,*row in rows:self._finalize(identity,row)
        return len(rows)
    def recover_fenced(self,observed,blockhash,height,canonical_hash,finalized_height,sender_nonce):
        blockhash=hexword(blockhash,32)
        if canonical_hash!=blockhash or not 0<=height<=finalized_height or not 0<=sender_nonce<2**63:raise ValueError('fence is not finalized')
        with self.transaction():
            halted={a for a, in self.db.execute('select account from accounts where halted=1')}
            if not halted or set(observed)!=halted:raise ValueError('incomplete recovery component')
            for account in halted:
                highest=self.db.execute("select max(m.nonce) from members m join batches b on b.id=m.batch where m.account=? and b.state='reorg'",(account,)).fetchone()[0]
                if highest is None or not highest<=observed[account]<2**63:raise ValueError('unfenced command')
            highest_sender=self.db.execute("select max(sender_nonce) from batches where state='reorg'").fetchone()[0]
            if highest_sender is None or sender_nonce<=highest_sender:raise ValueError('unfenced sender nonce')
            # The reorged bytes can never apply after the finalized command nonce fence.
            # Unknown gas costs remain fully reserved, including after recovery.
            self.db.execute("update batches set state='fenced' where state='reorg'")
            self.db.execute("delete from active_members where batch in (select id from batches where state='fenced')")
            self.db.executemany('update accounts set nonce=?,halted=0,provisional=0,unresolved=0 where account=?',[(n,a) for a,n in observed.items()])
            self.db.executemany('insert into fences(blockhash,blocknum,account,nonce) values(?,?,?,?)',[(blockhash,height,a,n) for a,n in observed.items()])
            self.db.execute('update config set next_nonce=? where id=1',(sender_nonce,))
    def halt_for_fence(self):
        # Converts an unresolved signer/broadcast outcome into a fenced recovery
        # requirement; it never declares the original bytes safe to retry.
        with self.transaction():
            touched={a for a, in self.db.execute('select distinct account from active_members')}
            if not touched:raise ValueError('no unresolved execution')
            self.db.execute("update batches set state='reorg' where state not in ('finalized','reverted','fenced','aborted')")
            self.db.executemany('update accounts set halted=1 where account=?',[(a,) for a in touched])
    def get(self,identity):
        self.db.row_factory=sqlite3.Row
        row=self.db.execute('select * from batches where id=?',(identity,)).fetchone();self.db.row_factory=None
        if row is None:raise ValueError('unknown batch')
        result=dict(row);result['members']=self.db.execute('select account,nonce,command from members where batch=? order by account',(identity,)).fetchall();return result
    def close(self):self.db.close()
