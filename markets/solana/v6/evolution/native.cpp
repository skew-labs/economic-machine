#include "machine/economics/runtime.hpp"
#include <new>
using namespace machine::economics;
using U=std::uint64_t;using I=std::int64_t;
struct Node{I op,a,b,c,value;};
struct Output{U error;I values[4];U fingerprint,generation,authority;};
struct Context{EconomicRuntime<64,4,8> runtime;U event{},feed{},expires;};
static_assert(sizeof(Node)==40 && sizeof(Output)==64);
extern "C" void* ev_create(const Node* nodes,U count,const U* outputs,U now,U expires){
    if(!nodes||!outputs||!count||count>40||!now||expires<=now)return nullptr;
    auto* ctx=new(std::nothrow) Context{};if(!ctx)return nullptr;ctx->expires=expires;
    const RegisterType number{FactUnit::rate,0,0},flag{FactUnit::boolean,0,0};
    for(U output=0;output<4;++output){
        ProgramRegistration r{};auto& q=r.program;q.id=output+1;q.version=1;q.policy_version=1;q.ttl_ns=3'000'000'000;q.dependency_count=6;
        for(U n=0;n<6;++n){q.dependency_types[n]=number;r.dependencies[n]={{1,103,1,1,n+1},FactUnit::rate,0,0};}
        RegisterType types[64]{};
        auto add=[&](StrategyOpcode op,U dst,U a,U b,U c,I value,RegisterType type){q.instructions[q.count++]={op,(std::uint32_t)dst,(std::uint32_t)a,(std::uint32_t)b,(std::uint32_t)c,value,type,0,0};types[dst]=type;};
        bool valid=true;
        for(U n=0;n<count;++n){auto x=nodes[n];
            if(x.op<1||x.op>15||x.a<0||x.b<0||x.c<0||x.a>63||x.b>63||x.c>63||x.value< -10000||x.value>10000){valid=false;break;}
            StrategyOpcode op=StrategyOpcode::constant;I value=0;RegisterType t=number;
            switch(x.op){
            case 1:op=StrategyOpcode::observe;value=x.value;break;
            case 2:op=StrategyOpcode::constant;value=x.value*scale;break;
            case 3:op=StrategyOpcode::add;break;case 4:op=StrategyOpcode::subtract;break;case 5:op=StrategyOpcode::multiply;break;
            case 6:op=StrategyOpcode::minimum;break;case 7:op=StrategyOpcode::maximum;break;
            case 8:op=StrategyOpcode::absolute;break;case 9:op=StrategyOpcode::negate;break;
            case 10:op=StrategyOpcode::compare;value=(I)Comparator::greater;t=flag;break;
            case 11:op=StrategyOpcode::compare;value=(I)Comparator::less;t=flag;break;
            case 12:op=StrategyOpcode::logical_and;t=flag;break;case 13:op=StrategyOpcode::logical_or;t=flag;break;
            case 14:op=StrategyOpcode::logical_not;t=flag;break;
            case 15:op=StrategyOpcode::select;t=types[x.b];break;
            }
            add(op,n,x.a,x.b,x.c,value,t);
        }
        if(!valid||outputs[output]>=count||types[outputs[output]].unit!=FactUnit::rate){delete ctx;return nullptr;}
        // Encode bounded numeric outputs as upstream SCORE in [0,1]. The host
        // still enforces the narrower immutable user limits on decoded quotes.
        const I lo=output==0?-24:0,hi=output==0?24:output==1?32:2;
        add(StrategyOpcode::constant,40,0,0,0,lo*scale,number);
        add(StrategyOpcode::constant,41,0,0,0,hi*scale,number);
        add(StrategyOpcode::clamp,42,outputs[output],40,41,0,number);
        add(StrategyOpcode::subtract,43,42,40,0,0,number);
        add(StrategyOpcode::constant,44,0,0,0,(hi-lo)*scale,number);
        add(StrategyOpcode::divide,45,43,44,0,0,number);
        add(StrategyOpcode::score,46,45,0,0,0,number);
        add(StrategyOpcode::hold,47,0,0,0,0,number);
        r.mandate={1,103,1,1,expires,0,0,true};
        if(ctx->runtime.install(r,now)!=Error::okay){delete ctx;return nullptr;}
    }return ctx;
}
extern "C" int ev_step(void* ptr,const I* values,U now,U observed,Output* out){
    if(!ptr||!values||!out)return -1;
    *out={};auto& c=*static_cast<Context*>(ptr);
    auto fail=[&](Error e){out->error=(U)e;return (int)e;};
    if(now>=c.expires)return fail(Error::expired);
    const I lower[6]={-8,-100,-100,0,0,0},upper[6]={8,100,100,100,4096,100};
    for(U n=0;n<6;++n)if(values[n]<lower[n]||values[n]>upper[n])return fail(Error::invalid);
    if(now<observed||now-observed>=3'000'000'000)return fail(Error::stale);
    ++c.feed;
    for(U n=0;n<6;++n){TypedFact f{};f.key={1,103,1,1,n+1};f.unit=FactUnit::rate;f.value=values[n]*scale;f.source=1;f.source_generation=1;f.sequence=c.feed;f.observed_ns=observed;f.valid_for_ns=3'000'000'000;f.status=FactStatus::coherent;
        FactDelta d{++c.event,c.event,FactOperation::snapshot,f};auto e=c.runtime.observe(d,now).error;if(e!=Error::okay)return fail(e);}
    auto e=c.runtime.poll(now,4);if(e!=Error::okay)return fail(e);U mask=0;
    RuntimeDecision d{};while(c.runtime.pop(d)){
        if(d.candidate.error!=Error::okay)return fail(d.candidate.error);
        U i=d.candidate.program-1;if(i>=4||mask&(U{1}<<i)||d.candidate.execution_authority||d.candidate.action!=EconomicAction::hold)return fail(Error::unauthorized);
        mask|=U{1}<<i;I span=i==0?48:i==1?32:2;
        out->values[i]=(d.candidate.score*span+scale/2)/scale-(i==0?24:0);
        out->fingerprint^=d.candidate.diagnostic_fingerprint;out->generation=d.candidate.state_generation;
    }
    if(mask!=15)return fail(Error::missing);
    return (int)c.runtime.checkpoint(c.event,out->generation,true);
}
extern "C" void ev_destroy(void* p){delete static_cast<Context*>(p);}
