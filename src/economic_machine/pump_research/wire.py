"""Exact integer instruction extraction; net balance deltas are not transfers."""
import struct
import re
from .store import PUMP, encode, decode

SYSTEM='11111111111111111111111111111111'
TOKENS={'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
# Pinned account positions from pump-fun/pump-public-docs idl/pump.json.
CREATES={bytes([24,30,200,40,5,28,7,119]):7,bytes([214,144,76,236,95,139,49,180]):5}
TRADES={bytes([102,6,61,18,1,218,235,234]):'buy',bytes([56,252,116,8,158,223,205,95]):'buy',bytes([51,230,133,164,1,127,131,173]):'sell'}
TRADE_POSITIONS={d:(kind,2,6) for d,kind in TRADES.items()}
TRADE_POSITIONS.update({bytes(d):(kind,1,user) for d,kind,user in [
    ([194,171,28,70,104,77,91,47],'buy',13),([184,23,238,97,103,197,211,61],'buy',13),
    ([93,246,130,60,231,233,64,178],'sell',13),([225,247,80,30,213,179,132,136],'buy',8),
    ([7,5,29,196,245,23,101,80],'buy',8),([28,146,222,119,38,196,105,213],'sell',8)]})

def varint(raw, pos=0):
    n=0
    for i in range(10):
        if pos>=len(raw): raise ValueError('TRUNCATED_VARINT')
        b=raw[pos];pos+=1
        if i==9 and b>1: raise ValueError('VARINT_OVERFLOW')
        n|=(b&127)<<(i*7)
        if b<128: return n,pos
    raise ValueError('VARINT_OVERFLOW')

def raw_transaction(raw):
    """Legacy/v0 wire -> signature, static keys, compiled instructions.

    ALT keys come only from executed metadata; unknown future formats fail.
    """
    n,p=varint(raw)
    if not 1<=n<=64 or p+64*n>=len(raw): raise ValueError('SIGNATURE_COUNT')
    signatures=[raw[p+64*i:p+64*(i+1)] for i in range(n)]
    sig=encode(signatures[0]);p+=64*n;message_start=p
    if raw[p]&128:
        if raw[p]!=128: raise ValueError('UNSUPPORTED_TRANSACTION_VERSION')
        p+=1;version=0
    else: version=None
    if p+3>len(raw) or raw[p]!=n: raise ValueError('MESSAGE_HEADER')
    p+=3; k,p=varint(raw,p)
    if not n<=k<=256 or p+32*k+32>len(raw): raise ValueError('ACCOUNT_COUNT')
    keys=[raw[p+32*i:p+32*(i+1)] for i in range(k)];p+=32*k+32
    count,p=varint(raw,p);ix=[]
    if count>1024: raise ValueError('INSTRUCTION_COUNT')
    for _ in range(count):
        program=raw[p];p+=1;a,p=varint(raw,p)
        accounts=raw[p:p+a];p+=a;d,p=varint(raw,p);data=raw[p:p+d];p+=d
        if len(accounts)!=a or len(data)!=d: raise ValueError('TORN_INSTRUCTION')
        ix.append((program,list(accounts),data))
    if version==0:
        tables,p=varint(raw,p)
        if tables>256: raise ValueError('ALT_BOUND')
        for _ in range(tables):
            p+=32;w,p=varint(raw,p);p+=w;r,p=varint(raw,p);p+=r
    if p!=len(raw): raise ValueError('TRANSACTION_TRAILING_BYTES')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        for signer,signature in zip(keys[:n],signatures):
            Ed25519PublicKey.from_public_bytes(signer).verify(signature,raw[message_start:])
    except InvalidSignature: raise ValueError('TRANSACTION_SIGNATURE_INVALID') from None
    return sig,keys,ix

def parse_strings(data):
    p=8;strings=[]
    for _ in range(3):
        if p+4>len(data): raise ValueError('CREATE_STRING')
        n=struct.unpack_from('<I',data,p)[0];p+=4
        if n>2048 or p+n>len(data): raise ValueError('CREATE_STRING')
        strings.append(data[p:p+n].decode('utf8',errors='replace'));p+=n
    creator=encode(data[p:p+32]) if p+32<=len(data) else ''
    return strings[0][:256],strings[1][:64],creator

def committed_inner(meta,outer,keys):
    """Map complete runtime invocation logs to recorded CPI instructions.

    A successful transaction can contain a failed CPI caught by its caller.
    Child successes inside that reverted subtree are also uncommitted. Missing
    logs, repeated ambiguous outer programs, or a metadata/log mismatch cannot
    establish CPI commitment. Top-level instructions remain independently
    committed by the transaction's successful status.
    """
    groups={}
    for group in meta.inner_instructions:
        if not 0<=group.index<len(outer):raise ValueError('INNER_OUTER_INDEX')
        if len(group.instructions)>=65536:raise ValueError('INNER_INSTRUCTION_BOUND')
        if group.index in groups:raise ValueError('INNER_OUTER_DUPLICATE')
        if group.instructions:groups[group.index]=list(group.instructions)
    if not groups:return set(),[]
    roots=[];stack=[]
    def unresolved(reason):return set(),[reason]
    for line in getattr(meta,'log_messages',[]):
        match=re.fullmatch(r'Program ([1-9A-HJ-NP-Za-km-z]+) invoke \[(\d+)\]',line)
        if match:
            depth=int(match[2])
            if depth!=len(stack)+1:return unresolved('CPI_INCOMPLETE_INVOCATION_STACK')
            node=dict(program=match[1],depth=depth,children=[],success=None)
            (stack[-1]['children'] if stack else roots).append(node);stack.append(node)
            continue
        match=re.fullmatch(r'Program ([1-9A-HJ-NP-Za-km-z]+) (success|failed:.*)',line)
        if match:
            if not stack or stack[-1]['program']!=match[1]:return unresolved('CPI_INCOMPLETE_INVOCATION_STACK')
            stack.pop()['success']=match[2]=='success'
    if stack or not roots:return unresolved('CPI_INCOMPLETE_INVOCATION_STACK')
    if any(not root['success'] for root in roots):return unresolved('CPI_OUTER_STATUS_CONFLICT')
    programs=[]
    for program,accounts,data in outer:
        if not 0<=program<len(keys):raise ValueError('INSTRUCTION_ACCOUNT_INDEX')
        programs.append(keys[program])
    # Some top-level native/precompile instructions do not emit invocation logs.
    # An earliest/latest subsequence match admits them only when the mapping is
    # unique; repeated programs cannot silently shift the CPI group assignment.
    earliest=[];pos=0
    for root in roots:
        while pos<len(programs) and programs[pos]!=root['program']:pos+=1
        if pos==len(programs):return unresolved('CPI_OUTER_LOG_MISMATCH')
        earliest.append(pos);pos+=1
    latest=[];pos=len(programs)-1
    for root in reversed(roots):
        while pos>=0 and programs[pos]!=root['program']:pos-=1
        if pos<0:return unresolved('CPI_OUTER_LOG_MISMATCH')
        latest.append(pos);pos-=1
    if earliest!=list(reversed(latest)):return unresolved('CPI_AMBIGUOUS_OUTER_LOG_MAPPING')
    mapped=dict(zip(earliest,roots))
    if not set(groups)<=set(mapped):return unresolved('CPI_MISSING_OUTER_LOG')
    committed=set();errors=[]
    for parent,instructions in groups.items():
        flattened=[];pending=[(node,True) for node in reversed(mapped[parent]['children'])]
        while pending:
            node,ancestors_committed=pending.pop()
            success=ancestors_committed and node['success'] is True
            flattened.append((node,success))
            pending.extend((child,success) for child in reversed(node['children']))
        if len(flattened)!=len(instructions):
            errors.append('CPI_INSTRUCTION_LOG_CARDINALITY');continue
        if any(not 0<=ix.program_id_index<len(keys) for ix in instructions):raise ValueError('INSTRUCTION_ACCOUNT_INDEX')
        if any(keys[ix.program_id_index]!=node['program'] or
               (getattr(ix,'stack_height',0) not in (None,0,node['depth']))
               for ix,(node,success) in zip(instructions,flattened)):
            errors.append('CPI_INSTRUCTION_LOG_MISMATCH');continue
        for j,(node,success) in enumerate(flattened):
            if success:committed.add(parent*65536+j+1)
            else:errors.append('CPI_REVERTED_INVOCATION')
    return committed,sorted(set(errors))

def extract(signature, keys, outer, meta, slot, index=0):
    keys=[encode(k) if isinstance(k,bytes) else k for k in keys]
    keys += [encode(bytes(k)) for k in [*meta.loaded_writable_addresses,*meta.loaded_readonly_addresses]]
    if len(keys)>256 or any(len(decode(k))!=32 for k in keys): raise ValueError('ACCOUNT_KEYS')
    result=dict(signature=signature,slot=int(slot),index=int(index),success=not meta.HasField('err'),transfers=[],launches=[],activity=[],balances=[])
    if not result['success']: return result
    if len(meta.post_balances)==len(keys): result['balances']=[(k,int(b)) for k,b in zip(keys,meta.post_balances)]
    token_mints={}
    for b in [*meta.pre_token_balances,*meta.post_token_balances]:
        old=token_mints.get(b.account_index)
        if old is not None and old!=b.mint: raise ValueError('TOKEN_MINT_CONFLICT')
        token_mints[b.account_index]=b.mint
    committed,commit_errors=committed_inner(meta,outer,keys)
    inner_count=sum(len(group.instructions) for group in meta.inner_instructions)
    result['instruction_commit_evidence']=dict(version=1,committed_inner_count=len(committed),
        committed_inner_orders=sorted(committed),omitted_inner_count=inner_count-len(committed),errors=commit_errors,
        full_transfer_coverage=False)
    instructions=[(i*65536,p,a,d) for i,(p,a,d) in enumerate(outer)]
    for group in meta.inner_instructions:
        if group.index>=len(outer): raise ValueError('INNER_OUTER_INDEX')
        for j,ix in enumerate(group.instructions):
            order=int(group.index)*65536+j+1
            if order in committed:instructions.append((order,ix.program_id_index,list(ix.accounts),bytes(ix.data)))
    for order,program,accounts,data in sorted(instructions):
        if program>=len(keys) or any(a>=len(keys) for a in accounts): raise ValueError('INSTRUCTION_ACCOUNT_INDEX')
        p=keys[program];a=[keys[v] for v in accounts]
        if p==SYSTEM and len(data)>=4:
            kind=int.from_bytes(data[:4],'little')
            if kind==2 and len(data)==12 and len(a)>=2:
                result['transfers'].append(dict(ix=order,source=a[0],destination=a[1],mint='SOL',amount=str(int.from_bytes(data[4:12],'little')),kind='system_transfer',authority=a[0]))
            elif kind==0 and len(data)==52 and len(a)>=2:
                result['transfers'].append(dict(ix=order,source=a[0],destination=a[1],mint='SOL',amount=str(int.from_bytes(data[4:12],'little')),kind='account_creation_funding',authority=a[0]))
            elif kind==11 and len(data)>=20 and len(a)>=3:
                result['transfers'].append(dict(ix=order,source=a[0],destination=a[2],mint='SOL',amount=str(int.from_bytes(data[4:12],'little')),kind='system_transfer_with_seed',authority=a[1]))
        elif p in TOKENS and data:
            if data[0]==3 and len(data)==9 and len(a)>=3:
                mint=token_mints.get(accounts[0])
                if mint and mint==token_mints.get(accounts[1]):
                    result['transfers'].append(dict(ix=order,source=a[0],destination=a[1],mint=mint,amount=str(int.from_bytes(data[1:9],'little')),kind='spl_token_account_transfer',authority=a[2]))
            elif data[0]==12 and len(data)==10 and len(a)>=4:
                result['transfers'].append(dict(ix=order,source=a[0],destination=a[2],mint=a[1],amount=str(int.from_bytes(data[1:9],'little')),kind='spl_token_account_transfer_checked',authority=a[3]))
        elif p==PUMP:
            if data[:8] in CREATES:
                who=CREATES[data[:8]]
                if len(a)<=who: raise ValueError('PUMP_ACCOUNT_BOUND')
                name,symbol,creator=parse_strings(data)
                result['launches'].append(dict(ix=order,mint=a[0],payer=a[who],creator=creator,name=name,symbol=symbol))
            elif data[:8] in TRADE_POSITIONS:
                kind,mint,user=TRADE_POSITIONS[data[:8]]
                if len(a)<=max(mint,user): raise ValueError('PUMP_TRADE_ACCOUNT_BOUND')
                result['activity'].append(dict(ix=order,mint=a[mint],wallet=a[user],action=kind))
    from .events import evidence_events
    result['events'],result['event_errors']=evidence_events(meta,result['activity'],outer,keys)
    return result

def from_geyser(info,slot):
    if not info.HasField('meta') or not info.HasField('transaction'): raise ValueError('MISSING_META')
    t=info.transaction
    if not t.signatures or bytes(t.signatures[0])!=bytes(info.signature) or len(info.signature)!=64: raise ValueError('SIGNATURE_IDENTITY')
    return extract(encode(bytes(info.signature)),[bytes(k) for k in t.message.account_keys],[(i.program_id_index,list(i.accounts),bytes(i.data)) for i in t.message.instructions],info.meta,slot,info.index)
