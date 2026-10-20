#pragma once
#include "machine/economics/value.hpp"
#include <cmath>
#include <optional>

// Native research and planning only. This module cannot sign or submit an order.
// Prices/quantities at the executor boundary use Economic Machine's 1e6 scale.
namespace machine::economics::sol_mm {
using Ns = std::uint64_t;
constexpr Ns second = 1'000'000'000;
constexpr std::size_t dimensions = 7;
using Features = std::array<double, dimensions>;
inline bool positive(double x) { return std::isfinite(x) && x > 0; }
inline double units(std::int64_t x) { return double(x) / scale; }
inline std::optional<std::int64_t> fixed(double x) {
    if (!std::isfinite(x) || std::abs(x) > double(INT64_MAX / scale) - 1) return {};
    return std::llround(x * scale);
}

struct Observation {
    std::uint32_t source{}; // A configured pool, not an arbitrary ticker.
    std::uint64_t sequence{}, slot{};
    Ns received_ns{};
    double price{}, signed_sol{}, vault_sol{};
    bool executed{}, provisional{true};
};
struct PoolState {
    std::uint64_t sequence{}, slot{};
    Ns at{};
    double log_price{}, variance{1e-6}, signed_flow{}, gross_flow{}, vault_sol{},vault_change{};
    bool valid{};
    Ns vault_at{};
};
struct Fair {
    double price{}, sigma_bps{}, flow{}, liquidity_proxy_sol{};
    std::size_t pools{};
    Ns at{};
    bool provisional{};
    double liquidity_change{}; // Vault inventory change, not active CLMM depth.
};

// Independent per-pool robust local-level filters. Equal pool weights prevent
// one chatty feed from becoming thousands of apparently independent votes.
class FairFilter {
    std::array<PoolState, 32> pools_{};
public:
    std::uint64_t rejected{}, resets{};
    bool observe(const Observation& o) {
        if (o.source >= pools_.size() || !o.sequence || !o.received_ns || !o.slot ||
            !o.executed || !positive(o.price) || !std::isfinite(o.signed_sol) ||
            !std::isfinite(o.vault_sol) || o.vault_sol < 0) { ++rejected; return false; }
        auto& p = pools_[o.source];
        if (o.sequence <= p.sequence || (p.at && o.received_ns < p.at)) { ++rejected; return false; }
        double dt = p.at ? double(o.received_ns - p.at) / second : 0;
        double z = std::log(o.price), decay = std::exp(-dt / 2.0);
        if (!p.valid || dt > 5) { p.log_price = z; p.variance = 1e-6; p.signed_flow = p.gross_flow = 0; }
        else {
            // Huber innovation + scalar Kalman update; statistical precision is
            // capped. Trades are noisy observations, not executable spot quotes.
            double prior = std::min(1e-3, p.variance + 2e-8 * dt);
            double residual = z - p.log_price, noise = 1e-8;
            double bound = 3 * std::sqrt(prior + noise);
            double gain = prior / (prior + noise);
            p.log_price += gain * std::clamp(residual, -bound, bound);
            p.variance = std::max(1e-10, (1 - gain) * prior);
        }
        p.signed_flow = p.signed_flow * decay + o.signed_sol;
        p.gross_flow = p.gross_flow * decay + std::abs(o.signed_sol);
        if(!p.vault_at||(o.received_ns>=p.vault_at&&o.received_ns-p.vault_at>2*second)){
            p.vault_change=p.valid&&p.vault_sol>0&&dt<=5?std::clamp(std::log(std::max(o.vault_sol,1e-9)/p.vault_sol),-1.0,1.0):0;
            p.vault_sol=o.vault_sol;
        }
        p.sequence = o.sequence; p.slot = std::max(p.slot,o.slot);
        p.at = o.received_ns; p.valid = true; return true;
    }
    void invalidate_fork(std::uint64_t slot) {
        // Rebuilding statistical state is safer than subtracting from a Kalman
        // filter. Never undo account fills on the basis of a source-chain fork.
        for (auto& p : pools_) if (p.slot >= slot) { p.valid = false;p.vault_at=0;p.vault_sol=0;p.vault_change=0; ++resets; }
    }
    void reset() { for (auto& p : pools_) p.valid = false; ++resets; }
    bool vault(std::uint32_t source,std::uint64_t slot,Ns at,double sol){
        if(source>=pools_.size()||!slot||!at||!std::isfinite(sol)||sol<0)return false;
        auto& p=pools_[source];if(p.vault_at&&at<p.vault_at)return false;
        p.vault_change=p.vault_at&&p.vault_sol>0?std::clamp(std::log(std::max(sol,1e-9)/p.vault_sol),-1.0,1.0):0;
        p.vault_sol=sol;p.vault_at=at;p.slot=std::max(p.slot,slot);return true;
    }
    Fair snapshot(Ns now, Ns max_age = 2 * second) const {
        std::array<double, 32> prices{}; std::size_t n = 0;
        for (const auto& p : pools_) if (p.valid && now >= p.at && now - p.at < max_age) prices[n++] = p.log_price;
        if (!n) return {};
        std::sort(prices.begin(), prices.begin() + n);
        double median = prices[n / 2], sum = 0, weights = 0, flow = 0, liquidity = 0, dispersion = 0;
        Ns latest = 0;double liquidity_change=0;
        for (const auto& p : pools_) if (p.valid && now >= p.at && now - p.at < max_age) {
            double w = std::exp(-double(now - p.at) / second) / (1 + std::abs(p.log_price - median) / .0005);
            sum += w * p.log_price; weights += w;
            flow += w * p.signed_flow / std::max(1.0, p.gross_flow);
            dispersion += w * (p.variance + std::pow(p.log_price - median, 2));
            liquidity += p.vault_sol; liquidity_change+=w*p.vault_change;latest = std::max(latest, p.at);
        }
        return {std::exp(sum / weights), 10000 * std::sqrt(dispersion / weights), flow / weights,
                liquidity, n, latest, true,liquidity_change/weights};
    }
};

// Robust recursive least squares, updated only after an actual future outcome.
// This is a predictor, never a profitability certificate.
class Regression {
    Features beta_{};
    std::array<Features, dimensions> covariance_{};
    double error2_{1};
public:
    std::uint64_t labels{};
    Regression() { for (std::size_t i=0; i<dimensions; ++i) covariance_[i][i]=10; }
    double predict(const Features& x) const {
        double y=0; for(std::size_t i=0;i<dimensions;++i)y+=beta_[i]*x[i]; return std::clamp(y,-50.0,50.0);
    }
    bool learn(const Features& x,double y) {
        if(!std::isfinite(y)||std::abs(y)>1000)return false;
        for(double v:x)if(!std::isfinite(v)||std::abs(v)>10)return false;
        Features px{};double den=.999;
        for(std::size_t i=0;i<dimensions;++i){for(std::size_t j=0;j<dimensions;++j)px[i]+=covariance_[i][j]*x[j];den+=x[i]*px[i];}
        if(!positive(den))return false;
        double residual=y-predict(x), clipped=std::clamp(residual,-3*std::sqrt(error2_),3*std::sqrt(error2_));
        for(std::size_t i=0;i<dimensions;++i){beta_[i]+=px[i]/den*clipped;for(std::size_t j=0;j<dimensions;++j)covariance_[i][j]=(covariance_[i][j]-px[i]*px[j]/den)/.999;}
        error2_=.99*error2_+.01*std::min(residual*residual,10000.0);++labels;return true;
    }
    double uncertainty() const {return std::sqrt(error2_)*(labels<30?2:1);}
};
struct Forecast {double alpha_bps{}, uncertainty_bps{10};std::uint64_t labels{};};
class ForwardStudy {
    struct Pending {Ns due{}, born{};double mid{}, prediction{};Features x{};bool used{};};
    std::array<Pending,256> pending_{};std::size_t next_{};Ns sampled_{};
    Regression model_;
public:
    std::uint64_t missing{}, scored{};double model_squared_error{}, zero_squared_error{};
    void observe(Ns now,double mid,const Features& x) {
        if(!positive(mid))return;
        for(auto& p:pending_)if(p.used&&now>=p.due){
            if(now-p.due>250'000'000){++missing;p.used=false;continue;}
            double y=10000*std::log(mid/p.mid), e=y-p.prediction;
            model_squared_error+=e*e;zero_squared_error+=y*y;++scored;model_.learn(p.x,y);p.used=false;
        }
        if(sampled_&&now-sampled_<50'000'000)return;
        auto& p=pending_[next_++%pending_.size()];if(p.used)++missing;
        p={now+500'000'000,now,mid,model_.predict(x),x,true};sampled_=now;
    }
    void gap(){for(auto&p:pending_)if(p.used){++missing;p.used=false;}}
    Forecast forecast(const Features& x) const {
        // Do not silently add unproven on-chain directional alpha to fair value.
        bool advantage=scored>=100&&model_squared_error<zero_squared_error;
        return {advantage?model_.predict(x):0,model_.uncertainty(),model_.labels};
    }
};

struct QueueBounds {double ahead_low{},ahead_high{};bool exact_rank{false};};
inline QueueBounds queue_after(double initial,double traded,double cancelled,double added) {
    if(!std::isfinite(initial+traded+cancelled+added)||std::min({initial,traded,cancelled,added})<0)return {};
    // Aggregate L2 cannot identify which cancellations were ahead of us.
    return {std::max(0.0,initial-traded-cancelled),std::max(0.0,initial-traded)+added,false};
}
class Survival {
    double exposure_{}, events_{};
public:
    bool observe(double seconds_at_risk,bool first_fill) {
        if(!std::isfinite(seconds_at_risk)||seconds_at_risk<0||seconds_at_risk>30)return false;
        exposure_+=seconds_at_risk;events_+=first_fill?1:0;return true;
    }
    double intensity()const{return (events_+.5)/(exposure_+5);}
    double probability(double horizon,double ahead,double flow_rate)const {
        double delay=ahead/std::max(flow_rate,.01);
        return -std::expm1(-intensity()*std::max(0.0,horizon-delay));
    }
    bool supported()const{return events_>=30&&exposure_>=60;}
    double exposure()const{return exposure_;}
};
struct FillLabel {std::uint64_t receipt{};Ns at{},due{};double price{};int side{};Features x{};bool active{};};
class ConditionalMarkout {
    std::array<FillLabel,256> pending_{};std::array<std::uint64_t,4096> seen_{};
    std::size_t next_{},seen_next_{};Regression model_;
public:
    std::uint64_t missing{}, accepted{};
    bool fill(std::uint64_t id,Ns now,int side,double price,const Features& x) {
        if(!id||!now||(side!=1&&side!=-1)||!positive(price))return false;
        for(auto seen:seen_)if(seen==id)return false;
        seen_[seen_next_++%seen_.size()]=id;
        auto&p=pending_[next_++%pending_.size()];if(p.active)++missing;
        p={id,now,now+second,price,side,x,true};++accepted;return true;
    }
    void mark(Ns now,double mid) {
        if(!positive(mid))return;
        for(auto&p:pending_)if(p.active&&now>=p.due){
            if(now-p.due>250'000'000)++missing;
            else model_.learn(p.x,p.side*10000*std::log(mid/p.price));
            p.active=false;
        }
    }
    void gap(){for(auto&p:pending_)if(p.active){p.active=false;++missing;}}
    Forecast forecast(const Features& x)const{return {model_.labels>=30?model_.predict(x):0,model_.uncertainty(),model_.labels};}
};

struct ModelSnapshot {
    std::uint64_t version{};Ns at{};
    double fair{},sigma_bps{},flow{},direction_bps{},markout_bps{},markout_uncertainty_bps{};
    double fill_intensity{.1};
    bool conditional_supported{},onchain_available{};
    std::array<double,2> side_markout{},side_uncertainty{},side_reference{};
    std::array<bool,2> side_supported{};
};
inline Features features(const Fair& f,double mid,double spread_bps,double inventory_fraction,int side=1) {
    return {1,std::clamp(f.price>0?side*10000*std::log(f.price/mid)/10:0,-5.0,5.0),
            side*f.flow,std::clamp(f.sigma_bps/10,0.0,5.0),std::clamp(spread_bps/10,0.0,5.0),
            std::clamp(double(side)*inventory_fraction,-1.0,1.0),std::clamp(f.liquidity_change*100,-5.0,5.0)};
}

enum class Action : std::uint8_t {keep,create,cancel,wait};
enum class Reason : std::uint8_t {queue_value,new_quote,adverse_edge,replace_gain,inventory,stale,account_pending,no_capacity};
struct Resting {
    std::uint64_t id{};std::int64_t price{},remaining{};Ns born{};
    QueueBounds queue{};bool pending{};bool reduce_only{};
};
struct Account {
    std::uint64_t revision{};Ns at{};
    double equity{},available_margin{},allocated_margin{60};int leverage{20};
    std::int64_t net{},opening_reserved{},closing_reserved{}; // SOL in 1e6 units.
    bool reconciled{};
    double opening_reserved_usd{-1};
};
struct Market {Ns at{};std::int64_t bid{},ask{},tick{10000},lot{10000};double min_notional{10};};
struct Parameters {
    double maker_rebate_bps{.5},inventory_penalty{.0005},replacement_cost_bps{.1};
    double horizon_seconds{2},order_fraction{.5},max_dislocation_bps{30};
    Ns market_age{second},model_age{2*second},account_age{15*second};
};
struct Proposal {
    Action action{Action::wait};Reason reason{Reason::no_capacity};int side{};
    std::uint64_t replaces{};std::int64_t price{},quantity{};bool reduce_only{};
    double utility_usd{},keep_utility_usd{},fill_probability{},expected_exit_seconds{};
};
struct Plan {
    std::uint64_t account_revision{},model_version{};Ns at{};
    std::array<Proposal,2> sides{};bool market_orders_allowed{false};
};
inline bool fresh(Ns now,Ns at,Ns age){return at&&now>=at&&now-at<age;}

// Slow side: evaluate a bounded maker-only inventory objective. The result is a
// compact plan. A connector must recheck account revision before reservation.
// No liquidation price, venue fee, queue-rank or actual fill is invented here.
inline Plan plan(Ns now,const Market& book,const Account& account,const ModelSnapshot& model,
                 const std::array<Resting,2>& orders,const Parameters& p={}) {
    Plan out{account.revision,model.version,now};
    const bool valid_book=book.bid>0&&book.ask>book.bid&&book.tick>0&&book.lot>0&&fresh(now,book.at,p.market_age);
    const bool valid_account=account.reconciled&&account.leverage==20&&positive(account.equity)&&
        std::isfinite(account.available_margin)&&account.available_margin>=0&&positive(account.allocated_margin)&&fresh(now,account.at,p.account_age);
    const bool valid_model=positive(model.fair)&&std::isfinite(model.sigma_bps)&&model.sigma_bps>=0&&
        std::isfinite(model.direction_bps)&&std::isfinite(model.markout_bps)&&std::isfinite(model.markout_uncertainty_bps)&&
        model.markout_uncertainty_bps>=0&&positive(model.fill_intensity)&&fresh(now,model.at,p.model_age);
    if(!valid_book||!valid_account||!valid_model) {
        for(int i=0;i<2;++i)out.sides[i]={orders[i].id&&!orders[i].pending?Action::cancel:Action::wait,
            !valid_account?Reason::account_pending:Reason::stale,i==0?1:-1,orders[i].id};
        return out;
    }
    const double mid=(units(book.bid)+units(book.ask))/2, inventory=units(account.net);
    const double cap=std::min(account.equity,account.allocated_margin)*account.leverage;
    const double reserved=account.opening_reserved_usd>=0?account.opening_reserved_usd:units(account.opening_reserved)*mid;
    double capacity=std::max(0.0,std::min(cap-std::abs(inventory)*mid-reserved,
        account.available_margin*account.leverage));
    double fair=std::clamp(model.fair,mid*(1-p.max_dislocation_bps/10000),mid*(1+p.max_dislocation_bps/10000));
    fair*=std::exp(std::clamp(model.direction_bps,-10.0,10.0)/10000);
    const double penalty=p.inventory_penalty*(1+model.sigma_bps/10);
    auto utility=[&](int side,double price,double qty,double ahead) {
        double lambda=model.fill_intensity/(1+std::max(0.0,ahead));
        double probability=-std::expm1(-lambda*p.horizon_seconds);
        double edge=side*(fair-price)+price*p.maker_rebate_bps/10000;
        // Conditional markout is anchored to the fill price and already
        // contains directional movement: never add it to fair-value edge.
        if(model.conditional_supported)edge=price*(model.markout_bps-model.markout_uncertainty_bps+p.maker_rebate_bps)/10000;
        const auto i=side==1?0:1;
        if(model.side_supported[i]&&positive(model.side_reference[i])&&std::isfinite(model.side_markout[i])&&
           std::isfinite(model.side_uncertainty[i])&&model.side_uncertainty[i]>=0)
            edge=model.side_reference[i]*(model.side_markout[i]-model.side_uncertainty[i])/10000+
                side*(model.side_reference[i]-price)+price*p.maker_rebate_bps/10000;
        double after=inventory+side*qty;
        double inventory_cost=penalty*(after*after-inventory*inventory);
        return std::array<double,3>{probability*(qty*edge-inventory_cost),probability,1/std::max(lambda,1e-9)};
    };
    for(int i=0;i<2;++i) {
        const int side=i==0?1:-1;const auto& old=orders[i];auto& result=out.sides[i];result.side=side;result.replaces=old.id;
        if(old.pending){result.reason=Reason::account_pending;continue;}
        bool reducing=side*inventory<0;
        double available_close=std::max(0.0,std::abs(inventory)-units(account.closing_reserved));
        // The caller excludes this side's old reservation when evaluating a
        // replacement. Nothing is released until its actual terminal receipt.
        if(reducing&&old.reduce_only)available_close+=units(old.remaining);
        double qty=reducing?std::min(available_close,std::max(units(book.lot),cap*p.order_fraction/mid)):
                             std::min(cap*p.order_fraction,capacity)/(units(book.ask)+4*units(book.tick));
        if(old.id&&!reducing)qty=std::max(qty,units(old.remaining));
        auto qfixed=fixed(qty);if(!qfixed)continue;
        std::int64_t lots=*qfixed/book.lot*book.lot;
        if(lots<=0||units(lots)*mid<book.min_notional){result.action=old.id?Action::cancel:Action::wait;result.reason=Reason::no_capacity;continue;}
        double best=-1e300;Proposal candidate;
        // Five passive prices, no market or IOC candidate. A more urgent exit
        // competes at the touch; maker-only exit time is not guaranteed.
        for(int ticks=0;ticks<5;++ticks) {
            Wide raw=side==1?Wide(book.bid)-Wide(ticks)*book.tick:Wide(book.ask)+Wide(ticks)*book.tick;
            auto rounded=narrow(raw);if(!rounded||rounded.value<=0)continue;
            auto price=rounded.value;double ahead=std::max(0.0,old.queue.ahead_high)+ticks;
            auto u=utility(side,units(price),units(lots),ahead);
            if(u[0]>best){best=u[0];candidate={Action::create,Reason::new_quote,side,old.id,price,lots,reducing,u[0],0,u[1],u[2]};}
        }
        if(old.id) {
            auto keep=utility(side,units(old.price),units(old.remaining),old.queue.ahead_low);
            result={Action::keep,Reason::queue_value,side,old.id,old.price,old.remaining,old.reduce_only,keep[0],keep[0],keep[1],keep[2]};
            const bool quantity_unsafe=old.reduce_only?(!reducing||units(old.remaining)>std::abs(inventory)+1e-9):
                reducing||std::abs(inventory)*mid+reserved>cap+1e-8;
            double worst_edge=side*(fair-units(old.price))+units(old.price)*p.maker_rebate_bps/10000;
            // Inventory relief is part of the same objective, not a separate
            // process stop. Healthy quotes do not expire on an arbitrary timer.
            if(quantity_unsafe){result.action=Action::cancel;result.reason=Reason::inventory;}
            else if(worst_edge< -units(book.tick)*2&&!reducing){result.action=Action::cancel;result.reason=Reason::adverse_edge;}
            else if(candidate.price!=old.price||candidate.reduce_only!=old.reduce_only||candidate.quantity<old.remaining) {
                double switching=units(old.price)*units(old.remaining)*p.replacement_cost_bps/10000;
                if(best>keep[0]+switching){result=candidate;result.action=Action::cancel;result.reason=Reason::replace_gain;result.keep_utility_usd=keep[0];}
            }
        } else if(best>=0||reducing) {
            result=candidate;
            if(!reducing)capacity=std::max(0.0,capacity-units(lots)*units(candidate.price));
        }
    }
    return out;
}

// Fast side: version/freshness and equality checks only; the potentially slow
// fair-value/learning/candidate search above runs outside the order callback.
inline bool applicable(const Plan& p,Ns now,std::uint64_t revision,Ns max_age=100'000'000) {
    return !p.market_orders_allowed&&p.account_revision==revision&&fresh(now,p.at,max_age);
}
} // namespace machine::economics::sol_mm
