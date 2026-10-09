// Fixed compiler for the owner-admitted Muse conditional DSL. All regime and
// inventory branches execute through upstream EconomicRuntime on fresh facts.
#include "machine/economics/runtime.hpp"
#include <new>
#include <cstring>
using namespace machine::economics;
using U=std::uint64_t;using I=std::int64_t;
struct Input {U now,observed,slot,oracle_slot,mark,sequence;I position;U bid,ask;I imbalance;U movement,depth;};
struct Quote {U slot,price,lots,reduce;};
struct Output {U error,reduce,fingerprint,generation,until,authority,count;I center;Quote quotes[4];U mode,version;};
static_assert(sizeof(Input)==96 && sizeof(Output)==208);
// One scan of the book per source frame, then O(1) facts per agent. Two best
// prices from distinct owners let each agent exclude its own liquidity exactly.
struct View {U seats,node,mark,publish,oracle_slot,sequence,buy{},sell{},bid{},bid_owner{~U{}},bid_other{},ask{},ask_owner{~U{}},ask_other{};};
static U read64(const unsigned char* d,U n) {U v;std::memcpy(&v,d+n,8);return v;}
static U read32(const unsigned char* d,U n) {std::uint32_t v;std::memcpy(&v,d+n,4);return v;}
static int prepare(const unsigned char* d,U size,U slot,U wall,View& v) noexcept {
    if(!d)return -1;
    if(size==199232 && !std::memcmp(d,"MPERPS01",8)){v.seats=16;v.node=4608;}
    else if(size==549440 && !std::memcmp(d,"MPERPS02",8)){v.seats=256;v.node=66048;}
    else return -1;
    if(d[137] || d[216]!=1)return -1;
    v.mark=read64(d,144);v.publish=read64(d,224);v.oracle_slot=read64(d,152);v.sequence=read64(d,160);
    if(!v.mark || v.mark>0xffffff || !v.publish || wall<v.publish || wall-v.publish>90)return -2;
    for(U n=0;n<v.seats*16;++n){const U o=v.node+n*48,x=read64(d,o),expiry=read64(d,o+8),until=read64(d,o+40);
        if(!x || expiry<slot || (until && until<=wall))continue;
        const U price=read32(d,o+16),owner=n/16;
        if(x>1000000 || !price || price>0xffffff || !d[512+owner*256+168])return -3;
        if(n%16<8){v.buy+=x;
            if(price>v.bid){if(owner!=v.bid_owner)v.bid_other=v.bid;v.bid=price;v.bid_owner=owner;}
            else if(owner!=v.bid_owner && price>v.bid_other)v.bid_other=price;
        }else{v.sell+=x;
            if(!v.ask || price<v.ask){if(owner!=v.ask_owner)v.ask_other=v.ask;v.ask=price;v.ask_owner=owner;}
            else if(owner!=v.ask_owner && (!v.ask_other || price<v.ask_other))v.ask_other=price;
        }
    }
    return 0;
}
static int input(const unsigned char* d,const View& v,U agent,U slot,U received,U now,U previous_mark,Input* x) noexcept {
    if(!x || agent>=v.seats)return -1;
    const U a=512+agent*256;
    if(!d[a+168] || read64(d,a+112)<500000)return -2;
    const U depth=v.buy+v.sell;const I numerator=((I)v.buy-(I)v.sell)*10000;
    const I imbalance=depth?(numerator>=0?numerator/(I)depth:-((-numerator+(I)depth-1)/(I)depth)):0;
    const U movement=previous_mark?std::min<U>(10000,(v.mark>previous_mark?v.mark-previous_mark:previous_mark-v.mark)*10000/previous_mark):0;
    *x={now,received,slot,v.oracle_slot,v.mark,v.sequence,(I)read64(d,a+120),agent==v.bid_owner?v.bid_other:v.bid,agent==v.ask_owner?v.ask_other:v.ask,imbalance,movement,depth};return 0;
}
extern "C" int mp_decode(const unsigned char* d,U size,U agent,U slot,U received,U now,U wall,U previous_mark,Input* x) noexcept {
    View v{};const int code=prepare(d,size,slot,wall,v);if(code)return code;
    return input(d,v,agent,slot,received,now,previous_mark,x);
}
// The publisher stores these fixed facts under the same generation as the arena.
// Inactive/underfunded seats have now==0 and cannot emit a decision.
extern "C" int mp_batch(const unsigned char* d,U size,U slot,U received,U now,U wall,Input* out,U capacity) noexcept {
    if(!out)return -1;
    View v{};const int code=prepare(d,size,slot,wall,v);if(code)return code;
    if(capacity!=v.seats)return -1;
    for(U s=0;s<capacity;++s){out[s]={};input(d,v,s,slot,received,now,0,&out[s]);}
    return 0;
}
struct Context {EconomicRuntime<64,1,2> runtime;U agent,event{},feed{},last_sequence{},expires,version;U p[6];};
extern "C" void* mp_create(U agent,U now,U expires,U version,const U* p) noexcept {
    if(!p || agent>255 || !version || now>=expires || p[0]<4 || p[0]>12 || p[1]<p[0] || p[1]>32 || p[2]<2000 || p[2]>8000 || p[3]<5 || p[3]>100 || p[4]<2 || p[4]>4 || p[5]<1 || p[5]>2)return nullptr;
    auto* c=new(std::nothrow) Context{};if(!c)return nullptr;c->agent=agent;c->expires=expires;c->version=version;for(U n=0;n<6;++n)c->p[n]=p[n];
    ProgramRegistration r{};auto& q=r.program;q.id=agent+1;q.version=version;q.policy_version=1;q.ttl_ns=3'000'000'000;q.dependency_count=5;
    const RegisterType sq{FactUnit::signed_quantity,1,0},price{FactUnit::price,1,2},rate{FactUnit::rate,0,0},quantity{FactUnit::quantity,1,0},flag{FactUnit::boolean,0,0};
    q.dependency_types[0]=sq;q.dependency_types[1]=price;q.dependency_types[2]=rate;q.dependency_types[3]=rate;q.dependency_types[4]=quantity;
    for(U n=0;n<5;++n)r.dependencies[n]={{n?0:agent+1,103,1,1,n+1},q.dependency_types[n].unit,q.dependency_types[n].asset,q.dependency_types[n].quote_asset};
    auto add=[&](StrategyOpcode op,U dst,U a,U b,U d,I imm,RegisterType type,U yes=0,U no=0){q.instructions[q.count++]={op,(std::uint32_t)dst,(std::uint32_t)a,(std::uint32_t)b,(std::uint32_t)d,imm,type,(std::uint32_t)yes,(std::uint32_t)no};};
    add(StrategyOpcode::observe,0,0,0,0,0,sq);
    add(StrategyOpcode::absolute,1,0,0,0,0,sq);
    add(StrategyOpcode::constant,2,0,0,0,p[4]*scale,sq);
    add(StrategyOpcode::compare,3,1,2,0,(I)Comparator::greater_equal,flag);
    add(StrategyOpcode::observe,4,0,0,0,2,rate);
    add(StrategyOpcode::absolute,5,4,0,0,0,rate);
    add(StrategyOpcode::constant,6,0,0,0,p[2]*scale/10000,rate);
    add(StrategyOpcode::compare,7,5,6,0,(I)Comparator::greater_equal,flag);
    add(StrategyOpcode::observe,8,0,0,0,3,rate);
    add(StrategyOpcode::constant,9,0,0,0,p[3]*scale/10000,rate);
    add(StrategyOpcode::compare,10,8,9,0,(I)Comparator::greater_equal,flag);
    add(StrategyOpcode::observe,11,0,0,0,4,quantity);
    add(StrategyOpcode::constant,12,0,0,0,0,quantity);
    add(StrategyOpcode::compare,13,11,12,0,(I)Comparator::equal,flag);
    add(StrategyOpcode::logical_or,14,7,10,0,0,flag);
    add(StrategyOpcode::logical_or,15,14,13,0,0,flag);
    add(StrategyOpcode::constant,16,0,0,0,scale,rate);
    add(StrategyOpcode::constant,17,0,0,0,0,rate);
    add(StrategyOpcode::select,18,15,16,17,0,rate);
    add(StrategyOpcode::score,19,18,0,0,0,rate);
    add(StrategyOpcode::branch,20,3,0,0,0,flag,21,23);
    add(StrategyOpcode::constant,21,0,0,0,p[5]*scale,quantity);
    add(StrategyOpcode::reduce,22,21,0,0,0,flag);
    add(StrategyOpcode::hold,23,0,0,0,0,flag);
    r.mandate={agent+1,103,1,1,expires,0,0,true};
    if(c->runtime.install(r,now)!=Error::okay){delete c;return nullptr;}return c;
}
extern "C" void mp_destroy(void* ptr) noexcept {delete static_cast<Context*>(ptr);}
extern "C" int mp_checkpoint(void* ptr,U generation) noexcept {if(!ptr)return -1;auto& c=*static_cast<Context*>(ptr);return (int)c.runtime.checkpoint(c.event,generation,true);}
extern "C" int mp_decide(void* ptr,const Input* input,Output* out) noexcept {
    if(!ptr || !input || !out){return -1;}*out={};auto& c=*static_cast<Context*>(ptr);auto& x=*input;
    auto fail=[&](Error e){out->error=(U)e;return (int)e;};
    if(x.now>=c.expires)return fail(Error::expired);
    if(x.mark<100 || x.mark>0xffffff-100 || x.position< -8 || x.position>8 || x.imbalance< -10000 || x.imbalance>10000 || x.movement>10000 || x.depth>4'096'000'000)return fail(Error::invalid);
    if(x.now<x.observed || x.slot<x.oracle_slot)return fail(Error::future);
    if(x.now-x.observed>=3'000'000'000 || x.slot-x.oracle_slot>100)return fail(Error::stale);
    if(x.sequence<c.last_sequence){return fail(Error::sequence);}c.last_sequence=x.sequence;++c.feed;
    const I values[5]={x.position*scale,(I)x.mark*scale,x.imbalance*scale/10000,(I)x.movement*scale/10000,(I)x.depth*scale};
    const FactUnit units[5]={FactUnit::signed_quantity,FactUnit::price,FactUnit::rate,FactUnit::rate,FactUnit::quantity};
    for(U n=0;n<5;++n){TypedFact f{};f.key={n?0:c.agent+1,103,1,1,n+1};f.unit=units[n];f.asset=(n==2 || n==3)?0:1;f.quote_asset=n==1?2:0;f.value=values[n];f.source=1;f.source_generation=1;f.sequence=c.feed;f.observed_ns=x.observed;f.valid_for_ns=3'000'000'000;f.status=FactStatus::coherent;FactDelta d{++c.event,c.event,FactOperation::snapshot,f};auto e=c.runtime.observe(d,x.now).error;if(e!=Error::okay)return fail(e);}
    auto e=c.runtime.poll(x.now,1);if(e!=Error::okay)return fail(e);RuntimeDecision d{};if(!c.runtime.pop(d))return fail(Error::missing);
    if(d.candidate.error!=Error::okay)return fail(d.candidate.error);
    if(d.candidate.action!=EconomicAction::hold && d.candidate.action!=EconomicAction::reduce)return fail(Error::unauthorized);
    out->reduce=d.candidate.action==EconomicAction::reduce;out->mode=d.candidate.score?3:2;out->version=c.version;
    out->fingerprint=d.candidate.diagnostic_fingerprint;out->generation=d.candidate.state_generation;out->until=d.candidate.valid_until_ns;out->authority=d.candidate.execution_authority;
    out->center=(I)x.mark+((c.agent&1)?1:-1)-2*x.position;
    const I spread=c.p[out->mode==3?1:0],clip=out->mode==3?1:c.p[5];const U levels=out->mode==3?1:2;
    I bid=std::min(out->center-spread,(I)x.mark-1),ask=std::max(out->center+spread,(I)x.mark+1);
    if(x.ask){bid=std::min(bid,(I)x.ask-1);}if(x.bid){ask=std::max(ask,(I)x.bid+1);}
    I buy=out->reduce?(x.position<0?-x.position:0):8-x.position,sell=out->reduce?(x.position>0?x.position:0):8+x.position;
    for(U n=0;n<levels;++n){I b=std::min(clip,buy),s=std::min(clip,sell);buy-=b;sell-=s;if(b)out->quotes[out->count++]={n,(U)(bid-(I)n*2),(U)b,out->reduce};if(s)out->quotes[out->count++]={8+n,(U)(ask+(I)n*2),(U)s,out->reduce};}
    return 0;
}
