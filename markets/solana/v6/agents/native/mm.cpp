// The upstream event runtime owns typed observations, TTL and decision scheduling.
// This adapter adds a bounded Solana quote stencil; it never signs or does I/O.
#include "machine/economics/runtime.hpp"
#include <new>
using namespace machine::economics;
using U = std::uint64_t; using I = std::int64_t;
struct Quote { U slot, price, lots, reduce; };
struct Output { U error, reduce, fingerprint, generation, until, authority, count; I center; Quote quotes[4]; };
struct Input { U now, observed, slot, oracle_slot, mark, market_sequence; I position; U best_bid, best_ask; };
static_assert(sizeof(Input)==72 && sizeof(Output)==192);
struct Context {
    EconomicRuntime<64,1,2> runtime;
    U agent, event{}, feed{}, last_market{}, catalog{2}, policy_until{UINT64_MAX};
    explicit Context(U a):agent(a) {}
};
extern "C" void* mm_create(U agent,U now,U expires) noexcept {
    if(agent>1 || now>=expires) return nullptr;
    auto* c=new(std::nothrow) Context(agent); if(!c)return nullptr;
    ProgramRegistration r{}; auto& p=r.program;
    p.id=agent+1;p.version=1;p.policy_version=1;p.ttl_ns=10'000'000'000;p.dependency_count=2;p.count=8;
    const RegisterType sq{FactUnit::signed_quantity,1,0}, price{FactUnit::price,1,2}, flag{FactUnit::boolean,0,0};
    p.dependency_types[0]=sq;p.dependency_types[1]=price;
    p.instructions[0]={StrategyOpcode::observe,0,0,0,0,0,sq};
    p.instructions[1]={StrategyOpcode::absolute,1,0,0,0,0,sq};
    p.instructions[2]={StrategyOpcode::constant,2,0,0,0,4*scale,sq};
    p.instructions[3]={StrategyOpcode::compare,3,1,2,0,static_cast<I>(Comparator::greater_equal),flag};
    p.instructions[4]={StrategyOpcode::branch,4,3,0,0,0,flag,5,7};
    p.instructions[5]={StrategyOpcode::constant,5,0,0,0,2*scale,{FactUnit::quantity,1,0}};
    p.instructions[6]={StrategyOpcode::reduce,6,5,0,0,0,flag};
    p.instructions[7]={StrategyOpcode::hold,7,0,0,0,0,flag};
    r.dependencies[0]={{agent+1,103,1,1,1},sq.unit,1,0};
    r.dependencies[1]={{0,103,1,1,2},price.unit,1,2};
    r.mandate={agent+1,103,1,1,expires,0,0,true};
    if(c->runtime.install(r,now)!=Error::okay){delete c;return nullptr;} return c;
}
extern "C" void mm_destroy(void* p) noexcept {delete static_cast<Context*>(p);}
extern "C" int mm_policy(void* p,U code,U now,U expires) noexcept {
    if(!p || code>3 || expires<=now)return -1;
    auto& c=*static_cast<Context*>(p);c.catalog=code;c.policy_until=expires;return 0;
}
// Caller acknowledges that the corresponding observation/decision was journaled.
extern "C" int mm_checkpoint(void* p,U generation) noexcept {
    if(!p)return -1;
    auto& c=*static_cast<Context*>(p);
    return static_cast<int>(c.runtime.checkpoint(c.event,generation,true));
}
extern "C" int mm_decide(void* ptr,const Input* in,Output* out) noexcept {
    if(!ptr || !in || !out)return -1;
    *out={}; auto& c=*static_cast<Context*>(ptr); const auto& x=*in;
    const auto fail=[&](Error e){out->error=static_cast<U>(e);return static_cast<int>(e);};
    if(x.now>=c.policy_until)return fail(Error::expired);
    if(c.catalog<2)return fail(Error::unauthorized);
    if(x.mark<100 || x.mark>0xffffff-100 || x.position< -8 || x.position>8)return fail(Error::invalid);
    if(x.slot<x.oracle_slot || x.slot-x.oracle_slot>100)return fail(Error::stale);
    if(x.observed>x.now)return fail(Error::future);
    if(x.now-x.observed>=10'000'000'000)return fail(Error::stale);
    if(x.market_sequence<c.last_market)return fail(Error::sequence);
    c.last_market=x.market_sequence;++c.feed;
    for(U field=0;field<2;++field){
        TypedFact f{};f.key={field?0:c.agent+1,103,1,1,field+1};
        f.unit=field?FactUnit::price:FactUnit::signed_quantity;f.asset=1;f.quote_asset=field?2:0;
        f.value=field?static_cast<I>(x.mark)*scale:x.position*scale;
        f.source=1;f.source_generation=1;f.sequence=c.feed;f.observed_ns=x.observed;
        f.valid_for_ns=10'000'000'000;f.status=FactStatus::coherent;
        FactDelta d{++c.event,c.event,FactOperation::snapshot,f};
        auto e=c.runtime.observe(d,x.now).error;if(e!=Error::okay)return fail(e);
    }
    auto e=c.runtime.poll(x.now,1);if(e!=Error::okay)return fail(e);
    RuntimeDecision d{};if(!c.runtime.pop(d))return fail(Error::missing);
    if(d.candidate.error!=Error::okay)return fail(d.candidate.error);
    if(d.candidate.action!=EconomicAction::hold && d.candidate.action!=EconomicAction::reduce)return fail(Error::unauthorized);
    out->reduce=d.candidate.action==EconomicAction::reduce;out->fingerprint=d.candidate.diagnostic_fingerprint;
    out->generation=d.candidate.state_generation;out->until=std::min(d.candidate.valid_until_ns,c.policy_until);
    out->authority=d.candidate.execution_authority;
    out->center=static_cast<I>(x.mark)+(c.agent?1:-1)-2*x.position;
    const I spread=c.catalog==2?4:12,clip=c.catalog==2?2:1;const U levels=c.catalog==2?2:1;
    I bid=std::min(out->center-spread,static_cast<I>(x.mark)-1);
    I ask=std::max(out->center+spread,static_cast<I>(x.mark)+1);
    if(x.best_ask)bid=std::min(bid,static_cast<I>(x.best_ask)-1);
    if(x.best_bid)ask=std::max(ask,static_cast<I>(x.best_bid)+1);
    I buy=out->reduce?(x.position<0?-x.position:0):8-x.position;
    I sell=out->reduce?(x.position>0?x.position:0):8+x.position;
    for(U level=0;level<levels;++level){
        const I b=std::min<I>(clip,buy),s=std::min<I>(clip,sell);buy-=b;sell-=s;
        if(b)out->quotes[out->count++]={level,static_cast<U>(bid-static_cast<I>(level)*2),static_cast<U>(b),out->reduce};
        if(s)out->quotes[out->count++]={8+level,static_cast<U>(ask+static_cast<I>(level)*2),static_cast<U>(s),out->reduce};
    }
    return 0;
}
