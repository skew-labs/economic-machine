#pragma once
#include "value.hpp"
#include <cstring>

namespace machine::economics::pump {
// ABI v1. Integer identities are interned bijectively by the bounded adapter;
// no truncated wallet hashes, heap allocations, I/O or model calls per event.
struct Policy {
    uint64_t version, expires_ms, window_slots, buyers, positive_buckets;
    uint64_t net_lamports, buyer_bps, funder_bps, roundtrip_bps;
    uint64_t notional, minimum_real_sol, maximum_quote_age_slots;
};
struct Event {
    uint64_t id, slot, tx_order, ix, wallet, buy, sol, tokens;
    uint64_t virtual_sol, virtual_token, real_sol, real_token;
    uint64_t protocol_bps, creator_bps, supported, developer, tx_id;
};
struct Result {
    uint64_t reason, events, buyers, positive_buckets, buy_lamports;
    int64_t net_lamports;
    uint64_t buyer_bps, funder_bps, roundtrip_bps, linked_buyers;
    uint64_t developer_sell, quote_tokens, quote_exit_lamports;
    uint64_t policy_version, cutoff_slot, execution_authority, history_complete;
};
enum Reason : uint64_t {
    candidate, observing, policy_expired, bad_input, capacity, unsupported,
    breadth, persistence, demand, concentration, funding_concentration,
    roundtrip, developer_selling, quote_stale, liquidity, invalidated
};
inline bool valid(const Policy& p) noexcept {
    return p.version && p.expires_ms && (p.window_slots==12 || p.window_slots==24)
        && p.buyers>=6 && p.buyers<=32 && p.positive_buckets>=3 && p.positive_buckets<=6
        && p.net_lamports>=300000000 && p.net_lamports<=2000000000
        && p.buyer_bps>=1000 && p.buyer_bps<=3500 && p.funder_bps>=3000 && p.funder_bps<=6000
        && p.roundtrip_bps<=3500 && p.notional==50000000
        && p.minimum_real_sol>=500000000 && p.minimum_real_sol<=5000000000
        && p.maximum_quote_age_slots<=2;
}
inline uint64_t fee(uint64_t amount, uint64_t bps) noexcept {
    return static_cast<uint64_t>((Wide(amount)*bps+9999)/10000);
}
inline bool quotes(const Event& e, uint64_t budget, uint64_t& tokens, uint64_t& proceeds) noexcept {
    if (!e.supported || !e.virtual_sol || !e.virtual_token || !e.real_sol
        || !e.real_token || e.real_token>=e.virtual_token
        || e.virtual_sol>INT64_MAX || e.virtual_token>INT64_MAX
        || e.real_sol>INT64_MAX || e.protocol_bps>1000 || e.creator_bps>1000
        || e.protocol_bps+e.creator_bps>1000 || budget>INT64_MAX) return false;
    uint64_t lo=0, hi=budget;
    while(lo<hi) {
        const auto mid=lo+(hi-lo+1)/2;
        if(Wide(mid)+fee(mid,e.protocol_bps)+fee(mid,e.creator_bps)<=budget) lo=mid;
        else hi=mid-1;
    }
    auto amount=static_cast<uint64_t>(Wide(e.virtual_token)*lo/(Wide(e.virtual_sol)+lo));
    if(amount<=1 || amount-1>=e.real_token) return false;
    tokens=amount-1;
    const auto gross=static_cast<uint64_t>(Wide(tokens)*e.virtual_sol/(Wide(e.virtual_token)+tokens));
    if(gross>e.real_sol) return false;
    const auto costs=fee(gross,e.protocol_bps)+fee(gross,e.creator_bps);
    proceeds=gross>costs ? gross-costs : 0;
    return proceeds>0;
}
class Engine {
    struct Wallet {uint64_t id=0, buy=0, sell=0, funder=0;};
    struct Seen {uint64_t id=0; Event value{};};
    struct Atomic {uint64_t tx=0,wallet=0;bool buy=false,sell=false;};
    std::array<Wallet,256> wallets{};
    std::array<Seen,2048> seen{};
    std::array<Atomic,2048> atomic{};
    std::array<int64_t,6> buckets{};
    Policy policy{};
    uint64_t launch=0, count=0, buys=0, sells=0, dev_sells=0;
    Reason poisoned=candidate;
    bool atomic_roundtrip=false;
    Event quote{};
    Wallet* wallet(uint64_t id, bool insert) noexcept {
        for(size_t i=0;i<wallets.size();++i) {
            auto& w=wallets[(id+i)&255];
            if(w.id==id) return &w;
            if(!w.id) {if(insert) w.id=id; return insert?&w:nullptr;}
        }
        return nullptr;
    }
public:
    Engine(const Policy& p,uint64_t slot):policy(p),launch(slot) {
        if(!valid(p) || slot>UINT64_MAX-24) poisoned=bad_input;
    }
    uint64_t ingest(const Event& e) noexcept {
        if(poisoned!=candidate) return poisoned;
        if(!e.id || !e.tx_id || !e.wallet || e.buy>1 || e.developer>1 || e.supported>1
            || e.sol>INT64_MAX/2048 || e.tokens>INT64_MAX
            || e.slot<launch) return poisoned=bad_input;
        if(e.slot>=launch+policy.window_slots) return observing; // no future leakage
        Seen* entry=nullptr;
        for(size_t i=0;i<seen.size();++i) {
            auto& x=seen[(e.id+i)&2047];
            if(x.id==e.id) {
                if(std::memcmp(&x.value,&e,sizeof(e))) return poisoned=bad_input;
                return candidate; // exact idempotent duplicate
            }
            if(!x.id) {entry=&x;break;}
        }
        if(count>=1024 || !entry) return poisoned=capacity;
        auto* w=wallet(e.wallet,true);
        if(!w) return poisoned=capacity;
        if(!e.supported) return poisoned=unsupported;
        for(size_t i=0;i<atomic.size();++i) {
            auto& x=atomic[(((e.tx_id*2654435761ULL)^e.wallet)+i) & 2047];
            if(!x.tx || (x.tx==e.tx_id && x.wallet==e.wallet)) {
                x.tx=e.tx_id;x.wallet=e.wallet;
                if(e.buy)x.buy=true;else x.sell=true;
                atomic_roundtrip |= x.buy && x.sell;break;
            }
        }
        entry->id=e.id;entry->value=e;++count;
        if(e.buy) {w->buy+=e.sol;buys+=e.sol;} else {w->sell+=e.sol;sells+=e.sol;if(e.developer)dev_sells+=e.sol;}
        buckets[(e.slot-launch)/4] += e.buy ? int64_t(e.sol) : -int64_t(e.sol);
        if(!quote.id || e.slot>quote.slot || (e.slot==quote.slot && (e.tx_order>quote.tx_order
            || (e.tx_order==quote.tx_order && e.ix>quote.ix)))) quote=e;
        return candidate;
    }
    bool funding(uint64_t id, uint64_t funder, uint64_t edge_slot) noexcept {
        if(!id || !funder || edge_slot>=launch) return false;
        auto* w=wallet(id,false);if(!w)return false;
        if(w->funder && w->funder!=funder) {poisoned=invalidated;return false;}
        w->funder=funder;return true;
    }
    void invalidate() noexcept {poisoned=invalidated;}
    Result decide(uint64_t current_slot,uint64_t now_ms) const noexcept {
        Result r{};r.reason=observing;r.events=count;r.buy_lamports=buys;
        r.net_lamports=int64_t(buys)-int64_t(sells);r.developer_sell=dev_sells;
        r.policy_version=policy.version;r.cutoff_slot=launch+policy.window_slots;
        // No decision in this module conveys transaction authority or complete history.
        if(poisoned!=candidate) {r.reason=poisoned;return r;}
        if(now_ms>=policy.expires_ms) {r.reason=policy_expired;return r;}
        if(current_slot<r.cutoff_slot) return r;
        uint64_t max_buy=0,trip=0,max_group=0;
        struct Group {uint64_t id=0,amount=0;};
        std::array<Group,512> groups{};
        for(const auto& w:wallets) if(w.id && w.buy) {
            ++r.buyers;max_buy=std::max(max_buy,w.buy);trip+=std::min(w.buy,w.sell);
            if(w.funder) {
                ++r.linked_buyers;
                for(size_t i=0;i<groups.size();++i) {
                    auto& group=groups[(w.funder+i)&511];
                    if(!group.id || group.id==w.funder) {
                        group.id=w.funder;group.amount+=w.buy;
                        max_group=std::max(max_group,group.amount);break;
                    }
                }
            } else max_group=std::max(max_group,w.buy);
        }
        for(size_t i=0;i<policy.window_slots/4;++i) r.positive_buckets+=buckets[i]>0;
        if(buys) {r.buyer_bps=Wide(max_buy)*10000/buys;r.funder_bps=Wide(max_group)*10000/buys;r.roundtrip_bps=Wide(trip)*10000/buys;}
        if(r.buyers<policy.buyers) r.reason=breadth;
        else if(r.positive_buckets<policy.positive_buckets || buckets[policy.window_slots/4-1]<=0)r.reason=persistence;
        else if(r.net_lamports<int64_t(policy.net_lamports))r.reason=demand;
        else if(r.buyer_bps>policy.buyer_bps)r.reason=concentration;
        else if(r.funder_bps>policy.funder_bps)r.reason=funding_concentration;
        else if(atomic_roundtrip || r.roundtrip_bps>policy.roundtrip_bps)r.reason=roundtrip;
        else if(dev_sells)r.reason=developer_selling;
        else if(!quote.id || current_slot<quote.slot || current_slot-quote.slot>policy.maximum_quote_age_slots)r.reason=quote_stale;
        else if(quote.real_sol<policy.minimum_real_sol || !quotes(quote,policy.notional,r.quote_tokens,r.quote_exit_lamports))r.reason=liquidity;
        else r.reason=candidate;
        return r;
    }
};
static_assert(sizeof(Event)==17*8 && sizeof(Policy)==12*8 && sizeof(Result)==17*8);
}
