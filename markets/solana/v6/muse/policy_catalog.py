"""Closed public regime menu. A model response has no prices, sizes or instructions.

This is a 30-second control-plane hypothesis, not an extension of the v1 quote TTL.
An execution engine must independently require fresh market data on every quote.
"""
import json
from contract import Rejected,unique_object,require_activation
CATALOG={
    0:{'mode':'HALT','min_half_spread_ticks':0,'levels_per_side':0,'clip_lots':0},
    1:{'mode':'HOLD','min_half_spread_ticks':0,'levels_per_side':0,'clip_lots':0},
    2:{'mode':'QUOTE','min_half_spread_ticks':4,'levels_per_side':2,'clip_lots':2},
    3:{'mode':'QUOTE','min_half_spread_ticks':12,'levels_per_side':1,'clip_lots':1},
}
SYSTEM='Select one public market regime: c=0 unsafe data; c=1 no usable liquidity; c=2 balanced low volatility; c=3 elevated volatility or imbalance. Input s=spread ticks, i=imbalance bps, v=volatility bps, d=depth lots at 1/4/16 levels. Output only the schema. This is a regime label, never an order or price.'

def compact_request(base):
    source=json.loads(base['messages'][1]['content'])['features']
    result=dict(base)
    result['messages']=[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps({'s':source['spread_ticks'],'i':source['imbalance_bps'],'v':source['volatility_bps'],'d':[source['depth_lots_l1'],source['depth_lots_l4'],source['depth_lots_l16']]},separators=(',',':'))}]
    result['max_tokens']=256
    result['response_format']={'type':'json_schema','json_schema':{'name':'regime_v2','strict':True,'schema':{'type':'object','properties':{'c':{'type':'integer','enum':[0,1,2,3]}},'required':['c'],'additionalProperties':False}}}
    return result

def validate_compact(response,*,started_ns,now_ns,activation):
    require_activation(activation,now_ns)
    if not 0<=now_ns-started_ns<30_000_000_000:raise Rejected('CONTROL_DEADLINE')
    if response.get('model')!='meta/muse-spark-1.3-contributor' or response.get('provider') not in ('Meta','meta'):raise Rejected('ROUTE_ID')
    choices=response.get('choices')
    if not isinstance(choices,list) or len(choices)!=1 or choices[0].get('finish_reason')!='stop':raise Rejected('INCOMPLETE')
    message=choices[0].get('message',{})
    if message.get('refusal') or message.get('tool_calls'):raise Rejected('NON_POLICY_RESPONSE')
    content=message.get('content')
    if not isinstance(content,str) or len(content.encode())>128:raise Rejected('OUTPUT_SIZE')
    code=json.loads(content,object_pairs_hook=unique_object)
    if type(code) is not dict or set(code)!={'c'} or type(code['c']) is not int or code['c'] not in CATALOG:raise Rejected('CATALOG_CODE')
    return {'catalog_version':2,'code':code['c'],'definition':CATALOG[code['c']].copy(),
            'valid_until_monotonic_ns':started_ns+30_000_000_000,'execution_authority':False,'requires_current_market_and_mandate':True}
