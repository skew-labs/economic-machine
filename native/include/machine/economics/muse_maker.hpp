#pragma once
#include "machine/economics/value.hpp"

// Replacement maker's pure decision kernel. No allocator, JSON, model request,
// network or signing in this path. A returned plan is NOT execution authority.
namespace machine::economics::muse_maker {
enum class Mode : std::int64_t { quote, reduce_only, pause };
struct Policy {
    Mode mode{Mode::pause};
    std::int64_t half_spread_millibps{}, inventory_skew_millibps{}, flow_shift_millibps{};
    std::int64_t volatility_multiplier_milli{}, clip_usd_micros{}, refresh_ticks{}, ttl_ms{};
};
struct Input {
    std::int64_t now_ms{}, market_ms{}, account_ms{}, policy_ms{};
    std::int64_t bid{}, ask{}, tick{}, lot{}, minimum_notional{}; // 1e6 fixed units
    std::int64_t equity{}, free_margin{}, inventory{}, pending_opening{};
    std::int64_t pending_reducing{}; // absolute base quantity already reserved
    std::int64_t sigma_millibps{}, signed_flow_millionths{};
    bool reconciled{}, exclusive_owner{}, flow_available{};
    bool bid_outstanding{}, ask_outstanding{};
};
struct Quote { std::int64_t price{}, quantity{}; bool reduce_only{}; };
struct Plan { Quote bid{}, ask{}; Error error{Error::unknown}; std::int64_t opening_cap{}; };

inline bool valid(const Policy& p) noexcept {
    return p.mode>=Mode::quote && p.mode<=Mode::pause &&
        p.half_spread_millibps>=100 && p.half_spread_millibps<=20000 &&
        p.inventory_skew_millibps>=0 && p.inventory_skew_millibps<=50000 &&
        p.flow_shift_millibps>=0 && p.flow_shift_millibps<=5000 &&
        p.volatility_multiplier_milli>=0 && p.volatility_multiplier_milli<=5000 &&
        p.clip_usd_micros>=scale && p.clip_usd_micros<=600*scale &&
        p.refresh_ticks>=1 && p.refresh_ticks<=20 && p.ttl_ms>=1000 && p.ttl_ms<=300000;
}
inline bool fresh(std::int64_t now,std::int64_t stamp,std::int64_t age) noexcept {
    return stamp>0 && now>=stamp && now-stamp<=age;
}
inline Wide notional(std::int64_t price,std::int64_t quantity) noexcept {
    return (Wide(price)*quantity+scale-1)/scale;
}
inline std::int64_t sized(std::int64_t budget,std::int64_t price,std::int64_t lot) noexcept {
    return static_cast<std::int64_t>(Wide(budget)*scale/price/lot)*lot;
}
inline std::int64_t minimum_quantity(std::int64_t price,std::int64_t lot,std::int64_t minimum) noexcept {
    const Wide unit=Wide(price)*lot;
    return static_cast<std::int64_t>((Wide(minimum)*scale+unit-1)/unit)*lot;
}
inline std::int64_t executable_size(std::int64_t budget,std::int64_t price,std::int64_t lot,std::int64_t minimum) noexcept {
    const auto quantity=sized(budget,price,lot);
    // Do not round an order UP beyond available capital to meet venue minimum.
    return quantity>=minimum_quantity(price,lot,minimum)?quantity:0;
}

inline Plan decide(const Policy& p,const Input& s) noexcept {
    Plan out;
    auto fail=[&](Error e){out.error=e;return out;};
    if(!valid(p))return fail(Error::invalid);
    // Explicit numerical domain permits checked wide intermediate arithmetic.
    constexpr auto max_price=1'000'000*scale, max_qty=1'000'000*scale;
    if(s.bid<=0 || s.ask<=s.bid || s.ask>max_price || s.tick<=0 || s.tick>s.bid ||
       s.bid%s.tick || s.ask%s.tick || s.lot<=0 || s.lot>max_qty ||
       s.minimum_notional<=0 || s.minimum_notional>1200*scale ||
       s.equity<0 || s.equity>1'000'000*scale || s.free_margin<0 || s.free_margin>1'000'000*scale ||
       s.inventory < -max_qty || s.inventory>max_qty || s.inventory%s.lot ||
       s.pending_opening<0 || s.pending_opening>1'000'000*scale ||
       s.pending_reducing<0 || s.pending_reducing>max_qty ||
       s.sigma_millibps<0 || s.sigma_millibps>1'000'000 ||
       s.signed_flow_millionths < -scale || s.signed_flow_millionths>scale)
        return fail(Error::invalid);
    if(!s.reconciled)return fail(Error::unknown);
    if(!s.exclusive_owner)return fail(Error::conflict);
    if(!fresh(s.now_ms,s.market_ms,2000) || !fresh(s.now_ms,s.account_ms,2000))return fail(Error::stale);
    if(!fresh(s.now_ms,s.policy_ms,p.ttl_ms-1))return fail(Error::expired);
    out.opening_cap=20*std::min(s.equity,60*scale);
    if(p.mode==Mode::pause){out.error=Error::okay;return out;}
    const auto abs_inventory=s.inventory<0?-s.inventory:s.inventory;
    const Wide position_notional=notional(s.ask,abs_inventory); // conservative upper mark
    if(s.pending_reducing>abs_inventory)return fail(Error::conflict);
    const auto mid=s.bid+(s.ask-s.bid)/2;
    const auto ratio=out.opening_cap>0 ? static_cast<std::int64_t>(std::min<Wide>(scale,position_notional*scale/out.opening_cap)):scale;
    const auto signed_ratio=s.inventory<0?-ratio:(s.inventory>0?ratio:0);
    const Wide skew=Wide(p.inventory_skew_millibps)*signed_ratio/scale;
    const Wide flow=s.flow_available?Wide(p.flow_shift_millibps)*s.signed_flow_millionths/scale:0;
    const Wide center=Wide(mid)+Wide(mid)*(flow-skew)/10'000'000;
    const Wide half=std::max<Wide>(p.half_spread_millibps,Wide(s.sigma_millibps)*p.volatility_multiplier_milli/1000);
    const Wide distance=Wide(mid)*half/10'000'000;
    Wide bp=std::min<Wide>(s.bid,center-distance), ap=std::max<Wide>(s.ask,center+distance);
    if(bp<=0 || ap>max_price)return fail(Error::domain);
    bp=bp/s.tick*s.tick; ap=(ap+s.tick-1)/s.tick*s.tick;
    if(bp<=0 || ap>max_price || bp>=ap)return fail(Error::domain);
    out.bid.price=static_cast<std::int64_t>(bp);out.ask.price=static_cast<std::int64_t>(ap);
    Wide remaining=std::max<Wide>(0,Wide(out.opening_cap)-position_notional-s.pending_opening);
    remaining=std::min<Wide>(remaining,Wide(s.free_margin)*20);
    std::int64_t reducing=abs_inventory-s.pending_reducing;
    // Opening reservations include BOTH sides. Never allocate 1200 to each side.
    for(int side : {1,-1}) {
        auto& q=side==1?out.bid:out.ask;
        q.reduce_only=(s.inventory>0 && side<0)||(s.inventory<0 && side>0);
        // Pending opening value is already subtracted above. Do not allocate a
        // second hypothetical budget to a side that already owns a quote.
        if(side==1?s.bid_outstanding:s.ask_outstanding)continue;
        if(q.reduce_only) {
            q.quantity=std::min(executable_size(p.clip_usd_micros,q.price,s.lot,s.minimum_notional),reducing/s.lot*s.lot);
            reducing-=q.quantity;
        }else if(p.mode==Mode::quote) {
            const Wide share=s.inventory==0&&side==1&&!s.ask_outstanding?remaining/2:remaining;
            auto budget=static_cast<std::int64_t>(std::min<Wide>(p.clip_usd_micros,share));
            q.quantity=executable_size(budget,q.price,s.lot,s.minimum_notional);
            remaining-=notional(q.price,q.quantity);
        }
        if(notional(q.price,q.quantity)<s.minimum_notional)q.quantity=0;
    }
    out.error=Error::okay;
    return out;
}
}
