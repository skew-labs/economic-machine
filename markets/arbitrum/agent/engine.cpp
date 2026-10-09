#include "engine.hpp"
#include "machine/economics/runtime.hpp"
#include <algorithm>
#include <limits>
#include <new>
using namespace machine::economics;
using U=std::uint64_t;using I=std::int64_t;
struct Quote { U bid{},ask{},bq{},aq{},expiry{}; };
struct Context { U index{},best_bid{},best_ask{};I inventory{},momentum{}; };
struct Engine { EconomicRuntime<16,1,4> runtime;EmProfile p;U event{},last_sequence{},nonce{};Quote confirmed{},proposal{};Context cache{},proposal_context{};bool pending{}; };
extern "C" std::uint32_t em_abi_version(){return 2;}
extern "C" void* em_create(const EmProfile* p,U now){
    if(!p || (p->chain!=42161 && p->chain!=421614) || !p->account || p->account>0x7ffffffe || !p->version ||
       p->expires_ns<=now || !now || !p->max_position || p->max_position>1000000000 || !p->clip || p->clip>p->max_position ||
       !p->spread || p->spread>64 || p->inventory_weight>16 || !p->ttl_seconds || p->ttl_seconds>60 ||
       p->session_epoch>0xffffffff || p->initial_nonce==std::numeric_limits<U>::max())return nullptr;
    auto* e=new(std::nothrow) Engine{};if(!e)return nullptr;e->p=*p;e->nonce=p->initial_nonce;
    ProgramRegistration r{};auto& q=r.program;q.id=1;q.version=q.policy_version=p->version;q.ttl_ns=1'000'000'000;q.dependency_count=2;
    RegisterType number{FactUnit::rate,0,0};
    for(U i=0;i<2;++i){q.dependency_types[i]=number;r.dependencies[i]={{p->account,p->chain,1,1,i+1},FactUnit::rate,0,0};}
    auto add=[&](StrategyOpcode op,unsigned dst,unsigned a,unsigned b,unsigned c,I value){q.instructions[q.count++]={op,dst,a,b,c,value,number,0,0};};
    add(StrategyOpcode::observe,0,0,0,0,0);add(StrategyOpcode::observe,1,0,0,0,1);
    add(StrategyOpcode::constant,2,0,0,0,I(p->inventory_weight)*scale);
    add(StrategyOpcode::multiply,3,0,2,0,0);add(StrategyOpcode::subtract,4,1,3,0,0);
    add(StrategyOpcode::constant,5,0,0,0,-16*scale);add(StrategyOpcode::constant,6,0,0,0,16*scale);
    add(StrategyOpcode::clamp,7,4,5,6,0);add(StrategyOpcode::subtract,8,7,5,0,0);
    add(StrategyOpcode::constant,9,0,0,0,32*scale);add(StrategyOpcode::divide,10,8,9,0,0);
    add(StrategyOpcode::score,11,10,0,0,0);add(StrategyOpcode::hold,12,0,0,0,0);
    r.mandate={p->account,p->chain,1,p->version,p->expires_ns,0,0,true};
    if(e->runtime.install(r,now)!=Error::okay){delete e;return nullptr;}return e;
}
static void put(std::uint8_t* bytes,U value,unsigned start,unsigned width){
    // start measured from least-significant bit, ABI big-endian 32-byte word.
    for(unsigned i=0;i<width;++i)if(value&(U{1}<<i))bytes[31-(start+i)/8]|=std::uint8_t(1U<<((start+i)%8));
}
extern "C" int em_step(void* ptr,const EmFrame* f,EmCommand* out){
    if(!ptr||!f||!out)return -1;*out={};auto& e=*static_cast<Engine*>(ptr);const auto& p=e.p;
    if(e.pending)return 1;
    if(f->chain!=p.chain || f->account_nonce!=e.nonce || f->sequence<=e.last_sequence ||
       f->session_epoch!=p.session_epoch || e.nonce==std::numeric_limits<U>::max())return 2;
    if(f->now_ns>=p.expires_ns || f->now_ns<f->observed_ns || f->now_ns-f->observed_ns>=1'000'000'000 ||
       f->unix_seconds>=(U{1}<<40)-p.ttl_seconds)return 3;
    if(!f->index || f->index>65535 || f->best_bid>65535 || f->best_ask>65535 ||
       (f->best_bid && f->best_ask && f->best_bid>=f->best_ask) ||
       f->inventory < -I(p.max_position) || f->inventory>I(p.max_position) || f->momentum < -100 || f->momentum>100)return 4;
    U ttl=std::min(p.ttl_seconds,(p.expires_ns-f->now_ns)/1'000'000'000);if(!ttl)return 3;
    const auto& cached=e.confirmed;
    const bool resting=cached.expiry>f->unix_seconds && cached.expiry-f->unix_seconds>std::max(U{1},ttl/2) &&
        f->resting_bid==cached.bid && f->resting_ask==cached.ask && f->resting_bid_lots==cached.bq &&
        f->resting_ask_lots==cached.aq && f->resting_expiry==cached.expiry;
    if(resting && e.cache.index==f->index && e.cache.best_bid==f->best_bid && e.cache.best_ask==f->best_ask &&
       e.cache.inventory==f->inventory && e.cache.momentum==f->momentum){e.last_sequence=f->sequence;return 7;}
    const I values[2]={f->inventory,f->momentum};
    for(U i=0;i<2;++i){
        TypedFact fact{};fact.key={p.account,p.chain,1,1,i+1};fact.unit=FactUnit::rate;fact.value=values[i]*scale;
        fact.source=1;fact.source_generation=1;fact.sequence=f->sequence;fact.observed_ns=f->observed_ns;fact.valid_for_ns=1'000'000'000;fact.status=FactStatus::coherent;
        FactDelta delta{++e.event,e.event,FactOperation::snapshot,fact};if(e.runtime.observe(delta,f->now_ns).error!=Error::okay)return 5;
    }
    e.last_sequence=f->sequence;
    if(e.runtime.poll(f->now_ns,1)!=Error::okay)return 5;RuntimeDecision d{};
    if(!e.runtime.pop(d)||d.candidate.error!=Error::okay||d.candidate.execution_authority||d.candidate.action!=EconomicAction::hold)return 6;
    if(e.runtime.checkpoint(e.event,d.candidate.state_generation,true)!=Error::okay)return 5;
    I skew=(d.candidate.score*32+scale/2)/scale-16;
    I bid=I(f->index)+skew-I(p.spread),ask=I(f->index)+skew+I(p.spread);
    if(f->best_ask)bid=std::min(bid,I(f->best_ask)-1);
    if(f->best_bid)ask=std::max(ask,I(f->best_bid)+1);
    if(bid<1||ask>65535||bid>=ask)return 4;
    U bq=std::min(p.clip,U(I(p.max_position)-f->inventory));
    U aq=std::min(p.clip,U(I(p.max_position)+f->inventory));
    if(!bq)bid=0;if(!aq)ask=0;
    const auto& last=e.confirmed;
    if(last.bid==U(bid) && last.ask==U(ask) && last.bq==bq && last.aq==aq &&
       f->resting_bid==U(bid) && f->resting_ask==U(ask) &&
       f->resting_bid_lots==bq && f->resting_ask_lots==aq && f->resting_expiry==last.expiry &&
       last.expiry>f->unix_seconds && last.expiry-f->unix_seconds>std::max(U{1},ttl/2)){
        e.cache={f->index,f->best_bid,f->best_ask,f->inventory,f->momentum};return 7;
    }
    out->calldata[0]=0xac;out->calldata[1]=0x8e;out->calldata[2]=0x6b;out->calldata[3]=0x9e;
    put(out->calldata+4,p.account,0,32);auto* word=out->calldata+36;
    put(word,e.nonce+1,0,64);put(word,f->unix_seconds+ttl,64,40);
    put(word,U(bid),104,16);put(word,U(ask),120,16);put(word,bq,136,32);put(word,aq,168,32);
    put(word,p.session_epoch,200,32);
    out->size=68;out->nonce=e.nonce+1;out->generation=d.candidate.state_generation;out->policy_version=p.version;
    e.proposal={U(bid),U(ask),bq,aq,f->unix_seconds+ttl};
    e.proposal_context={f->index,f->best_bid,f->best_ask,f->inventory,f->momentum};e.pending=true;return 0;
}
extern "C" int em_ack(void* ptr,U nonce,int status){
    if(!ptr)return -1;auto& e=*static_cast<Engine*>(ptr);if(!e.pending)return 1;
    if(status==1 && nonce==e.nonce+1){e.nonce=nonce;e.confirmed=e.proposal;e.cache=e.proposal_context;e.pending=false;return 0;}
    if(status==0 && nonce==e.nonce){e.pending=false;return 0;}return 2;
}
extern "C" void em_destroy(void* p){delete static_cast<Engine*>(p);}
static U read(const std::uint8_t* bytes,unsigned begin,unsigned count){
    U n{};for(unsigned i=0;i<count;++i)n=(n<<8)|bytes[begin+i];return n;
}
extern "C" int em_bound_quote(void* ptr,U bid_ceiling,U ask_floor,EmCommand* command){
    if(!ptr || !command || !bid_ceiling || ask_floor>65535 || bid_ceiling>=ask_floor)return -1;
    auto& e=*static_cast<Engine*>(ptr);
    if(!e.pending || command->size!=68 || command->execution_authority || command->policy_version!=e.p.version ||
       command->nonce!=e.nonce+1 || read(command->calldata,32,4)!=e.p.account)return -2;
    if(e.proposal.bq)e.proposal.bid=std::min(e.proposal.bid,bid_ceiling);
    if(e.proposal.aq)e.proposal.ask=std::max(e.proposal.ask,ask_floor);
    auto* word=command->calldata+36;word[15]=word[16]=word[17]=word[18]=0;
    put(word,e.proposal.bid,104,16);put(word,e.proposal.ask,120,16);return 0;
}
extern "C" int em_pack_batch(const EmCommand* commands,std::uint32_t count,std::uint8_t* out,std::uint32_t capacity){
    if(!commands || !out || !count || count>32)return -1;
    const unsigned length=count*36,size=68+((length+31)&~31U);if(capacity<size)return -2;
    U accounts[32]{};
    for(unsigned i=0;i<count;++i){
        const auto& c=commands[i];const auto* bytes=c.calldata;
        if(c.size!=68 || c.execution_authority || !c.policy_version || !c.nonce ||
           bytes[0]!=0xac || bytes[1]!=0x8e || bytes[2]!=0x6b || bytes[3]!=0x9e ||
           read(bytes,60,8)!=c.nonce || bytes[36] || bytes[37] || bytes[38])return -3;
        for(unsigned j=4;j<32;++j)if(bytes[j])return -3;
        accounts[i]=read(bytes,32,4);if(!accounts[i] || accounts[i]>0x7ffffffe)return -3;
        for(unsigned j=0;j<i;++j)if(accounts[j]==accounts[i])return -4;
    }
    std::fill(out,out+size,std::uint8_t{});out[0]=0x30;out[1]=0x55;out[2]=0x4c;out[3]=0x7e;
    out[35]=32;out[66]=std::uint8_t(length>>8);out[67]=std::uint8_t(length);
    for(unsigned i=0;i<count;++i)std::copy(commands[i].calldata+32,commands[i].calldata+68,out+68+i*36);
    return int(size);
}
