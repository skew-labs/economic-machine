"""One session signer per actor, independent transactions and exact-byte recovery."""
import asyncio,base64,hashlib,json,os,struct,time
from solders.hash import Hash
from solders.message import Message
from solders.transaction import VersionedTransaction
from solders.compute_budget import set_compute_unit_limit
from wire import *
from rt_store import TERMINAL
from cu_profile import compute_limit
from topology import allowed,keeper
PYTH=Pubkey.from_string('7AviUf9nL62mcxNbQGKm4nKDQnPjswo6c5MX4D57HmyE')
class Tx:
    def __init__(self,role,key,manifest,mandate,net,store):
        self.role=role;self.key=key;self.manifest=manifest;self.mandate=mandate;self.net=net;self.store=store;self.blockhash=None;self.blockhash_at=0
        self.program=Pubkey.from_string(manifest['program']);self.market=Pubkey.from_string(manifest['market'])
    async def current(self,finalized=False,minimum=0):
        data,slot,received,owner=await self.net.market(self.market,'finalized' if finalized else 'confirmed',minimum)
        if owner!=str(self.program) or data[:8]!=MAGIC:raise RuntimeError('market identity')
        st=snapshot(data);st['read_slot']=slot;return st,slot,received
    async def recover(self,finalized=False):
        for row in self.store.pending(self.role):await self.resolve(row,finalized)
        if finalized:
            rows=self.store.rows(self.role)
            confirmed=[r for r in rows if r['state']=='CONFIRMED']
            if confirmed:await self.resolve(confirmed[-1],True)
    async def resolve(self,row,finalized=False):
        body=json.loads(row['body']);sig=row['signature'];started=time.monotonic()
        while time.monotonic()-started<65:
            status=(await self.net.call('getSignatureStatuses',[[sig],{'searchTransactionHistory':True}]))['value'][0]
            if status and status['err'] is not None:
                body['chain_error']=status['err'];body['observed_failure_ns']=time.monotonic_ns();self.store.update(sig,'FAILED',body)
                self.store.event(self.role,'transaction_failed',{'signature':sig,'error':status['err']});return
            if status and status.get('confirmationStatus') in (('finalized',) if finalized else ('confirmed','finalized')):
                confirmed=time.monotonic_ns()
                receipt=await self.net.call('getTransaction',[sig,{'encoding':'json','commitment':'finalized' if finalized else 'confirmed','maxSupportedTransactionVersion':0}])
                if not receipt:await asyncio.sleep(0.2);continue
                if receipt['meta']['err'] is not None or receipt['transaction']['signatures'][0]!=sig:raise RuntimeError('receipt mismatch')
                body.setdefault('confirmed_ns',confirmed);body['receipt_ns']=time.monotonic_ns();body['confirmation_status']=status['confirmationStatus']
                if body['op']==6:
                    ret=receipt['meta'].get('returnData')
                    if not ret or ret['programId']!=str(self.program):raise RuntimeError('missing fill return data')
                    raw=base64.b64decode(ret['data'][0]);filled,turnover=struct.unpack('<QQ',raw)
                    if filled>body['max_lots'] or turnover>body['turnover_cap']:raise RuntimeError('execution exceeded intent')
                    body.update(filled_lots=filled,turnover=turnover)
                self.store.update(sig,'CONFIRMED',body,receipt);return
            if status is None:
                height=await self.net.call('getBlockHeight',[{'commitment':'finalized'}])
                if height>body['last_valid_height']:
                    minslot=await self.net.call('getSlot',[{'commitment':'finalized'}])
                    st,slot,_=await self.current(True,minslot)
                    if self.role==keeper(self.manifest):
                        # Oracle/funding refresh may be repeated only after signature
                        # expiry. They derive state onchain and cannot move collateral.
                        body['expiry_proof']={'height':height,'slot':slot};self.store.update(sig,'EXPIRED',body);return
                    seat=st['seats'][self.role]
                    if seat['epoch']==body['epoch'] and seat['sequence']<body['sequence']:
                        body['expiry_proof']={'height':height,'slot':slot,'epoch':seat['epoch'],'sequence':seat['sequence']}
                        self.store.update(sig,'EXPIRED',body);return
                    if seat['epoch']==body['epoch'] and seat['sequence']==body['sequence'] and seat['command_digest']==body['command_digest']:
                        self.store.event(self.role,'applied_waiting_receipt',{'signature':sig})
                    else:raise RuntimeError('sequence/epoch changed; actor fenced pending reconciliation')
                elif not body.get('rebroadcasted'):
                    body['rebroadcasted']=True;self.store.note_body(sig,body)
                    try:await self.net.call('sendTransaction',[row['raw'],{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':2}])
                    except RuntimeError:pass
            await asyncio.sleep(0.2)
        raise RuntimeError('unresolved signature; actor exposure remains blocked')
    async def send(self,st,op,payload=b'',*,policy_until=0,source_ns=None,decision_ns=None,kind='operation'):
        if op not in allowed(self.role,self.manifest):raise RuntimeError('session operation not authorized')
        if self.store.pending(self.role):raise RuntimeError('pending actor operation')
        if time.time()>=self.mandate['ends_unix']:raise RuntimeError('mandate expired')
        seat=st['seats'][self.role] if self.role!=keeper(self.manifest) else None
        if seat and (seat['session']!=str(self.key.pubkey()) or not seat['session_limited']):raise RuntimeError('session revoked')
        if seat:
            confirmed=self.store.last_confirmed(self.role)
            if confirmed and (seat['epoch']!=confirmed['epoch'] or seat['sequence']<confirmed['sequence']):
                raise RuntimeError('market stream has not caught up with actor receipt')
        slot=st['read_slot']
        if seat and slot+60>=seat['session_expiry']:raise RuntimeError('session expired or too near expiry to dispatch')
        extras=[meta(PYTH)] if op==16 else []
        ix=command(self.program,self.market,self.key.pubkey(),op,self.role if seat else 0,payload,
            seq=seat['sequence']+1 if seat else 1,epoch=seat['epoch'] if seat else 1,deadline=slot+60,
            observed=st['market_sequence'],extras=extras)
        if self.blockhash is None or time.monotonic()-self.blockhash_at>15:
            self.blockhash=(await self.net.call('getLatestBlockhash',[{'commitment':'confirmed'}]))['value'];self.blockhash_at=time.monotonic()
        # Network waits must not turn an admitted quote into a stale submission.
        if op in (5,6) and (source_ns is None or not 0<=time.monotonic_ns()-source_ns<3_000_000_000):raise RuntimeError('source expired during transaction preparation')
        if policy_until and time.time()+1>=policy_until:raise RuntimeError('policy lease too short to dispatch')
        sign_start=time.monotonic_ns()
        cu=compute_limit(op,self.mandate)
        tx=VersionedTransaction(Message.new_with_blockhash([set_compute_unit_limit(cu),ix],self.key.pubkey(),Hash.from_string(self.blockhash['blockhash'])),[self.key])
        signed=time.monotonic_ns();raw=base64.b64encode(bytes(tx)).decode();sig=str(tx.signatures[0])
        body={'kind':kind,'op':op,'compute_unit_limit':cu,'source_ns':source_ns or sign_start,'decision_ns':decision_ns or sign_start,'sign_start_ns':sign_start,'signed_ns':signed,
              'epoch':seat['epoch'] if seat else 0,'sequence':seat['sequence']+1 if seat else 0,'source_slot':slot,'policy_until':policy_until,
              'command_digest':hashlib.sha256(b'MPERPS-CMD-v1'+bytes(self.program)+bytes(self.market)+bytes(ix.data)).hexdigest(),
              'last_valid_height':self.blockhash['lastValidBlockHeight'],'before_position':seat['position'] if seat else 0,'boot_id':self.mandate['boot_id']}
        if op==6:body.update(max_lots=struct.unpack_from('<Q',payload,8)[0],turnover_cap=struct.unpack_from('<Q',payload,24)[0])
        self.store.prepare(self.role,sig,raw,body);body['journaled_ns']=time.monotonic_ns()
        expired_source=op in (5,6) and not 0<=time.monotonic_ns()-source_ns<3_000_000_000
        if expired_source or time.time()>=self.mandate['ends_unix'] or (policy_until and time.time()+0.5>=policy_until):
            self.store.update(sig,'CANCELLED',body);return None
        body['submit_ns']=time.monotonic_ns();self.store.note_body(sig,body)
        try:
            returned=await self.net.call('sendTransaction',[raw,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':3}])
            if returned!=sig:raise RuntimeError('RPC signature mismatch')
            body['ack_ns']=time.monotonic_ns();self.store.note_body(sig,body)
        except RuntimeError:
            # Transport and preflight ambiguity are reconciled with this exact
            # signature and finalized sequence before release or replacement.
            self.store.event(self.role,'send_unknown',{'signature':sig});raise
        if self.mandate['fault_drill'] and self.role==0 and op==5 and time.time()>self.mandate['starts_unix']+50 and not self.store.count_events(0,'crash_after_send'):
            self.store.event(0,'crash_after_send',{'signature':sig});os._exit(73)
        row=self.store.get(sig)
        await self.resolve(row)
        return self.store.get(sig)
