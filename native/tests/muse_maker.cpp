#include "machine/economics/muse_maker.hpp"
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <random>
#include <vector>

namespace mm=machine::economics::muse_maker;
using machine::economics::scale;
using machine::economics::Error;
using machine::economics::Wide;
std::uint64_t checks=0;
void check(bool result){++checks;if(!result){std::cerr<<"check failed "<<checks<<'\n';std::exit(1);}}
mm::Policy policy(){return {mm::Mode::quote,1000,5000,1000,1000,20*scale,2,60000};}
mm::Input input(){return {10000,9999,9999,9999,100*scale,100*scale+10000,10000,10000,10*scale,
                        60*scale,60*scale,0,0,0,800,100000,true,true,true};}
int main(){
    auto p=policy();auto s=input();auto plan=mm::decide(p,s);
    check(plan.error==Error::okay && plan.bid.quantity>0 && plan.ask.quantity>0);
    check(plan.bid.price<=s.bid && plan.ask.price>=s.ask);
    check(plan.opening_cap==1200*scale);
    s.reconciled=false;check(mm::decide(p,s).error==Error::unknown);
    s=input();s.exclusive_owner=false;check(mm::decide(p,s).error==Error::conflict);
    s=input();s.market_ms=1;check(mm::decide(p,s).error==Error::stale);
    s=input();s.account_ms=10001;check(mm::decide(p,s).error==Error::stale);
    s=input();s.policy_ms=10001;check(mm::decide(p,s).error==Error::expired);
    s=input();s.now_ms=s.policy_ms+p.ttl_ms;s.market_ms=s.account_ms=s.now_ms;
    check(mm::decide(p,s).error==Error::expired);
    --s.now_ms;s.market_ms=s.account_ms=s.now_ms;check(mm::decide(p,s).error==Error::okay);
    s=input();s.inventory=scale;plan=mm::decide(p,s);
    check(plan.ask.reduce_only && plan.ask.quantity<=s.inventory);
    s.pending_reducing=s.inventory;check(mm::decide(p,s).ask.quantity==0);
    s.pending_reducing+=s.lot;check(mm::decide(p,s).error==Error::conflict);
    s=input();s.pending_opening=1200*scale;plan=mm::decide(p,s);
    check(plan.bid.quantity==0 && plan.ask.quantity==0);
    p.mode=mm::Mode::reduce_only;s=input();check(mm::decide(p,s).bid.quantity==0);
    s.inventory=-scale;plan=mm::decide(p,s);check(plan.bid.reduce_only && plan.bid.quantity>0 && plan.ask.quantity==0);
    p.mode=mm::Mode::pause;plan=mm::decide(p,s);check(plan.bid.quantity==0 && plan.ask.quantity==0);
    p=policy();p.clip_usd_micros=INT64_MAX;check(mm::decide(p,s).error==Error::invalid);
    p=policy();s=input();s.inventory=INT64_MIN;check(mm::decide(p,s).error==Error::invalid);
    s=input();s.equity=30*scale;check(mm::decide(p,s).opening_cap==600*scale);
    s=input();s.equity=100*scale;check(mm::decide(p,s).opening_cap==1200*scale);
    s=input();s.equity=0;plan=mm::decide(p,s);check(plan.bid.quantity==0 && plan.ask.quantity==0);
    // Real failure: a $10 target rounds to < $10 on SOL's 0.01-lot grid.
    s=input();s.bid=10503*10000LL;s.ask=10504*10000LL;p=policy();p.clip_usd_micros=10*scale;
    plan=mm::decide(p,s);check(plan.bid.quantity==0&&plan.ask.quantity==0);
    p.clip_usd_micros=600*scale;plan=mm::decide(p,s);
    check(plan.bid.quantity>0&&plan.ask.quantity>0);
    check(mm::notional(plan.bid.price,plan.bid.quantity)>590*scale);
    check(mm::notional(plan.ask.price,plan.ask.quantity)>590*scale);
    // Once the bid ACK arrives, don't halve the remaining ask budget again.
    s.pending_opening=static_cast<std::int64_t>(mm::notional(plan.bid.price,plan.bid.quantity));s.bid_outstanding=true;
    auto second_side=mm::decide(p,s);check(second_side.bid.quantity==0&&second_side.ask.quantity>0);
    check(mm::notional(second_side.ask.price,second_side.ask.quantity)>590*scale);
    check(s.pending_opening+mm::notional(second_side.ask.price,second_side.ask.quantity)<=1200*scale);
    s.ask_outstanding=true;check(mm::decide(p,s).ask.quantity==0);
    s=input();s.bid=10503*10000LL;s.ask=10504*10000LL;s.ask_outstanding=true;s.pending_opening=599*scale;
    plan=mm::decide(p,s);check(plan.ask.quantity==0&&mm::notional(plan.bid.price,plan.bid.quantity)>590*scale);
    const auto minimum=mm::minimum_quantity(s.bid,s.lot,s.minimum_notional);
    check(mm::notional(s.bid,minimum)>=s.minimum_notional);
    check(mm::notional(s.bid,minimum-s.lot)<s.minimum_notional);
    check(mm::executable_size(10*scale,s.bid,s.lot,s.minimum_notional)==0);
    check(mm::executable_size(11*scale,s.bid,s.lot,s.minimum_notional)>0);
    s=input();s.free_margin=1;p.clip_usd_micros=600*scale;
    plan=mm::decide(p,s);check(plan.bid.quantity==0&&plan.ask.quantity==0);
    std::mt19937_64 rng(20261015);
    std::vector<std::int64_t> ns;ns.reserve(20000);std::int64_t checksum=0;
    for(int i=0;i<100000;++i){
        s=input();p=policy();s.bid=(5000+rng()%25000)*10000;s.ask=s.bid+(1+rng()%10)*10000;
        s.equity=(rng()%101)*scale;s.free_margin=(rng()%101)*scale;
        s.inventory=(static_cast<std::int64_t>(rng()%2001)-1000)*s.lot;
        s.pending_opening=(rng()%1201)*scale;s.signed_flow_millionths=static_cast<std::int64_t>(rng()%2000001)-scale;
        p.clip_usd_micros=(1+rng()%600)*scale;p.inventory_skew_millibps=rng()%50001;
        s.bid_outstanding=rng()%2;s.ask_outstanding=rng()%2;
        auto start=std::chrono::steady_clock::now();plan=mm::decide(p,s);auto end=std::chrono::steady_clock::now();
        if(i<20000)ns.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end-start).count());
        check(plan.error==Error::okay);check(plan.bid.price<=s.bid && plan.ask.price>=s.ask);
        check(plan.bid.price%s.tick==0 && plan.ask.price%s.tick==0);
        check(plan.bid.quantity%s.lot==0 && plan.ask.quantity%s.lot==0);
        check(!s.bid_outstanding||plan.bid.quantity==0);check(!s.ask_outstanding||plan.ask.quantity==0);
        Wide new_open=0;
        for(auto q : {plan.bid,plan.ask}){
            check(q.quantity==0 || mm::notional(q.price,q.quantity)>=s.minimum_notional);
            check(mm::notional(q.price,q.quantity)<=p.clip_usd_micros);
            if(q.reduce_only)check(q.quantity<=std::abs(s.inventory));
            else new_open+=mm::notional(q.price,q.quantity);
        }
        Wide used=mm::notional(s.ask,std::abs(s.inventory))+s.pending_opening;
        check(new_open<=std::max<Wide>(0,Wide(plan.opening_cap)-used));
        check(new_open<=Wide(s.free_margin)*20);
        checksum+=plan.bid.quantity+plan.ask.quantity;
    }
    std::sort(ns.begin(),ns.end());
    std::cout<<"{\"checks\":"<<checks<<",\"random_scenarios\":100000,\"synthetic_kernel_only\":true,"
             <<"\"p50_ns\":"<<ns[ns.size()/2]<<",\"p99_ns\":"<<ns[ns.size()*99/100]
             <<",\"checksum\":"<<checksum<<",\"orders_submitted\":0}\n";
}
