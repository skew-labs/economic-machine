"""Owner profiles and model-editable acyclic numeric strategy programs."""
import hashlib,json
FEATURES=['inventory','imbalance','momentum','volatility','depth','age']
BOUNDS=[(-8,8),(-100,100),(-100,100),(0,100),(0,4096),(0,100)]
OPS={1:'feature',2:'constant',3:'add',4:'subtract',5:'multiply',6:'min',7:'max',8:'abs',9:'negate',10:'greater',11:'less',12:'and',13:'or',14:'not',15:'select'}
MAX_NODES=40
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def integer(x):
    if type(x) is not int:raise ValueError('integer required')
    return x
def validate_program(p):
    if type(p) is not dict or set(p)!={'nodes','outputs'}:raise ValueError('program shape')
    if type(p['nodes']) is not list or not 1<=len(p['nodes'])<=MAX_NODES:raise ValueError('node count')
    intervals=[];types=[]
    for i,node in enumerate(p['nodes']):
        if type(node) is not list or len(node)!=5:raise ValueError('node shape')
        op,a,b,c,v=map(integer,node)
        if op not in OPS:raise ValueError('unsupported opcode')
        refs=[] if op<=2 else [a] if op in (8,9,14) else [a,b,c] if op==15 else [a,b]
        if any(r<0 or r>=i for r in refs):raise ValueError('forward or cyclic reference')
        if (op<=2 and any((a,b,c))) or (op>2 and v) or (op in (8,9,14) and (b or c)) or (op not in (1,2,8,9,14,15) and c):raise ValueError('unused payload must be zero')
        kind='number'
        if op==1:
            if not 0<=v<len(FEATURES):raise ValueError('feature index')
            interval=BOUNDS[v]
        elif op==2:
            if not -10000<=v<=10000:raise ValueError('constant bound')
            interval=(v,v)
        elif op in (12,13,14):
            if any(types[r]!='bool' for r in refs):raise ValueError('boolean operands')
            kind='bool';interval=(0,1)
        elif op==15:
            if types[a]!='bool' or types[b]!=types[c]:raise ValueError('select types')
            kind=types[b];interval=(min(intervals[b][0],intervals[c][0]),max(intervals[b][1],intervals[c][1]))
        else:
            if any(types[r]!='number' for r in refs):raise ValueError('numeric operands')
            lo,hi=intervals[a];bl,bh=intervals[b] if len(refs)>1 else (0,0)
            if op==3:interval=(lo+bl,hi+bh)
            elif op==4:interval=(lo-bh,hi-bl)
            elif op==5:
                products=[x*y for x in (lo,hi) for y in (bl,bh)];interval=(min(products),max(products))
            elif op==6:interval=(min(lo,bl),min(hi,bh))
            elif op==7:interval=(max(lo,bl),max(hi,bh))
            elif op==8:interval=(0 if lo<=0<=hi else min(abs(lo),abs(hi)),max(abs(lo),abs(hi)))
            elif op==9:interval=(-hi,-lo)
            else:kind='bool';interval=(0,1)
        if min(interval)<-10000 or max(interval)>10000:raise ValueError('intermediate interval bound')
        intervals.append(interval);types.append(kind)
    if type(p['outputs']) is not list or len(p['outputs'])!=4:raise ValueError('four outputs required')
    for r in p['outputs']:
        if not 0<=integer(r)<len(types) or types[r]!='number':raise ValueError('numeric output reference')
    return p
def structure(p):
    validate_program(p)
    return digest({'nodes':[[*n[:4],0 if n[0]==2 else n[4]] for n in p['nodes']],'outputs':p['outputs']})
def validate_meta(m):
    if type(m) is not dict or set(m)!={'parent_mode','focus','lesson'}:raise ValueError('meta shape')
    if m['parent_mode'] not in ('champion','diverse','recent'):raise ValueError('parent mode')
    if type(m['focus']) is not list or not 1<=len(m['focus'])<=3 or any(x not in ('inventory','adverse_selection','momentum','liquidity','drawdown','churn') for x in m['focus']):raise ValueError('search focus')
    if type(m['lesson']) is not str or len(m['lesson'].encode())>600:raise ValueError('lesson bound')
    return m
def profile(user,style):
    if type(user) is not str or not user or len(user)>80:raise ValueError('user namespace')
    settings={'defensive':(4,1,8,{'pnl':100,'drawdown':150,'inventory':20,'adverse':80,'churn':4}),
        'balanced':(6,1,6,{'pnl':100,'drawdown':60,'inventory':8,'adverse':40,'churn':2}),
        'active':(8,2,4,{'pnl':100,'drawdown':25,'inventory':2,'adverse':20,'churn':1})}
    if style not in settings:raise ValueError('unknown style')
    position,clip,spread,weights=settings[style]
    p={'version':1,'user_namespace':user,'style':style,'actor_seat':{'defensive':0,'balanced':2,'active':4}[style],'weights':weights,
       'limits':{'position':position,'clip':clip,'min_spread':spread,'max_spread':32,'max_skew':24,'loss_ticks':1500},
       'model_data_scope':'public_development_aggregates','live_promotion':False}
    p['profile_hash']=digest(p);return p
def validate_profile(p):
    if type(p) is not dict or set(p)!={'version','user_namespace','style','actor_seat','weights','limits','model_data_scope','live_promotion','profile_hash'}:raise ValueError('profile shape')
    if p['version']!=1 or type(p['user_namespace']) is not str or not 1<=len(p['user_namespace'])<=80 or p['style'] not in ('defensive','balanced','active'):raise ValueError('profile identity')
    if not 0<=integer(p['actor_seat'])<12 or type(p['live_promotion']) is not bool or p['model_data_scope']!='public_development_aggregates':raise ValueError('profile scope')
    body={k:v for k,v in p.items() if k!='profile_hash'}
    if digest(body)!=p['profile_hash']:raise ValueError('profile hash')
    lim=p['limits']
    if type(lim) is not dict or set(lim)!={'position','clip','min_spread','max_spread','max_skew','loss_ticks'}:raise ValueError('limit shape')
    if not 1<=integer(lim['position'])<=8 or not 1<=integer(lim['clip'])<=2:raise ValueError('owner scope widened')
    if not 2<=integer(lim['min_spread'])<=integer(lim['max_spread'])<=32 or not 0<=integer(lim['max_skew'])<=24:raise ValueError('price scope')
    if not 1<=integer(lim['loss_ticks'])<=1500:raise ValueError('loss scope')
    if set(p['weights'])!={'pnl','drawdown','inventory','adverse','churn'} or any(not 0<=integer(v)<=1000 for v in p['weights'].values()):raise ValueError('objective weights')
    return p
def seed(p):
    # All logic is represented in the same editable graph used by new candidates.
    spread=p['limits']['min_spread']
    return {'nodes':[[1,0,0,0,0],[2,0,0,0,-2],[5,0,1,0,0],[2,0,0,0,spread],
        [8,0,0,0,0],[2,0,0,0,2],[10,4,5,0,0],[2,0,0,0,1],[2,0,0,0,0],[15,6,7,8,0]],'outputs':[2,3,7,9]}
def evaluate_python(p,features):
    validate_program(p);r=[]
    for op,a,b,c,v in p['nodes']:
        if op==1:x=features[v]
        elif op==2:x=v
        elif op==3:x=r[a]+r[b]
        elif op==4:x=r[a]-r[b]
        elif op==5:x=r[a]*r[b]
        elif op==6:x=min(r[a],r[b])
        elif op==7:x=max(r[a],r[b])
        elif op==8:x=abs(r[a])
        elif op==9:x=-r[a]
        elif op==10:x=r[a]>r[b]
        elif op==11:x=r[a]<r[b]
        elif op==12:x=r[a] and r[b]
        elif op==13:x=r[a] or r[b]
        elif op==14:x=not r[a]
        else:x=r[b] if r[a] else r[c]
        r.append(x)
    out=[r[i] for i in p['outputs']]
    return [round(max(-24,min(24,out[0]))),round(max(0,min(32,out[1]))),round(max(0,min(2,out[2]))),round(max(0,min(2,out[3])))]
