#pragma once
#include <machine/economics/muse_maker.hpp>
#include "pacifica_controller.hpp"

// Host-injected venue boundary. This class never reads credentials, opens a
// network connection, installs a service, or grants itself execution authority.
namespace muse_session {
namespace mm=machine::economics::muse_maker;
using pacifica::J;
using machine::economics::scale;
using machine::economics::Error;
using pacifica::require;
inline constexpr std::int64_t duration_ms=48LL*60*60*1000;

struct Lease {
    std::int64_t start{},deadline{},last{};
    bool stopped{};
    static Lease begin(std::int64_t now) {
        require(now>0&&now<INT64_MAX-duration_ms,"lease clock domain");
        return {now,now+duration_ms,now,false};
    }
    bool observe(std::int64_t now) {
        if(now<last||now>=deadline)stopped=true;
        last=std::max(last,now);
        return !stopped;
    }
    J json()const{return {{"start",start},{"deadline",deadline},{"last",last},{"stopped",stopped}};}
    static Lease load(const J& j) {
        Lease l{j.at("start"),j.at("deadline"),j.at("last"),j.at("stopped")};
        require(l.start>0&&l.start<INT64_MAX-duration_ms&&l.deadline==l.start+duration_ms&&l.last>=l.start,
                "immutable 48 hour lease");
        if(l.last>=l.deadline)l.stopped=true;
        return l;
    }
};

inline std::int64_t fixed(double x,bool round_up=false) {
    require(std::isfinite(x)&&x>=0&&x<=1'000'000,"fixed value domain");
    return static_cast<std::int64_t>(round_up?std::ceil(x*scale):std::floor(x*scale));
}
inline std::int64_t venue_price(double x) {
    require(std::isfinite(x)&&x>0&&x<=1'000'000,"venue price domain");
    auto cents=std::llround(x*100);
    require(std::abs(x*100-cents)<1e-6,"off-grid venue price");
    return cents*10000;
}

class Session {
    Lease lease_;
    mm::Policy policy_{};
    std::int64_t policy_at_{},policy_version_{};
    bool installed_{};
public:
    pacifica::Controller controller;
    std::uint64_t plans{},creates{},cancels{};
    bool expected_rebate_verified{};
    mm::Plan last_plan{};
    const char* last_reason{"awaiting_policy"};
    Session(Lease lease,std::function<void()> persist,
            std::function<void(const std::string&,const std::string&,const J&)> transport):
        lease_(lease),controller(std::move(persist),std::move(transport)) {
        controller.policy.continuous=true; // Owner requested no P&L stop.
        controller.policy.native_continuity=false;
        controller.policy.capital_usd=60;
        controller.policy.leverage=20;
    }
    // The host validates the pinned Muse response and future-input sanity gate.
    // This does not establish profitability. Replaying cannot renew the TTL.
    void install(mm::Policy policy,std::int64_t issued,std::int64_t version,std::int64_t now) {
        require(mm::valid(policy)&&version>policy_version_&&mm::fresh(now,issued,policy.ttl_ms-1),"fresh new policy version required");
        require(issued>policy_at_,"policy issue time must advance across restart");
        policy_=policy;policy_at_=issued;policy_version_=version;installed_=true;
        controller.persist();
    }
    J state()const {
        return {{"schema","muse-maker-session-v1"},{"lease",lease_.json()},
                {"ledger",controller.ledger.json()},
                {"policy_version",policy_version_},{"policy_at",policy_at_}};
    }
    // Policy is intentionally not restored as fresh after a crash. Ownership,
    // reservations and original deadline are restored, then re-reconciled.
    void restore(const J& j) {
        require(j.at("schema")=="muse-maker-session-v1","session schema");
        lease_=Lease::load(j.at("lease"));controller.ledger.load(j.at("ledger"));
        policy_version_=j.at("policy_version");policy_at_=j.at("policy_at");
        require(policy_version_>=0&&policy_at_>=0,"persisted policy version");
        installed_=false;controller.ledger.reconciled=false;
    }
    const Lease& lease()const{return lease_;}
    std::int64_t policy_version()const{return policy_version_;}
    const mm::Policy& policy()const{return policy_;}
    bool policy_ready(std::int64_t now)const{return installed_&&mm::fresh(now,policy_at_,policy_.ttl_ms-1);}

    void tick(const quant::Top& t,std::int64_t now,bool exclusive_writer,
              std::int64_t sigma_millibps,std::int64_t flow_millionths,bool flow_available,bool evaluate_only=false) {
        auto& c=controller;auto& l=c.ledger;
        // Reject the legacy account-sized continuity mode. These limits cannot
        // be enlarged by a model response, account profit or another caller.
        require(c.policy.continuous&&!c.policy.native_continuity&&c.policy.capital_usd==60&&c.policy.leverage==20,
                "owner limits changed");
        bool was_stopped=lease_.stopped;
        bool within=lease_.observe(now);
        if(!was_stopped&&lease_.stopped)c.persist();
        bool fresh=policy_ready(now)&&t.valid()&&t.fresh_at(now)&&mm::fresh(now,l.account_ms,2000);
        bool account=l.reconciled&&l.halt.empty()&&!l.missing_fills()&&c.connected_account();
        expected_rebate_verified=std::abs(l.fee_bps+0.5)<1e-9;
        bool permitted=within&&fresh&&account&&exclusive_writer;
        bool opening_over_cap=false;
        last_reason=!within?"session_closed":!exclusive_writer?"writer_not_exclusive":
            !policy_ready(now)?"policy_unavailable_or_expired":!t.valid()||!t.fresh_at(now)?"market_stale":
            !mm::fresh(now,l.account_ms,2000)?"account_stale":!account?"account_not_reconciled":"evaluating";
        mm::Plan plan;
        if(permitted) {
            std::int64_t reducing=0;
            for(const auto&[_,o]:l.orders)if(!o.done()&&o.reduce)reducing+=o.reserved()*10000;
            mm::Input input{now,std::max(t.source_ms,t.confirmed_wall_ms),l.account_ms,policy_at_,
                venue_price(t.bid),venue_price(t.ask),10000,10000,fixed(c.market.min_usd,true),
                fixed(std::max(0.0,l.equity)),fixed(std::max(0.0,l.free_margin)),
                l.net_lots*10000,fixed(l.opening_pending(),true),reducing,
                sigma_millibps,flow_millionths,true,exclusive_writer,flow_available,
                l.outstanding(1),l.outstanding(-1)};
            auto policy=policy_;
            if(policy.mode!=mm::Mode::pause&&(c.margin_breached()||!expected_rebate_verified))policy.mode=mm::Mode::reduce_only;
            plan=mm::decide(policy,input);++plans;
            permitted=plan.error==Error::okay;
            opening_over_cap=permitted&&mm::notional(input.ask,std::abs(input.inventory))+input.pending_opening>plan.opening_cap;
            last_reason=!permitted?"native_plan_rejected":policy.mode==mm::Mode::pause?"muse_pause":
                plan.bid.quantity||plan.ask.quantity?"quote_available":l.any_open_order()?"existing_orders":
                policy.mode==mm::Mode::reduce_only?"no_reducible_inventory":"remaining_budget_below_minimum";
        }
        last_plan=plan;
        if(evaluate_only)return; // No journal reservation, signature or cancel.
        // Cancellation is scoped to owned orders. ACK alone cannot release a
        // reservation; Controller waits for terminal state AND matching fills.
        for(auto&[_,o]:l.orders)if(!o.done()) {
            const auto& desired=o.side==1?plan.bid:plan.ask;
            bool should_reduce=(l.net_lots>0&&o.side<0)||(l.net_lots<0&&o.side>0);
            std::string reason;
            if(!permitted)reason=within?"muse_source_account_or_policy_unavailable":"lease_48h_closed";
            else if(policy_.mode==mm::Mode::pause)reason="muse_pause";
            else if(opening_over_cap&&!o.reduce)reason="aggregate_cap_reduced";
            else if((policy_.mode==mm::Mode::reduce_only||c.margin_breached()||!expected_rebate_verified)&&!o.reduce)reason="reduce_only";
            else if(o.reduce!=should_reduce||(o.reduce&&o.reserved()>std::abs(l.net_lots)))reason="inventory_changed";
            else if(o.side==1?o.price/100.0>=t.ask:o.price/100.0<=t.bid)reason="crossed_quote";
            else if(std::abs(desired.price-o.price*10000)>=policy_.refresh_ticks*10000)reason="material_reprice";
            if(!reason.empty()&&c.cancel(o,now,false,reason))++cancels;
        }
        if(!permitted||l.unknown())return;
        for(const auto&[_,o]:l.orders)if(!o.done()&&o.cancel_requested)return;
        // At most one unresolved create at a time. Recompute on the next event
        // after the ACK; never send both sides against the same reservation.
        for(int side:{1,-1}) {
            auto q=side==1?plan.bid:plan.ask;
            if(!q.quantity||l.outstanding(side))continue;
            if(q.price%10000||q.quantity%10000)throw std::runtime_error("venue grid changed");
            pacifica::Quote proposal;
            proposal.side=side;proposal.price=q.price/10000;proposal.qty=q.quantity/10000;proposal.reduce=q.reduce_only;
            proposal.basis="muse_economic_machine_v1";proposal.expected_edge_bps=0;
            auto before=l.submissions;c.create(proposal,t.mid(),now);creates+=l.submissions-before;
            break;
        }
    }
};
}
