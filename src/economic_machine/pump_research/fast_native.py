"""Checked ABI for Economic Machine's bounded native Pump research kernel."""
import ctypes as C
from .events import valuation_reason

POLICY_FIELDS='version expires_ms window_slots buyers positive_buckets net_lamports buyer_bps funder_bps roundtrip_bps notional minimum_real_sol maximum_quote_age_slots'.split()
EVENT_FIELDS='id slot tx_order ix wallet buy sol tokens virtual_sol virtual_token real_sol real_token protocol_bps creator_bps supported developer tx_id'.split()
RESULT_FIELDS='reason events buyers positive_buckets buy_lamports net_lamports buyer_bps funder_bps roundtrip_bps linked_buyers developer_sell quote_tokens quote_exit_lamports policy_version cutoff_slot execution_authority history_complete'.split()
REASONS='candidate observing policy_expired bad_input capacity unsupported breadth persistence demand concentration funding_concentration roundtrip developer_selling quote_stale liquidity invalidated'.split()
class Policy(C.Structure):_fields_=[(x,C.c_uint64) for x in POLICY_FIELDS]
class Event(C.Structure):_fields_=[(x,C.c_uint64) for x in EVENT_FIELDS]
class Result(C.Structure):_fields_=[(x,C.c_int64 if x=='net_lamports' else C.c_uint64) for x in RESULT_FIELDS]

def integer(v):
    if type(v) is not int or not 0<=v<2**64:raise ValueError('NATIVE_INTEGER_RANGE')
    return v

class Library:
    def __init__(self,path):
        self.lib=C.CDLL(str(path));l=self.lib
        l.machine_pump_abi.restype=C.c_uint64
        if l.machine_pump_abi()!=1:raise ValueError('PUMP_ABI')
        l.machine_pump_bytes.restype=C.c_uint64
        l.machine_pump_create.argtypes=[C.POINTER(Policy),C.c_uint64];l.machine_pump_create.restype=C.c_void_p
        l.machine_pump_destroy.argtypes=[C.c_void_p];l.machine_pump_destroy.restype=None
        l.machine_pump_event.argtypes=[C.c_void_p,C.POINTER(Event)];l.machine_pump_event.restype=C.c_uint64
        l.machine_pump_invalidate.argtypes=[C.c_void_p];l.machine_pump_invalidate.restype=None
        l.machine_pump_decide.argtypes=[C.c_void_p,C.c_uint64,C.c_uint64,C.POINTER(Result)];l.machine_pump_decide.restype=C.c_uint64
        l.machine_pump_funding.argtypes=[C.c_void_p,C.c_uint64,C.c_uint64,C.c_uint64];l.machine_pump_funding.restype=C.c_uint64
        l.machine_pump_quotes.argtypes=[C.POINTER(Event),C.c_uint64,C.POINTER(C.c_uint64),C.POINTER(C.c_uint64)];l.machine_pump_quotes.restype=C.c_uint64

class Engine:
    def __init__(self,library,policy,launch):
        self.library=library;self.launch=launch;self.ids={};self.events={};self.funders={};self.transactions={}
        self.policy=policy;self.closed=False
        p=Policy(**{k:integer(v) for k,v in dict(policy['parameters'],version=policy['version'],expires_ms=policy['expires_ms']).items()})
        self.ptr=library.lib.machine_pump_create(C.byref(p),integer(launch['slot']))
        if not self.ptr:raise ValueError('NATIVE_POLICY_REJECTED')
    def close(self):
        if not self.closed:self.library.lib.machine_pump_destroy(self.ptr);self.closed=True
    def invalidate(self):self.library.lib.machine_pump_invalidate(self.ptr)
    def ingest(self,e):
        if self.closed:raise ValueError('NATIVE_CLOSED')
        wallet=e['user'];key=(e['signature'],e['ix'])
        if wallet not in self.ids:
            if len(self.ids)>=256:self.invalidate();raise ValueError('NATIVE_WALLET_CAPACITY')
            self.ids[wallet]=len(self.ids)+1
        if key not in self.events:
            if len(self.events)>=1024:self.invalidate();raise ValueError('NATIVE_EVENT_CAPACITY')
            self.events[key]=len(self.events)+1
        if e['signature'] not in self.transactions:self.transactions[e['signature']]=len(self.transactions)+1
        event=encode_event(e,self.events[key],self.ids[wallet],wallet in (self.launch.get('payer'),self.launch.get('creator')),self.transactions[e['signature']])
        return REASONS[self.library.lib.machine_pump_event(self.ptr,C.byref(event))]
    def funding(self,wallet,funder,slot):
        if wallet not in self.ids:return False
        if funder not in self.funders:
            if len(self.funders)>=256:return False
            self.funders[funder]=len(self.funders)+1
        return bool(self.library.lib.machine_pump_funding(self.ptr,self.ids[wallet],self.funders[funder],integer(slot)))
    def decide(self,slot,now_ms):
        if self.closed:raise ValueError('NATIVE_CLOSED')
        r=Result();self.library.lib.machine_pump_decide(self.ptr,integer(slot),integer(now_ms),C.byref(r))
        result={k:getattr(r,k) for k in RESULT_FIELDS};result['reason']=REASONS[result['reason']]
        return result

def encode_event(e,event_id=1,wallet_id=1,developer=False,tx_id=1):
    supported=valuation_reason(e) is None
    values=dict(id=event_id,slot=e['slot'],tx_order=e.get('tx_order',0),ix=e['ix'],wallet=wallet_id,
                buy=int(e['is_buy']),sol=e['sol_amount'],tokens=e['token_amount'],
                virtual_sol=e.get('virtual_sol_reserves',0),virtual_token=e.get('virtual_token_reserves',0),
                real_sol=e.get('real_sol_reserves',0),real_token=e.get('real_token_reserves',0),
                protocol_bps=e.get('fee_basis_points',0),creator_bps=e.get('creator_fee_basis_points',0),
                supported=int(supported),developer=int(developer),tx_id=tx_id)
    return Event(**{k:integer(v) for k,v in values.items()})
