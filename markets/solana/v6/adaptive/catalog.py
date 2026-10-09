"""Thirty reproducible adversaries; preferences never change the owner's scope."""
import copy,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'evolution'))
from schema import profile,digest,validate_program,validate_profile
ARCHETYPES=('inventory_maker','trend_follower','mean_reverter','imbalance_reader','liquidity_defender','regime_switcher')
STYLES=('capital_preserver','low_inventory','balanced','active','opportunistic')
class Graph:
    def __init__(self):self.nodes=[]
    def n(self,op,a=0,b=0,c=0,v=0):self.nodes.append([op,a,b,c,v]);return len(self.nodes)-1
    def constant(self,v):return self.n(2,v=v)
    def feature(self,v):return self.n(1,v=v)
    def sign(self,value,threshold,amplitude):
        hi=self.n(10,value,self.constant(threshold));lo=self.n(11,value,self.constant(-threshold));zero=self.constant(0)
        down=self.n(15,lo,self.constant(-amplitude),zero)
        return self.n(15,hi,self.constant(amplitude),down)

def persona(archetype,risk):
    name=ARCHETYPES[archetype]+'-'+STYLES[risk];p=profile('persona-'+name,('defensive','defensive','balanced','active','active')[risk])
    p['limits'].update(position=2+risk,clip=1 if risk<3 else 2,min_spread=(10,8,6,5,4)[risk],loss_ticks=500+risk*200)
    p['weights'].update(drawdown=(200,140,60,35,20)[risk],inventory=(30,24,8,4,2)[risk]);p['profile_hash']=digest({k:v for k,v in p.items() if k!='profile_hash'})
    g=Graph();inv=g.feature(0);mom=g.feature(2);imb=g.feature(1);vol=g.feature(3);depth=g.feature(4)
    skew=g.n(5,inv,g.constant(-max(1,4-risk//2)))
    if archetype in (1,2,5):
        trend=g.sign(mom,1,3+risk)
        if archetype==2:trend=g.n(9,trend)
        if archetype==5:trend=g.n(15,g.n(10,vol,g.constant(12)),trend,g.n(9,trend))
        skew=g.n(3,skew,trend)
    elif archetype==3:skew=g.n(3,skew,g.sign(imb,25,3+risk))
    spread=g.constant(p['limits']['min_spread'])
    if archetype in (0,4,5):spread=g.n(7,spread,g.n(6,vol,g.constant(24)))
    soft=g.n(10,g.n(8,inv),g.constant(max(1,p['limits']['position']-2)))
    one=g.constant(1);zero=g.constant(0);action=g.n(15,soft,one,zero)
    if archetype==4:action=g.n(15,g.n(11,depth,g.constant(2)),g.constant(2),action)
    clip=g.constant(p['limits']['clip']);program={'nodes':g.nodes,'outputs':[skew,spread,clip,action]}
    validate_program(program);validate_profile(p)
    return {'id':f'p{archetype*5+risk:02}','name':name,'archetype':archetype,'risk':risk,'profile':p,'program':program,'program_hash':digest(program)}
def personas():return [persona(a,r) for a in range(6) for r in range(5)]
def candidates(path):
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    rows=db.execute('SELECT g.attempt,g.accepted,p.body,a.body,g.report FROM generations g JOIN profiles p ON p.hash=g.profile JOIN attempts a ON a.id=g.attempt ORDER BY g.attempt').fetchall();db.close()
    return [{'attempt':i,'research_champion':bool(accepted),'profile':json.loads(p),'program':json.loads(a)['proposal']['program'],'research_report':json.loads(r)} for i,accepted,p,a,r in rows]
