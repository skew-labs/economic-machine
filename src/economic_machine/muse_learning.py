"""Read-only interpretation and causal research. Never a live policy installer.

Existing receipt attribution is kept separate from counterfactual forecasts.
Graphs produce research signals, never order permissions or fill claims.
"""
import ctypes as C
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sqlite3
import math
import hashlib
from decimal import Decimal

from .muse_maker import MODEL, canonical, digest, unique
from .muse_receipts import Receipts, HORIZONS, decimal, integer
from .muse_policy_worker import Budget, cost_limit

FEATURES = ["inventory_buckets", "flow_percent", "momentum_bps", "volatility_bps", "spread_ticks", "market_age_20ms"]
BOUNDS = [(-8, 8), (-100, 100), (-100, 100), (0, 100), (0, 4096), (0, 100)]


class ResearchBudget(Budget):
    """Separate research output allowance; never changes the live worker cap."""
    def reserve(self, req, now):
        body=canonical(req).encode()
        if len(body)>16000 or req.get('model')!=MODEL or req.get('max_tokens')!=4096:
            raise ValueError('research request bound')
        bound=Decimal(len(body)+1024)*Decimal('0.0000001')+Decimal(4096)*Decimal('0.0000002')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE');totals=self.totals();maximum=cost_limit(self.scope['maximum_usd'])
            if (totals['cost_breach'] or now>=self.scope['deadline_ms'] or totals['calls_reserved']>=self.scope['max_calls']
                or (maximum is not None and Decimal(totals['cost_committed_or_reserved_usd'])+bound>maximum)):
                raise ValueError('research budget or deadline exhausted')
            cur=self.db.execute('INSERT INTO calls(request_hash,reserved,status,created_ms) VALUES(?,?,?,?)',
                                (hashlib.sha256(body).hexdigest(),str(bound),'reserved',now))
        return cur.lastrowid


@lru_cache(maxsize=1)
def grammar():
    # Reuse only the validated numeric graph grammar. The Solana replay,
    # profile semantics and simulated fills are not imported into Pacifica.
    path = Path(__file__).resolve().parents[2]/"markets/solana/v6/evolution/schema.py"
    spec = importlib.util.spec_from_file_location("muse_numeric_grammar", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def baseline():
    # Predict no movement; expose observed volatility. Outputs are signal
    # interpretation only, not the v6 spread/clip/action interpretation.
    return {"nodes": [[2,0,0,0,0], [1,0,0,0,3], [2,0,0,0,1]], "outputs": [0,1,2,0]}


def lower_boolean_values(program):
    """Lossless bool -> numeric 0/1 at mixed select branches and outputs only.

    This is a typed compiler conversion, not an LLM retry or invented signal.
    Numeric conditions, forward references and other type mistakes still fail.
    """
    if type(program) is not dict or set(program)!={'nodes','outputs'}:raise ValueError('program shape')
    source=program['nodes'];outputs=program['outputs']
    if type(source) is not list or not 1<=len(source)<=40 or type(outputs) is not list or len(outputs)!=4:raise ValueError('program shape')
    nodes=[];mapping=[];types=[];casts={};zero=None;one=None
    def number(ref):
        nonlocal zero,one
        if types[ref]=='number':return mapping[ref]
        if ref not in casts:
            if zero is None:
                zero=len(nodes);nodes.append([2,0,0,0,0]);one=len(nodes);nodes.append([2,0,0,0,1])
            casts[ref]=len(nodes);nodes.append([15,mapping[ref],one,zero,0])
        return casts[ref]
    for i,node in enumerate(source):
        if type(node) is not list or len(node)!=5 or any(type(v) is not int for v in node):raise ValueError('node shape')
        op,a,b,c,v=node
        if op not in range(1,16):raise ValueError('unsupported opcode')
        refs=[] if op<=2 else [a] if op in (8,9,14) else [a,b,c] if op==15 else [a,b]
        if any(r<0 or r>=i for r in refs):raise ValueError('forward or cyclic reference')
        if (op<=2 and any((a,b,c))) or (op>2 and v) or (op in (8,9,14) and (b or c)) or (op not in (1,2,8,9,14,15) and c):raise ValueError('unused payload must be zero')
        kind='bool' if op in (10,11,12,13,14) else 'number'
        mapped=[mapping[r] for r in refs]
        if op==15:
            if types[a]!='bool':raise ValueError('numeric conditions are not accepted')
            if types[b]!=types[c]:mapped=[mapping[a],number(b),number(c)];kind='number'
            else:kind=types[b]
        fields=(mapped+[0,0,0])[:3]
        mapping.append(len(nodes));types.append(kind);nodes.append([op,*fields,v])
    result=[]
    for ref in outputs:
        if type(ref) is not int or not 0<=ref<len(source):raise ValueError('output reference')
        result.append(number(ref))
    lowered={'nodes':nodes,'outputs':result};grammar().validate_program(lowered)
    return lowered,{'kind':'lossless_boolean_to_0_or_1','cast_source_nodes':sorted(casts)}


class Node(C.Structure):
    _fields_ = [(x, C.c_int64) for x in ("op", "a", "b", "c", "value")]


class Output(C.Structure):
    _fields_ = [("error", C.c_uint64), ("values", C.c_int64*4),
                ("fingerprint", C.c_uint64), ("generation", C.c_uint64), ("authority", C.c_uint64)]


class Graph:
    def __init__(self, program, library):
        grammar().validate_program(program)
        self.lib = C.CDLL(str(library)); self.ptr = None
        self.lib.ml_create.argtypes = [C.POINTER(Node), C.c_uint64, C.POINTER(C.c_uint64)]
        self.lib.ml_create.restype = C.c_void_p
        self.lib.ml_step.argtypes = [C.c_void_p, C.POINTER(C.c_int64), C.c_uint64, C.POINTER(Output)]
        self.lib.ml_step.restype = C.c_int
        self.lib.ml_destroy.argtypes = [C.c_void_p]
        self.ptr = self.lib.ml_create((Node*len(program['nodes']))(*(Node(*n) for n in program['nodes'])),
                                     len(program['nodes']), (C.c_uint64*4)(*program['outputs']))
        if not self.ptr: raise ValueError("native research graph rejected")
        self.clock = 1

    def evaluate(self, features):
        if not self.ptr: raise ValueError("research graph closed")
        if len(features) != 6 or any(type(x) is not int or not lo <= x <= hi for x,(lo,hi) in zip(features,BOUNDS)):
            raise ValueError("research feature bounds")
        out = Output(); self.clock += 1
        code = self.lib.ml_step(self.ptr, (C.c_int64*6)(*features), self.clock, C.byref(out))
        if code or out.error or out.authority: raise ValueError("research cannot authorize execution")
        return list(out.values)

    def close(self):
        if self.ptr: self.lib.ml_destroy(self.ptr); self.ptr = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Store:
    def __init__(self, root, lease):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root/"research.sqlite")
        self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY, source_ms INTEGER UNIQUE, received_ms INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS intents(cid TEXT PRIMARY KEY, version INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS attributed_fills(id INTEGER PRIMARY KEY, version INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS pending_fills(id INTEGER PRIMARY KEY, body TEXT);
        CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY, inode TEXT, offset INTEGER, version INTEGER);
        ''')
        binding = {k:lease[k] for k in ('session_id','start_ms','deadline_ms')}
        body = canonical(binding)
        with self.db: self.db.execute("INSERT OR IGNORE INTO binding VALUES(1,?)", (body,))
        if self.db.execute("SELECT body FROM binding WHERE id=1").fetchone()[0] != body:
            self.db.close()
            raise ValueError("research session changed")
        self.lease = binding
        self.receipts = Receipts(self.root/"receipts.sqlite", "session:"+lease['session_id'], lease['start_ms'], lease['deadline_ms'])
        # Idempotently finish a receipt write after a crash between the two DBs.
        for body, in self.db.execute("SELECT body FROM attributed_fills"):
            self.receipts.trade(json.loads(body))

    def ingest(self, event, version):
        kind = event.get('kind')
        if kind == 'policy_installed':
            v = event.get('version')
            if type(v) is not int or v <= version: raise ValueError("research policy order")
            return v
        if kind == 'unsigned_intent' and event.get('op') == 'create_order':
            data = event['data']; cid = data['client_order_id']
            if data.get('symbol') != 'SOL' or type(cid) is not str or len(cid)>64 or version<=0:
                raise ValueError("unattributable order intent")
            body = canonical(data)
            prior = self.db.execute("SELECT version,body FROM intents WHERE cid=?", (cid,)).fetchone()
            if prior and prior != (version, body): raise ValueError("order identity changed")
            with self.db: self.db.execute("INSERT OR IGNORE INTO intents VALUES(?,?,?)", (cid,version,body))
            for old, in list(self.db.execute("SELECT body FROM pending_fills")):
                self._fill(json.loads(old))
        if kind == 'account_event':
            envelope = event.get('data', {})
            if envelope.get('channel') == 'account_trades':
                rows = envelope.get('data', [])
                if isinstance(rows, dict): rows=[rows]
                for row in rows:
                    mapping={'h':'history_id','i':'order_id','I':'client_order_id','s':'symbol','a':'amount','p':'price',
                             'f':'fee','n':'pnl','te':'event_type','ts':'side','t':'created_at','tc':'cause'}
                    self._fill({long:row[short] for short,long in mapping.items()})
        return version

    def _fill(self, value):
        if value.get('symbol')!='SOL' or not self.lease['start_ms']<=value.get('created_at',0)<=self.lease['deadline_ms']:
            return
        cid=value.get('client_order_id'); row=self.db.execute("SELECT version FROM intents WHERE cid=?",(cid,)).fetchone()
        encoded=canonical(value); ident=value['history_id']
        for table in ('attributed_fills','pending_fills'):
            prior=self.db.execute(f"SELECT body FROM {table} WHERE id=?",(ident,)).fetchone()
            if prior and prior[0]!=encoded: raise ValueError("fill identity changed")
        if row is None:
            with self.db:self.db.execute("INSERT OR IGNORE INTO pending_fills VALUES(?,?)",(ident,encoded))
            return
        # Validate before committing the ownership record. Ownership is written
        # first; restart repairs a missing derived receipt, never an orphan P&L.
        for k in ('history_id','order_id','created_at'):integer(value[k])
        if value['event_type'] not in ('fulfill_maker','fulfill_taker') or value['side'] not in ('open_long','close_short','open_short','close_long'):
            raise ValueError('receipt event/side')
        if value['cause'] not in ('normal','liquidation','adl','ADL'):raise ValueError('receipt cause')
        if decimal(value['amount'])<=0 or decimal(value['price'])<=0:raise ValueError('nonpositive fill')
        decimal(value['fee']);decimal(value['pnl'])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO attributed_fills VALUES(?,?,?)",(ident,row[0],encoded))
            self.db.execute("DELETE FROM pending_fills WHERE id=?",(ident,))
        self.receipts.trade(value)

    def frame(self, status, observer, now):
        if status.get('session_id') != self.lease['session_id']:raise ValueError('runtime session changed')
        if not self.lease['start_ms']<=now<=self.lease['deadline_ms']:return False
        book=status.get('public_book',{}); source=book.get('source_ms')
        if type(source) is not int or not 0<=now-source<=2000 or not 0<=now-status.get('updated_ms',0)<=2000: return False
        bid,ask=decimal(str(book['bid'])),decimal(str(book['ask']))
        if not 0<bid<ask or bid*100!=(bid*100).to_integral_value() or ask*100!=(ask*100).to_integral_value():return False
        previous=self.db.execute("SELECT source_ms,body FROM frames ORDER BY id DESC LIMIT 1").fetchone()
        if previous and source<=previous[0]: return False
        mid=(bid+ask)/2
        model=observer.get('model',{}); model_ms=model.get('at_wall_ms',0)
        usable=type(model_ms) is int and 0<=now-model_ms<=2000 and observer.get('archive_failed') is False
        flow=model.get('flow') if usable and model.get('onchain_available') is True else None
        sigma=model.get('sigma_bps') if usable else None
        flow=flow if type(flow) in (int,float) and math.isfinite(flow) else None
        sigma=sigma if type(sigma) in (int,float) and math.isfinite(sigma) and sigma>=0 else None
        def clip(x,lo,hi):return max(lo,min(hi,int(x)))
        old_mid=Decimal(json.loads(previous[1])['mid']) if previous else mid
        features=[clip(decimal(str(status.get('net_SOL',0)))*mid*8/1200,-8,8),
                  clip(Decimal(str(flow))*100,-100,100) if type(flow) in (int,float) else 0,
                  clip((mid-old_mid)/old_mid*10000,-100,100),
                  clip(sigma,0,100) if type(sigma) in (int,float) else 0,
                  clip((ask-bid)*100,0,4096),clip((now-source)//20,0,100)]
        frame={'source_ms':source,'received_ms':now,'bid':str(bid),'ask':str(ask),'mid':str(mid),'features':features,
               'flow_available':flow is not None,'volatility_available':sigma is not None,'live_policy_version':status.get('policy_version')}
        with self.db:self.db.execute("INSERT INTO frames(source_ms,received_ms,body) VALUES(?,?,?)",(source,now,canonical(frame)))
        for ident,body in self.db.execute("SELECT id,body FROM attributed_fills"):
            fill=json.loads(body)
            for horizon in HORIZONS:
                if fill['created_at']+horizon<=source<=fill['created_at']+horizon+250:
                    try:self.receipts.mark(ident,horizon,market_ms=source,received_ms=now,bid=str(bid),ask=str(ask))
                    except ValueError:pass # Explicitly missing, not zero/winning.
        return True

    def report(self):
        economics=self.receipts.report()
        count=self.db.execute('SELECT COUNT(*) FROM attributed_fills').fetchone()[0]
        if economics['fills']!=count:raise ValueError('attributed receipt reconciliation required')
        versions={}
        for version,body in self.db.execute('SELECT version,body FROM attributed_fills'):
            fill=json.loads(body);v=versions.setdefault(str(version),{'fills':0,'reported_pnl_after_fee_usd':Decimal(0),'signed_fee_usd':Decimal(0)})
            v['fills']+=1;v['reported_pnl_after_fee_usd']+=decimal(fill['pnl']);v['signed_fee_usd']+=decimal(fill['fee'])
        versions={v:{k:str(x) if isinstance(x,Decimal) else x for k,x in row.items()} for v,row in versions.items()}
        return {'session_id':self.lease['session_id'],'frames':self.db.execute('SELECT COUNT(*) FROM frames').fetchone()[0],
                'attributed_fills':count,
                'unattributed_fill_records':self.db.execute('SELECT COUNT(*) FROM pending_fills').fetchone()[0],
                'economics':economics,'actual_funding_coverage':'not collected by this journal reader',
                'executed_policy_receipts':versions,
                'attribution_method':'session journal client-order-id to installed policy version',
                'receipt_history_completeness_verified':False,
                'own_fill_learning_ready':count>=20 and economics['markouts']['1000']['samples']*5>=count*4,
                'orders_submitted':0,'live_policy_writes':0,'execution_authority':False}

    def close(self):self.receipts.close();self.db.close()


SYSTEM='''Interpret available public SOL market observations for read-only Economic Machine research.
Weights stay frozen. Missing pool-level records, exact queue rank and a calibrated fill model are UNKNOWN fields, not reasons to refuse interpreting available BBO, flow or volatility. Do not invent them. Stale/missing actual input is a distinct data-quality state. You have no order permission, trading mode, wallet or live policy channel.
Return exactly {analysis,program,focus}. analysis={trend:up|down|flat|mixed|unknown,volatility:calm|elevated|unknown,usable:[unique available signals from bbo,flow,volatility,own_fill_markout],missing:[unique unavailable signals from flow,volatility,own_fill_markout,queue,pool_detail]}. focus is one of flow,volatility,inventory,source_quality.
Invent a bounded acyclic numeric signal graph, not executable source code. Prefer a compact 6-12 node hypothesis. You may change graph wiring and conditional logic, not merely constants. program={nodes:[[op,a,b,c,value],...],outputs:[move,volatility,confidence,regime]}. Max 40 nodes, integer values only. Every reference points backwards; all unused fields are 0. Ops:1 feature(value index),2 constant(value),3 add(a,b),4 subtract(a,b),5 multiply(a,b),6 min(a,b),7 max(a,b),8 abs(a),9 negate(a),10 greater(a,b) boolean,11 less(a,b) boolean,12 and(a,b),13 or(a,b),14 not(a),15 select(boolean a,b,c) with same-type branches. All intermediates must stay in [-10000,10000] for every allowed input. Feature ranges:0 normalized inventory[-8,8],1 signed flow percent[-100,100],2 preceding observed momentum bps[-100,100],3 sigma bps[0,100],4 BBO spread ticks[0,4096],5 market age in 20ms units[0,100]. Missing flow/volatility features are neutral zero and availability is separately reported.
Outputs are numeric node references, clamped by the native research engine: expected 1-second mid-price move in $0.01 ticks [-24,24]; volatility bucket [0,32]; confidence class [0,2]; regime label [0,2]. These are observations/hypotheses, never order, clip or trade-action instructions. The graph always executes HOLD with execution_authority=false.
Boolean values at outputs or mixed select branches are losslessly lowered to numeric 0/1 by the adapter. Conditions must still be Boolean. All arithmetic and interval bounds are checked after lowering.
Evaluation uses only observations collected after your response has completed and a purge interval. Future forecast accuracy is a research proxy, not trading P&L or a fill model. Existing actual receipt P&L includes signed fees already. These receipts belong to previously executed policies, never to your untraded candidate. No own fills means no demonstrated learning from own fills. Classify usable/missing exactly from supplied availability: bbo is usable; flow/volatility usable only when all supplied frames have that availability; own_fill_markout usable only when own_fill_evidence.ready is true. Supply a structurally different graph using the parent and prior future-window evaluation feedback. Do not assert profitability, always quote, always pause, or change any live control.'''


def research_request(parent, frames, feedback, own):
    grammar().validate_program(parent)
    # No addresses, IDs, raw receipts, user text, credentials or arbitrary
    # runtime dictionaries enter this model request.
    public=[]
    for f in frames[-16:]:
        public.append({k:f[k] for k in ('features','flow_available','volatility_available')})
    econ=own.get('economics',{})
    own_summary={'count':own.get('attributed_fills',0),'ready':own.get('own_fill_learning_ready',False),
                 'candidate_has_never_traded':True}
    if own_summary['count']:
        own_summary['reported_pnl_after_fee_usd']=str(decimal(econ['reported_trade_pnl_after_fee_usd']))
        own_summary['signed_fee_usd']=str(decimal(econ['signed_trade_fee_usd']))
        own_summary['markouts']={h:{k:row[k] for k in ('samples','missing','markout_after_fee_usd')} for h,row in econ['markouts'].items()}
    body={'parent_program':parent,'public_observations':public,
          'previous_future_evaluation':{k:feedback[k] for k in ('labels','parent_mae_ticks','candidate_mae_ticks','improved_forecast') if k in feedback},
          'own_fill_evidence':own_summary,
          'unavailable':['queue','pool_detail'],'outputs_are_research_only':True}
    return {'model':MODEL,'max_tokens':4096,'stream':False,'reasoning':{'effort':'minimal','exclude':True},
            'provider':{'only':['meta'],'allow_fallbacks':False,'require_parameters':True,'data_collection':'allow','max_price':{'prompt':0.10,'completion':0.20}},
            'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':canonical(body)}],
            'response_format':{'type':'json_object'}}


def proposal(raw, *, lower_flags=False):
    value=json.loads(raw,object_pairs_hook=unique)
    if value.get('model')!=MODEL or value.get('provider') not in ('Meta','meta'):raise ValueError('research provider')
    choices=value.get('choices');
    if type(choices) is not list or len(choices)!=1 or choices[0].get('finish_reason')!='stop':raise ValueError('research incomplete response')
    m=choices[0].get('message',{})
    if m.get('tool_calls') or m.get('refusal') or type(m.get('content')) is not str:raise ValueError('research response shape')
    result=json.loads(m['content'],object_pairs_hook=unique)
    if type(result) is not dict or set(result)!={'analysis','program','focus'}:raise ValueError('research fields')
    a=result['analysis']
    if type(a) is not dict or set(a)!={'trend','volatility','usable','missing'}:raise ValueError('analysis fields')
    if a['trend'] not in ('up','down','flat','mixed','unknown') or a['volatility'] not in ('calm','elevated','unknown'):raise ValueError('analysis values')
    for key,allowed in [('usable',{'bbo','flow','volatility','own_fill_markout'}),('missing',{'flow','volatility','own_fill_markout','queue','pool_detail'})]:
        if type(a[key]) is not list or any(type(x) is not str or x not in allowed for x in a[key]) or len(a[key])!=len(set(a[key])):raise ValueError('analysis signals')
    if set(a['usable'])&set(a['missing']):raise ValueError('contradictory analysis')
    if result['focus'] not in ('flow','volatility','inventory','source_quality'):raise ValueError('research focus')
    if lower_flags:
        result['program'],result['normalization']=lower_boolean_values(result['program'])
    else:grammar().validate_program(result['program'])
    return result


def validate_available(result, frames, own):
    # Availability is assigned by observed input, not by model confidence.
    if not frames:raise ValueError('public observations required')
    available={'bbo'}
    if all(f['flow_available'] for f in frames[-16:]):available.add('flow')
    if all(f['volatility_available'] for f in frames[-16:]):available.add('volatility')
    if own.get('own_fill_learning_ready') is True:available.add('own_fill_markout')
    missing={'flow','volatility','own_fill_markout','queue','pool_detail'}-available
    a=result['analysis']
    if set(a['usable'])!=available or set(a['missing'])!=missing:
        raise ValueError('model availability differs from supplied evidence')
    if 'volatility' not in available and a['volatility']!='unknown':raise ValueError('unobserved volatility')
    return result


def compare(parent,candidate,frames,*,completed_ms,cutoff_id,library):
    # A candidate can never use its proposal/training interval as holdout.
    if any(f['id']<=cutoff_id or f['source_ms']<=completed_ms+2000 or f['received_ms']<f['source_ms'] for f in frames):raise ValueError('future evaluation fence')
    if any(b['id']<=a['id'] or b['source_ms']<=a['source_ms'] for a,b in zip(frames,frames[1:])):raise ValueError('ordered future observations required')
    errors=[Decimal(0),Decimal(0)];labels=0;confidence=[0,0]
    with Graph(parent,library) as a,Graph(candidate,library) as b:
        for i,frame in enumerate(frames):
            future=next((f for f in frames[i+1:] if f['source_ms']>=frame['source_ms']+1000),None)
            if future is None or future['source_ms']>frame['source_ms']+1250:continue
            if frame['received_ms']>frame['source_ms']+2000 or future['received_ms']>future['source_ms']+2000:continue
            move=(Decimal(future['mid'])-Decimal(frame['mid']))*100
            for j,engine in enumerate((a,b)):
                out=engine.evaluate(frame['features']);errors[j]+=abs(Decimal(out[0])-move);confidence[j]+=int(out[2]>0)
            labels+=1
    return {'labels':labels,'parent_mae_ticks':float(errors[0]/labels) if labels else None,
            'candidate_mae_ticks':float(errors[1]/labels) if labels else None,
            'improved_forecast':labels>=20 and errors[1]<errors[0],
            'score_kind':'future_mid_forecast_error_not_pnl','fill_model_validated':False,
            'live_promotion_eligible':False,'execution_authority':False,'orders_submitted':0,
            'candidate_confident_samples':confidence[1],'window_hash':digest(frames),
            'completed_ms':completed_ms,'cutoff_id':cutoff_id,'samples_after_proposal_only':True}
