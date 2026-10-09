"""Cold execution journal. No signer/key custody. Bounded provisional L2 pipeline.
Prepare before broadcast; canonical L2 inclusion may unblock the next nonce.
A reorg halts the whole account pipeline. Recovery remains fail-closed here;
the module never declares a timed-out or orphaned transaction safe to retry.
Finalized is a separate state, never inferred from inclusion or timeout.
"""
import contextlib, hashlib, sqlite3
class Journal:
    def __init__(self,path,chain,market,code_hash,max_fee_wei,window=4):
        if chain not in (42161,421614) or max_fee_wei<=0 or not 1<=window<=16:raise ValueError('limits')
        if len(market)!=42 or len(code_hash)!=66:raise ValueError('deployment identity')
        self.db=sqlite3.connect(path,isolation_level=None)
        self.db.execute('pragma journal_mode=WAL');self.db.execute('pragma synchronous=FULL')
        self.db.executescript('''
        create table if not exists config (id integer primary key check(id=1), chain integer,market text,codehash text,budget text,window integer);
        create table if not exists accounts (account integer primary key,nonce integer not null,halted integer not null default 0);
        create table if not exists intents (id text primary key,account integer,nonce integer,calldata blob,fee_cap text,actual_fee text,txhash text unique,blockhash text,blocknum integer,state text);
        create unique index if not exists active_nonce on intents(account,nonce) where state!='reverted';
        ''')
        requested=(chain,market.lower(),code_hash.lower(),str(max_fee_wei),window)
        old=self.db.execute('select chain,market,codehash,budget,window from config where id=1').fetchone()
        if old is None:self.db.execute('insert into config values(1,?,?,?,?,?)',requested)
        elif old!=requested:raise ValueError('immutable execution mandate mismatch')
        self.chain,self.market,self.code_hash,self.budget,self.window=chain,market.lower(),code_hash.lower(),max_fee_wei,window
    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('begin immediate')
        try:yield;self.db.execute('commit')
        except BaseException:self.db.execute('rollback');raise
    def register(self,account,nonce,chain,market,code_hash):
        if (chain,market.lower(),code_hash.lower())!=(self.chain,self.market,self.code_hash):raise ValueError('wrong deployment')
        if not 0<account<2**31 or not 0<=nonce<2**63:raise ValueError('range')
        self.db.execute('insert into accounts(account,nonce) values(?,?)',(account,nonce))
    def prepare(self,account,nonce,calldata,fee_cap):
        # SQL signed integer range is intentionally narrower than contract uint64.
        if not 0<nonce<2**63 or fee_cap<=0:raise ValueError('range')
        if len(calldata)!=68 or calldata[:4]!=bytes.fromhex('ac8e6b9e') or int.from_bytes(calldata[4:36],'big')!=account or int.from_bytes(calldata[-8:],'big')!=nonce:raise ValueError('payload domain')
        identity=hashlib.sha256(str(self.chain).encode()+self.market.encode()+calldata).hexdigest()
        with self.transaction():
            record=self.db.execute('select nonce,halted from accounts where account=?',(account,)).fetchone()
            if not record or record[1] or nonce!=record[0]+1:raise ValueError('nonce or halted account')
            outstanding=self.db.execute("select count(*) from intents where account=? and state not in ('finalized','reverted')",(account,)).fetchone()[0]
            unresolved=self.db.execute("select count(*) from intents where account=? and state in ('prepared','broadcast','included_revert','reorg')",(account,)).fetchone()[0]
            if outstanding>=self.window or unresolved:raise ValueError('backpressure')
            rows=self.db.execute('select fee_cap,actual_fee,state from intents').fetchall()
            used=sum(int(actual) if state in ('finalized','reverted') else int(cap) for cap,actual,state in rows)
            if used+fee_cap>self.budget:raise ValueError('fee budget')
            self.db.execute('insert into intents(id,account,nonce,calldata,fee_cap,state) values(?,?,?,?,?,?)',(identity,account,nonce,calldata,str(fee_cap),'prepared'))
        return identity
    def broadcast(self,identity,txhash):
        if len(txhash)!=66:raise ValueError('transaction hash')
        with self.transaction():
            row=self.db.execute('select state,txhash from intents where id=?',(identity,)).fetchone()
            if row==('broadcast',txhash):return
            if row!=('prepared',None):raise ValueError('not prepared')
            self.db.execute("update intents set txhash=?,state='broadcast' where id=?",(txhash,identity))
    def included(self,identity,txhash,blockhash,blocknum,canonical_hash,observed_nonce,fee_wei,success):
        if blockhash!=canonical_hash or len(blockhash)!=66 or blocknum<0:raise ValueError('not canonical')
        with self.transaction():
            row=self.db.execute('select account,nonce,fee_cap,txhash,state from intents where id=?',(identity,)).fetchone()
            if not row or row[3]!=txhash or row[4]!='broadcast' or not 0<=fee_wei<=int(row[2]):raise ValueError('receipt mismatch')
            account,nonce=row[:2]
            if observed_nonce!=(nonce if success else nonce-1):raise ValueError('receipt and state disagree')
            if self.db.execute('select halted from accounts where account=?',(account,)).fetchone()[0]:raise ValueError('halted')
            self.db.execute('update intents set state=?,actual_fee=?,blockhash=?,blocknum=? where id=?',('included' if success else 'included_revert',str(fee_wei),blockhash,blocknum,identity))
            if success:self.db.execute('update accounts set nonce=? where account=?',(nonce,account))
    def check_canonical(self,identity,canonical_hash,finalized_height):
        with self.transaction():
            row=self.db.execute('select account,blockhash,blocknum,state from intents where id=?',(identity,)).fetchone()
            if not row or row[3] not in ('included','included_revert'):raise ValueError('not included')
            account,blockhash,height,state=row
            if canonical_hash!=blockhash:
                self.db.execute('update accounts set halted=1 where account=?',(account,))
                self.db.execute("update intents set state='reorg' where account=? and state not in ('finalized','reverted')",(account,));return 'halted_reorg'
            if height<=finalized_height:
                terminal='finalized' if state=='included' else 'reverted'
                self.db.execute('update intents set state=? where id=?',(terminal,identity));return terminal
            return 'included'
    def close(self):self.db.close()
