"""Versioned Pump event evidence. No prices inferred from wallet balance deltas.

IDL snapshot SHA256: 7616681ea557975f9f623ac211080f22a93060781eaf4765b0a488d980997207.
Unknown layouts are retained as unsupported, never silently priced as SOL.
"""
import base64
import hashlib
import re
import struct
from .store import PUMP, encode

TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
EVENT_CPI = bytes.fromhex('e445a52e51cb9a1d')
WSOL = 'So11111111111111111111111111111111111111112'
ZERO = '11111111111111111111111111111111'

class Reader:
    def __init__(self, raw): self.raw, self.p = raw, 8
    def read(self, kind):
        size = {'pubkey':32, 'u64':8, 'i64':8, 'u16':2, 'bool':1}.get(kind)
        if kind == 'string':
            if self.p+4>len(self.raw): raise ValueError('EVENT_TRUNCATED')
            size = int.from_bytes(self.raw[self.p:self.p+4], 'little'); self.p += 4
            if size>96: raise ValueError('EVENT_STRING_BOUND')
        if size is None or self.p+size>len(self.raw): raise ValueError('EVENT_TRUNCATED')
        v=self.raw[self.p:self.p+size]; self.p+=size
        if kind=='pubkey': return encode(v)
        if kind=='string': return v.decode('utf8')
        if kind=='bool':
            if v[0]>1: raise ValueError('EVENT_BOOL')
            return bool(v[0])
        return int.from_bytes(v,'little',signed=kind=='i64')

PREFIX = [('mint','pubkey'),('sol_amount','u64'),('token_amount','u64'),('is_buy','bool'),('user','pubkey'),('timestamp','i64'),('virtual_sol_reserves','u64'),('virtual_token_reserves','u64'),('real_sol_reserves','u64'),('real_token_reserves','u64')]
FEES = [('fee_recipient','pubkey'),('fee_basis_points','u64'),('fee','u64'),('creator','pubkey'),('creator_fee_basis_points','u64'),('creator_fee','u64')]
VOLUME = [('track_volume','bool'),('total_unclaimed_tokens','u64'),('total_claimed_tokens','u64'),('current_sol_volume','u64'),('last_update_timestamp','i64'),('ix_name','string'),('mayhem_mode','bool'),('cashback_fee_basis_points','u64'),('cashback','u64'),('buyback_fee_basis_points','u64'),('buyback_fee','u64')]
QUOTE = [('quote_mint','pubkey'),('quote_amount','u64'),('virtual_quote_reserves','u64'),('real_quote_reserves','u64'),('holder_rewards_bps','u64'),('holder_rewards','u64'),('creator_fee_unclaimed','u64')]

def trade(raw):
    if not raw.startswith(TRADE) or len(raw)>16384: raise ValueError('EVENT_IDENTITY')
    r=Reader(raw); out={k:r.read(t) for k,t in PREFIX}
    out.update(layout='legacy_prefix',layout_supported=False,raw_sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))
    if r.p==len(raw): return out  # Historical fees are unknown, not today's fee.
    out.update({k:r.read(t) for k,t in FEES})
    if r.p==len(raw): out['layout']='fee_prefix'; return out
    out.update({k:r.read(t) for k,t in VOLUME})
    if r.p+4>len(raw): raise ValueError('EVENT_SHAREHOLDERS')
    count=int.from_bytes(raw[r.p:r.p+4],'little');r.p+=4
    if count>64: raise ValueError('EVENT_SHAREHOLDER_BOUND')
    out['shareholders']=[dict(address=r.read('pubkey'),share_bps=r.read('u16')) for _ in range(count)]
    out.update({k:r.read(t) for k,t in QUOTE})
    out['layout']='idl_20261015';out['unknown_tail_bytes']=len(raw)-r.p
    out['layout_supported']=r.p==len(raw)
    return out

def scoped_logs(meta, activity):
    """Accept only data emitted while Pump is the current invocation.

    A router can log arbitrary base64: its event discriminator is not authority.
    Each event must also match an executed mint/user/side instruction. Truncated
    or structurally inconsistent call stacks cannot produce a validated event.
    """
    stack=[]; found=[]; errors=[]; matched={}; invalid=False
    for line in getattr(meta,'log_messages',[]):
        m=re.fullmatch(r'Program ([1-9A-HJ-NP-Za-km-z]+) invoke \[(\d+)\]',line)
        if m:
            if int(m[2])!=len(stack)+1: invalid=True; break
            stack.append(m[1]);continue
        m=re.fullmatch(r'Program ([1-9A-HJ-NP-Za-km-z]+) (success|failed:.*)',line)
        if m:
            if not stack or stack[-1]!=m[1]: invalid=True; break
            # A successful outer router can catch a reverted inner invocation.
            # Its log bytes are not proof that the emitted trade committed.
            if m[2].startswith('failed:'):return [],errors+['FAILED_INNER_INVOCATION']
            stack.pop();continue
        if not line.startswith('Program data: ') or not stack or stack[-1]!=PUMP: continue
        try:
            raw=base64.b64decode(line[14:],validate=True)
            if not raw.startswith(TRADE): continue
            e=trade(raw); key=(e['mint'],e['user'],'buy' if e['is_buy'] else 'sell')
            matches=[a for a in activity if (a['mint'],a['wallet'],a['action'])==key]
            n=matched.get(key,0)
            if n>=len(matches): raise ValueError('EVENT_INSTRUCTION_MISMATCH')
            e['ix']=matches[n]['ix'];matched[key]=n+1
            e['event_n']=len(found);e['network_fee_lamports']=int(getattr(meta,'fee',0));found.append(e)
        except (ValueError,UnicodeError,struct.error) as ex: errors.append(str(ex)[:80])
    if stack or invalid: return [],errors+['INCOMPLETE_INVOCATION_STACK']
    return found,errors

def evidence_events(meta,activity,outer,keys):
    """Recover truncated logs only with an unambiguous committed self-CPI.

    A direct Pump trade is a top-level instruction in a successful transaction;
    its depth-2 self event cannot belong to a router-caught reverted subtree.
    Nested router events remain unverified when their log stack is incomplete.
    """
    if meta.HasField('err'):return [],['FAILED_TRANSACTION']
    found,errors=scoped_logs(meta,activity)
    merged={e['ix']:dict(e,event_evidence='scoped_complete_logs') for e in found}
    for group in meta.inner_instructions:
        parent=group.index
        if parent>=len(outer):return [],errors+['CPI_PARENT_BOUND']
        program,accounts,data=outer[parent]
        if keys[program]!=PUMP:continue
        match=[a for a in activity if a['ix']==parent*65536]
        if len(match)!=1:continue
        a=match[0];candidates=[]
        for ix in group.instructions:
            raw=bytes(ix.data)
            if keys[ix.program_id_index]!=PUMP or getattr(ix,'stack_height',0)!=2 or not raw.startswith(EVENT_CPI+TRADE):continue
            try:
                e=trade(raw[8:])
                if (e['mint'],e['user'],'buy' if e['is_buy'] else 'sell')!=(a['mint'],a['wallet'],a['action']):
                    raise ValueError('CPI_EVENT_INSTRUCTION_MISMATCH')
                e.update(ix=a['ix'],network_fee_lamports=int(getattr(meta,'fee',0)),event_evidence='direct_pump_self_cpi')
                candidates.append(e)
            except (ValueError,UnicodeError,struct.error) as ex:errors.append(str(ex)[:80])
        if len(candidates)>1:return [],errors+['CPI_TRADE_CARDINALITY']
        if not candidates:continue
        e=candidates[0];old=merged.get(a['ix'])
        if old:
            if old['raw_sha256']!=e['raw_sha256']:return [],errors+['LOG_CPI_EVENT_CONFLICT']
            e['event_evidence']='logs_and_direct_pump_self_cpi'
        merged[a['ix']]=e
    result=sorted(merged.values(),key=lambda e:e['ix'])
    for n,e in enumerate(result):e['event_n']=n
    return result,errors

def valuation_reason(e):
    if not e.get('layout_supported'): return 'unsupported_event_layout'
    if e.get('quote_mint') not in (ZERO,WSOL): return 'non_sol_quote'
    if e.get('mayhem_mode'): return 'mayhem_not_modelled'
    if e.get('cashback_fee_basis_points') or e.get('holder_rewards_bps'): return 'alternate_fee_mode'
    # Official TRADE_V3: buyback is a distribution of the protocol fee,
    # not another percentage of trade notional. Check the event arithmetic.
    if not 0<=e.get('buyback_fee_basis_points',0)<=10000: return 'buyback_rate_bound'
    expected_buyback=e['fee']*e.get('buyback_fee_basis_points',0)//10000
    if abs(e.get('buyback_fee',0)-expected_buyback)>1:return 'buyback_allocation_mismatch'
    for rate,amount in [('fee_basis_points','fee'),('creator_fee_basis_points','creator_fee')]:
        if e[amount]!=(e['sol_amount']*e[rate]+9999)//10000:return 'fee_arithmetic_mismatch'
    if e.get('shareholders'): return 'shared_creator_fee_rounding'
    if e['virtual_quote_reserves']!=e['virtual_sol_reserves'] or e['real_quote_reserves']!=e['real_sol_reserves']: return 'quote_reserve_semantics'
    if not 0<e['virtual_sol_reserves']<2**63 or not 0<e['virtual_token_reserves']<2**63: return 'invalid_virtual_reserves'
    if not 0<e['real_token_reserves']<e['virtual_token_reserves'] or e['real_sol_reserves']<=0: return 'empty_or_migrated_curve'
    if not 0<=e['fee_basis_points']+e['creator_fee_basis_points']<=1000: return 'fee_bound'
    return None
