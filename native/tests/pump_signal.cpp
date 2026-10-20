#include "machine/economics/pump_signal.hpp"
#include <cassert>
#include <chrono>
#include <iostream>
#include <vector>
using namespace machine::economics::pump;
// Keep timing from specializing the pure kernel for the synthetic constants.
#if defined(__GNUC__) && !defined(__clang__)
#define KERNEL_BENCH_NOINLINE __attribute__((noinline,noipa))
#else
#define KERNEL_BENCH_NOINLINE __attribute__((noinline))
#endif
KERNEL_BENCH_NOINLINE uint64_t measured_ingest(Engine& e,const Event& v){return e.ingest(v);}
KERNEL_BENCH_NOINLINE Result measured_decide(Engine& e,uint64_t slot,uint64_t ms){return e.decide(slot,ms);}
Policy policy(){return {1,2000000,12,6,3,300000000,3500,6000,3500,50000000,500000000,2};}
Event event(uint64_t id,uint64_t slot,uint64_t wallet){
    return {id,slot,0,id,wallet,1,200000000,100000000,40000000000,
        800000000000000,10000000000,500000000000000,95,30,1,0,id};
}
void seed(Engine& engine) {
    for(uint64_t i=0;i<6;i++)assert(engine.ingest(event(i+1,100+i*2+1,i+1))==candidate);
}
int main(int argc,char**) {
    auto p=policy();Engine e(p,100);seed(e);
    auto r=e.decide(112,1000);assert(r.reason==candidate && r.events==6 && r.buyers==6 && r.positive_buckets==3);
    assert(!r.execution_authority && !r.history_complete && r.quote_tokens>0 && r.quote_exit_lamports<50000000);
    assert(e.decide(111,1000).reason==observing);assert(e.decide(112,2000000).reason==policy_expired);
    assert(e.ingest(event(1,101,1))==candidate);assert(e.decide(112,1000).events==6);
    assert(e.ingest(event(9,112,9))==observing);assert(e.decide(112,1000).events==6);
    auto bad=event(1,101,1);bad.sol++;assert(e.ingest(bad)==bad_input);assert(e.decide(112,1000).reason==bad_input);
    Engine fund(p,100);seed(fund);for(int i=1;i<=6;i++)assert(fund.funding(i,7,99));
    assert(fund.decide(112,1000).reason==funding_concentration);assert(!fund.funding(1,8,100));
    Engine reorg(p,100);seed(reorg);reorg.invalidate();assert(reorg.decide(112,1000).reason==invalidated);
    Engine atomic(p,100);seed(atomic);auto sell=event(8,111,6);sell.buy=0;sell.sol=1;sell.tx_id=6;
    atomic.ingest(sell);assert(atomic.decide(112,1000).reason==roundtrip);
    Engine dev(p,100);seed(dev);sell.tx_id=8;sell.developer=1;dev.ingest(sell);assert(dev.decide(112,1000).reason==developer_selling);
    Engine cap(p,100);for(uint64_t i=1;i<=256;i++)assert(cap.ingest(event(i,101,i))==candidate);
    assert(cap.ingest(event(257,101,257))==capacity);
    Engine evcap(p,100);for(uint64_t i=1;i<=1024;i++)assert(evcap.ingest(event(i,101,1))==candidate);
    assert(evcap.ingest(event(1025,101,1))==capacity);
    Engine burst(p,100);for(uint64_t i=1;i<=6;i++)burst.ingest(event(i,101,i));assert(burst.decide(112,1000).reason==persistence);
    Engine stale(p,100);seed(stale);assert(stale.decide(115,1000).reason==quote_stale);
    Engine unsupported_engine(p,100);bad=event(1,101,1);bad.supported=0;assert(unsupported_engine.ingest(bad)==unsupported);
    Engine overflow(p,100);bad=event(1,101,1);bad.sol=UINT64_MAX;assert(overflow.ingest(bad)==bad_input);
    uint64_t tokens=0,proceeds=0;auto q=event(1,101,1);assert(quotes(q,50000000,tokens,proceeds));
    q.real_sol=1;assert(!quotes(q,50000000,tokens,proceeds));q.real_sol=10000000000;q.real_token=1;assert(!quotes(q,50000000,tokens,proceeds));
    std::cout<<"native conformance passed; state_bytes="<<sizeof(Engine)<<"\n";
    if(argc>1) {
        using Clock=std::chrono::steady_clock;
        std::vector<uint64_t> ingest_ns,decide_ns;ingest_ns.reserve(100000);decide_ns.reserve(100000);
        uint64_t sink=0;
        for(int n=0;n<100000;n++) {
            Engine sample(p,100);seed(sample);auto x=event(7,111,7);
            auto t=Clock::now();sink+=measured_ingest(sample,x);auto u=Clock::now();
            auto out=measured_decide(sample,112,1000);auto v=Clock::now();sink+=out.quote_tokens+out.events;
            ingest_ns.push_back((u-t).count());decide_ns.push_back((v-u).count());
        }
        std::sort(ingest_ns.begin(),ingest_ns.end());std::sort(decide_ns.begin(),decide_ns.end());
        std::cout<<"{\"samples\":100000,\"fixture\":\"7_wallet_synthetic_candidate\",\"state_bytes\":"<<sizeof(Engine)
            <<",\"ingest_ns\":{\"p50\":"<<ingest_ns[50000]<<",\"p99\":"<<ingest_ns[99000]
            <<"},\"decision_ns\":{\"p50\":"<<decide_ns[50000]<<",\"p99\":"<<decide_ns[99000]<<"},\"sink\":"<<sink<<"}\n";
        Engine full(p,100);
        for(uint64_t i=1;i<=1024;i++) {
            auto x=event(i,100+(i%12),1+(i%256));full.ingest(x);
        }
        for(uint64_t i=1;i<=256;i++)assert(full.funding(i,1+(i%8),99));
        decide_ns.clear();
        for(int i=0;i<100000;i++) {
            auto t=Clock::now();auto out=measured_decide(full,112,1000);auto u=Clock::now();
            assert(out.reason==candidate && out.buyers==256 && out.linked_buyers==256);
            sink+=out.quote_tokens;decide_ns.push_back((u-t).count());
        }
        std::sort(decide_ns.begin(),decide_ns.end());
        std::cout<<"{\"samples\":100000,\"fixture\":\"256_wallets_1024_events_8_funding_groups\",\"decision_ns\":{\"p50\":"<<decide_ns[50000]<<",\"p99\":"<<decide_ns[99000]<<"},\"sink\":"<<sink<<"}\n";
    }
}
