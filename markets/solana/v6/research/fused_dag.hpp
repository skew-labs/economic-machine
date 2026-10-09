#pragma once
// Four bounded integer outputs fit losslessly in one upstream SCORE channel.
// max = ((48*33+32)*3+2)*3+2 = 14552; fixed-point resolution is far below 0.5.
// This changes scheduling/encoding, not the Economic Machine interpreter.
struct FusedContext {machine::economics::EconomicRuntime<64,1,4> runtime;U event{},feed{},expires;};
static_assert(machine::economics::scale>2*14552);
inline void* fused_create(const Node*nodes,U count,const U*outputs,U now,U expires){
    // Reuse the original admission validator off the hot path, then install
    // exactly one fused program. Old libevolution.so is never replaced.
    void*validation=ev_create(nodes,count,outputs,now,expires);if(!validation)return nullptr;ev_destroy(validation);
    auto*ctx=new(std::nothrow) FusedContext{};if(!ctx)return nullptr;ctx->expires=expires;
    using namespace machine::economics;
    const RegisterType number{FactUnit::rate,0,0},flag{FactUnit::boolean,0,0};RegisterType types[64]{};
    ProgramRegistration r{};auto&q=r.program;q.id=q.version=q.policy_version=1;q.ttl_ns=3'000'000'000;q.dependency_count=6;
    for(U n=0;n<6;++n){q.dependency_types[n]=number;r.dependencies[n]={{1,103,1,1,n+1},FactUnit::rate,0,0};}
    auto add=[&](StrategyOpcode op,U dst,U a,U b,U c,I value,RegisterType type){q.instructions[q.count++]={op,(std::uint32_t)dst,(std::uint32_t)a,(std::uint32_t)b,(std::uint32_t)c,value,type,0,0};types[dst]=type;};
    for(U n=0;n<count;++n){auto x=nodes[n];StrategyOpcode op=StrategyOpcode::constant;I value=0;RegisterType t=number;
        switch(x.op){
        case 1:op=StrategyOpcode::observe;value=x.value;break;
        case 2:op=StrategyOpcode::constant;value=x.value*scale;break;
        case 3:op=StrategyOpcode::add;break;case 4:op=StrategyOpcode::subtract;break;case 5:op=StrategyOpcode::multiply;break;
        case 6:op=StrategyOpcode::minimum;break;case 7:op=StrategyOpcode::maximum;break;
        case 8:op=StrategyOpcode::absolute;break;case 9:op=StrategyOpcode::negate;break;
        case 10:op=StrategyOpcode::compare;value=(I)Comparator::greater;t=flag;break;
        case 11:op=StrategyOpcode::compare;value=(I)Comparator::less;t=flag;break;
        case 12:op=StrategyOpcode::logical_and;t=flag;break;case 13:op=StrategyOpcode::logical_or;t=flag;break;
        case 14:op=StrategyOpcode::logical_not;t=flag;break;case 15:op=StrategyOpcode::select;t=types[x.b];break;
        default:delete ctx;return nullptr;
        }
        add(op,n,x.a,x.b,x.c,value,t);
    }
    auto constant=[&](U dst,I value){add(StrategyOpcode::constant,dst,0,0,0,value*scale,number);};
    constant(40,-24);constant(41,24);add(StrategyOpcode::clamp,42,outputs[0],40,41,0,number);add(StrategyOpcode::subtract,43,42,40,0,0,number);
    constant(44,0);constant(45,32);add(StrategyOpcode::clamp,46,outputs[1],44,45,0,number);
    constant(47,2);add(StrategyOpcode::clamp,48,outputs[2],44,47,0,number);add(StrategyOpcode::clamp,49,outputs[3],44,47,0,number);
    constant(50,33);constant(51,3);
    add(StrategyOpcode::multiply,52,43,50,0,0,number);add(StrategyOpcode::add,53,52,46,0,0,number);
    add(StrategyOpcode::multiply,54,53,51,0,0,number);add(StrategyOpcode::add,55,54,48,0,0,number);
    add(StrategyOpcode::multiply,56,55,51,0,0,number);add(StrategyOpcode::add,57,56,49,0,0,number);
    constant(58,14552);add(StrategyOpcode::divide,59,57,58,0,0,number);add(StrategyOpcode::score,60,59,0,0,0,number);add(StrategyOpcode::hold,61,0,0,0,0,number);
    r.mandate={1,103,1,1,expires,0,0,true};if(ctx->runtime.install(r,now)!=Error::okay){delete ctx;return nullptr;}return ctx;
}
inline int fused_step(void*ptr,const I*values,U now,U observed,Output*out){
    using namespace machine::economics;if(!ptr||!values||!out)return -1;*out={};auto&c=*static_cast<FusedContext*>(ptr);
    auto fail=[&](Error e){out->error=(U)e;return static_cast<int>(e);};
    if(now>=c.expires)return fail(Error::expired);
    const I lower[6]={-8,-100,-100,0,0,0},upper[6]={8,100,100,100,4096,100};
    for(U n=0;n<6;++n)if(values[n]<lower[n]||values[n]>upper[n])return fail(Error::invalid);
    if(now<observed||now-observed>=3'000'000'000)return fail(Error::stale);++c.feed;
    for(U n=0;n<6;++n){TypedFact f{};f.key={1,103,1,1,n+1};f.unit=FactUnit::rate;f.value=values[n]*scale;f.source=1;f.source_generation=1;f.sequence=c.feed;f.observed_ns=observed;f.valid_for_ns=3'000'000'000;f.status=FactStatus::coherent;
        FactDelta delta{++c.event,c.event,FactOperation::snapshot,f};auto e=c.runtime.observe(delta,now).error;if(e!=Error::okay)return fail(e);}
    auto code=c.runtime.poll(now,1);if(code!=Error::okay)return fail(code);RuntimeDecision d{};
    if(!c.runtime.pop(d))return fail(Error::missing);
    if(d.candidate.error!=Error::okay)return fail(d.candidate.error);
    if(d.candidate.program!=1||d.candidate.execution_authority||d.candidate.action!=EconomicAction::hold)return fail(Error::unauthorized);
    I packed=(d.candidate.score*14552+scale/2)/scale;if(packed<0||packed>14552)return fail(Error::invalid);
    out->values[3]=packed%3;packed/=3;out->values[2]=packed%3;packed/=3;out->values[1]=packed%33;packed/=33;out->values[0]=packed-24;
    out->fingerprint=d.candidate.diagnostic_fingerprint;out->generation=d.candidate.state_generation;
    if(c.runtime.pop(d))return fail(Error::conflict);return static_cast<int>(c.runtime.checkpoint(c.event,out->generation,true));
}
inline void fused_destroy(void*p){delete static_cast<FusedContext*>(p);}
