"""Binary devnet ABI. This client never accepts model-generated instruction bytes."""
import os,struct
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey
LAYOUT=os.environ.get('MP_LAYOUT','compact16')
if LAYOUT not in ('compact16','wide256','production256'):raise RuntimeError('unknown market layout')
SEATS=16 if LAYOUT=='compact16' else 256
PAGES=16 if LAYOUT=='compact16' else 64
MAGIC={'compact16':b'MPERPS01','wide256':b'MPERPS02','production256':b'MPERPS03'}[LAYOUT]
SEAT=512
NODE=SEAT+SEATS*256
ROOT=NODE+SEATS*16*48
MID=ROOT+64
DIR=MID+16384
PAGE=DIR+131072
SIZE=PAGE+PAGES*2176
TOKEN=Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')

def meta(k, writable=False, signer=False):
    return AccountMeta(k, signer, writable)

def command(program, market, signer, op, seat=0, payload=b'', seq=1, epoch=1, deadline=2**64-1, observed=0, extras=(), market_signer=False):
    data=struct.pack('<BBHHHQQQQQ',1,op,0,seat,len(payload),epoch,seq,deadline,1,observed)+payload
    return Instruction(program,data,[meta(market,True,market_signer),meta(signer,False,True),*extras])

def initialize(program,market,creator,mint,vault,tick_value,*,feed=None):
    """Distinct MPERPS03 initialization; the program validates the receiver data."""
    if not 0<tick_value<=1_000_000:raise ValueError('tick value outside supported range')
    production=LAYOUT=='production256'
    if production!=(feed is not None):raise ValueError('production initialization requires a pinned receiver account')
    authority,bump=Pubkey.find_program_address([b'vault',bytes(market)],program)
    extras=[meta(mint),meta(vault,True),meta(authority),meta(TOKEN)]
    if production:extras.append(meta(feed))
    return command(program,market,creator,0,payload=bytes([bump])+struct.pack('<Q',tick_value),extras=extras,market_signer=True)

def quotes(entries,policy_until=0,*,slide=False):
    """entries: (logical_slot, price_ticks, lots, expiry_slot, client_id, reduce_only)."""
    return bytes([len(entries),int(policy_until>0)|(2 if slide else 0)])+policy_until.to_bytes(6,'little')+b''.join(struct.pack('<BBHIQQQ',k,int(r),0,p,x,t,c) for k,p,x,t,c,r in entries)

def ioc(side, lots, price, *, minimum=0, budget=2**64-1, visits=16, reduce=False, client=0, policy_until=0):
    if policy_until and client:raise ValueError('IOC deadline occupies the client field')
    return struct.pack('<BBBBIQQQQ',side,int(reduce),visits,int(policy_until>0),price,lots,minimum,budget,policy_until or client)

def seat_state(data,s):
    a=SEAT+s*256
    return {'seat':s,'owner':str(Pubkey.from_bytes(data[a:a+32])), 'active':bool(data[a+168]),
            'collateral':struct.unpack_from('<Q',data,a+112)[0], 'position':struct.unpack_from('<q',data,a+120)[0],
            'quote':int.from_bytes(data[a+128:a+144],'little',signed=True),
            'funding_snapshot':struct.unpack_from('<q',data,a+144)[0],
            'sequence':struct.unpack_from('<Q',data,a+72)[0], 'epoch':struct.unpack_from('<Q',data,a+64)[0],
            'command_digest':data[a+80:a+112].hex(),'session':str(Pubkey.from_bytes(data[a+32:a+64])),
            'session_expiry':struct.unpack_from('<Q',data,a+152)[0], 'session_limited':bool(data[a+169]),
            'session_mask':struct.unpack_from('<Q',data,a+176)[0], 'session_clip':struct.unpack_from('<Q',data,a+184)[0],
            'session_turnover_remaining':struct.unpack_from('<Q',data,a+192)[0]}

def snapshot(data):
    u=lambda o:struct.unpack_from('<Q',data,o)[0]
    seats=[seat_state(data,s) for s in range(SEATS) if data[SEAT+s*256+168]]
    funding=struct.unpack_from('<q',data,176)[0]
    for s in seats:
        s['funding_adjusted_quote']=s['quote']-s['position']*(funding-s['funding_snapshot'])
        s['equity']=s['collateral']+s['funding_adjusted_quote']+s['position']*u(144)*u(168)
    orders=[]
    for n in range(SEATS*16):
        o=NODE+n*48; x=u(o)
        if x:orders.append({'seat':n//16,'slot':n%16,'side':n%16//8,'lots':x,'price':struct.unpack_from('<I',data,o+16)[0], 'expiry':u(o+8),'policy_until':u(o+40)})
    return {'oracle':u(144),'oracle_slot':u(152),'oracle_mode':data[216],'oracle_publish_time':u(224),
            'funding_observed_seconds':u(256),'funding_window_start':u(264),'funding_updates':u(328),
            'market_sequence':u(160),'funding':funding,'fees':u(192),
            'deposits':u(200),'withdrawals':u(208),'halted':bool(data[137]),
            'insurance':u(336),'insurance_contributions':u(344),'insurance_spent':u(352),
            'adl_loss':u(360),'resolutions':u(368),'resolved':bool(data[217]),
            'resolution_price':u(376),'resolution_slot':u(384),
            'pinned_oracle_account':str(Pubkey.from_bytes(data[400:432])) if data[:8]==b'MPERPS03' else None,
            'seats':seats,'orders':orders}

def assert_conservation(data,vault_amount):
    st=snapshot(data)
    assert sum(s['position'] for s in st['seats'])==0,st
    assert sum(s['funding_adjusted_quote'] for s in st['seats'])==0,st
    assert sum(s['collateral'] for s in st['seats'])+st['fees']+st['insurance']==vault_amount,st
    assert st['deposits']+st['insurance_contributions']-st['withdrawals']==vault_amount,st
    return st
