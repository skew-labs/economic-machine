#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

enum class OrderSide : std::uint32_t { buy = 1, sell = 2 };
enum class OrderType : std::uint32_t { limit = 1, immediate_or_cancel = 2, fill_or_kill = 3 };

struct DepthLevel {
    std::int64_t price{};
    std::int64_t quantity{};
};

struct VenueDepth {
    std::uint64_t venue{};
    std::uint64_t instrument{};
    std::uint64_t quote_asset{};
    std::uint64_t base_asset{};
    std::array<DepthLevel, 32> levels{};
    std::uint32_t count{};
    OrderSide side{};
    std::int64_t taker_fee_bps{};
    std::int64_t fixed_cost{};
    std::int64_t quantity_step{};
    std::int64_t minimum_quantity{};
    std::int64_t minimum_notional{};
    std::int64_t maximum_notional{};
    Envelope envelope{};
};

struct DepthQuote {
    std::uint64_t venue{};
    std::int64_t requested_quantity{};
    std::int64_t filled_quantity{};
    std::int64_t notional{};
    std::int64_t fee{};
    std::int64_t total_cash_flow{};
    std::int64_t average_price{};
    std::int64_t worst_price{};
    std::uint32_t levels_consumed{};
    std::uint64_t valid_until_ns{};
};

[[nodiscard]] inline Error validate_depth(const VenueDepth& depth) noexcept {
    if (!depth.venue || !depth.instrument || !depth.quote_asset || !depth.base_asset ||
        depth.quote_asset == depth.base_asset || !depth.count || depth.count > depth.levels.size() ||
        (depth.side != OrderSide::buy && depth.side != OrderSide::sell) ||
        depth.taker_fee_bps < 0 || depth.taker_fee_bps > bps_scale || depth.fixed_cost < 0 ||
        depth.quantity_step <= 0 || depth.minimum_quantity < 0 || depth.minimum_notional < 0 ||
        depth.maximum_notional < depth.minimum_notional) {
        return Error::invalid;
    }
    const auto envelope_error = validate(depth.envelope);
    if (envelope_error != Error::okay) {
        return envelope_error;
    }
    for (std::size_t i = 0; i < depth.count; ++i) {
        const auto& level = depth.levels[i];
        if (level.price <= 0 || level.quantity <= 0 || level.quantity % depth.quantity_step) {
            return Error::invalid;
        }
        if (i) {
            const auto previous = depth.levels[i - 1].price;
            if ((depth.side == OrderSide::buy && level.price <= previous) ||
                (depth.side == OrderSide::sell && level.price >= previous)) {
                return Error::sequence;
            }
        }
    }
    return Error::okay;
}

[[nodiscard]] inline Result<DepthQuote> depth_quote(
    const VenueDepth& depth,
    std::int64_t requested_quantity,
    std::int64_t limit_price,
    bool allow_partial
) noexcept {
    const auto valid = validate_depth(depth);
    if (valid != Error::okay) {
        return failure<DepthQuote>(valid);
    }
    if (requested_quantity <= 0 || requested_quantity % depth.quantity_step || limit_price <= 0) {
        return failure<DepthQuote>(Error::invalid);
    }
    DepthQuote quote{};
    quote.venue = depth.venue;
    quote.requested_quantity = requested_quantity;
    auto remaining = requested_quantity;
    Wide weighted = 0;
    for (std::size_t i = 0; i < depth.count && remaining; ++i) {
        const auto& level = depth.levels[i];
        if ((depth.side == OrderSide::buy && level.price > limit_price) ||
            (depth.side == OrderSide::sell && level.price < limit_price)) {
            break;
        }
        const auto take = std::min(remaining, level.quantity);
        const auto term = static_cast<Wide>(take) * level.price;
        if (term > static_cast<Wide>(INT64_MAX) * scale - weighted) {
            return failure<DepthQuote>(Error::overflow);
        }
        weighted += term;
        quote.worst_price = level.price;
        ++quote.levels_consumed;
        remaining -= take;
    }
    if (remaining && !allow_partial) {
        return failure<DepthQuote>(Error::illiquid);
    }
    quote.filled_quantity = requested_quantity - remaining;
    if (!quote.filled_quantity || quote.filled_quantity < depth.minimum_quantity) {
        return failure<DepthQuote>(Error::illiquid);
    }
    const auto notional = divide_wide(weighted, scale,
        depth.side == OrderSide::buy ? Rounding::ceil : Rounding::floor);
    const auto average = divide_wide(weighted, quote.filled_quantity,
        depth.side == OrderSide::buy ? Rounding::ceil : Rounding::floor);
    if (!notional || !average) {
        return failure<DepthQuote>(Error::overflow);
    }
    if (notional.value < depth.minimum_notional || notional.value > depth.maximum_notional) {
        return failure<DepthQuote>(Error::infeasible);
    }
    const auto fee = basis_points(notional.value, depth.taker_fee_bps, Rounding::ceil);
    if (!fee) {
        return failure<DepthQuote>(fee.error);
    }
    const auto flow = depth.side == OrderSide::buy
        ? narrow(static_cast<Wide>(notional.value) + fee.value + depth.fixed_cost)
        : narrow(static_cast<Wide>(notional.value) - fee.value - depth.fixed_cost);
    if (!flow) {
        return failure<DepthQuote>(flow.error);
    }
    if (flow.value <= 0) {
        return failure<DepthQuote>(Error::cost);
    }
    const auto until = expiry(depth.envelope);
    if (!until) {
        return failure<DepthQuote>(until.error);
    }
    quote.notional = notional.value;
    quote.average_price = average.value;
    quote.fee = fee.value;
    quote.total_cash_flow = flow.value;
    quote.valid_until_ns = until.value;
    return success(quote);
}

struct RoutePolicy {
    std::uint64_t instrument{};
    std::uint64_t base_asset{};
    std::uint64_t quote_asset{};
    OrderSide side{};
    OrderType order_type{};
    std::int64_t quantity{};
    std::int64_t limit_price{};
    std::int64_t quote_budget{};
    std::int64_t maximum_slippage_bps{};
    std::int64_t reference_price{};
    std::uint32_t maximum_venues{};
};

struct SplitChild {
    std::uint64_t venue{};
    std::int64_t quantity{};
    std::int64_t limit_price{};
    std::int64_t notional{};
    std::int64_t fee{};
    std::int64_t fixed_cost{};
    std::int64_t cash_flow{};
    std::uint64_t sequence{};
    std::uint64_t valid_until_ns{};
};

struct SplitPlan {
    std::array<SplitChild, 16> children{};
    std::uint32_t count{};
    std::int64_t requested_quantity{};
    std::int64_t filled_quantity{};
    std::int64_t unfilled_quantity{};
    std::int64_t total_notional{};
    std::int64_t total_fees{};
    std::int64_t fixed_costs{};
    std::int64_t cash_flow{};
    std::uint64_t valid_until_ns{};
    std::uint64_t execution_authority{};
};

// Enumerate complete single-venue quotes first. Selecting a venue by top-of-
// book price alone ignores depth and per-order costs. Splits are deliberately
// separate below because greedy marginal prices are not globally optimal in
// the presence of fixed costs and minimum orders.
[[nodiscard]] inline Result<SplitPlan> best_single_venue(
    std::span<const VenueDepth> venues,
    const RoutePolicy& policy
) noexcept {
    if (venues.empty() || venues.size() > 16 || !policy.instrument || !policy.base_asset ||
        !policy.quote_asset || policy.base_asset == policy.quote_asset || policy.quantity <= 0 ||
        policy.limit_price <= 0 || policy.quote_budget < 0 || policy.reference_price <= 0 ||
        policy.maximum_slippage_bps < 0 || policy.maximum_slippage_bps > bps_scale ||
        !policy.maximum_venues || policy.maximum_venues > 16 ||
        (policy.side != OrderSide::buy && policy.side != OrderSide::sell) ||
        (policy.order_type != OrderType::limit && policy.order_type != OrderType::immediate_or_cancel &&
         policy.order_type != OrderType::fill_or_kill)) {
        return failure<SplitPlan>(Error::invalid);
    }
    bool found = false;
    DepthQuote chosen{};
    const VenueDepth* selected = nullptr;
    for (std::size_t i = 0; i < venues.size(); ++i) {
        const auto& venue = venues[i];
        for (std::size_t j = 0; j < i; ++j) {
            if (venues[j].venue == venue.venue) {
                return failure<SplitPlan>(Error::conflict);
            }
        }
        if (venue.instrument != policy.instrument || venue.base_asset != policy.base_asset ||
            venue.quote_asset != policy.quote_asset || venue.side != policy.side) {
            return failure<SplitPlan>(Error::domain);
        }
        const auto quote = depth_quote(venue, policy.quantity, policy.limit_price,
            policy.order_type == OrderType::immediate_or_cancel);
        if (!quote) {
            if (quote.error == Error::overflow || quote.error == Error::invalid) {
                return failure<SplitPlan>(quote.error);
            }
            continue;
        }
        const auto adverse = policy.side == OrderSide::buy
            ? std::max<std::int64_t>(0, quote.value.worst_price - policy.reference_price)
            : std::max<std::int64_t>(0, policy.reference_price - quote.value.worst_price);
        const auto slippage = divide_wide(static_cast<Wide>(adverse) * bps_scale,
            policy.reference_price, Rounding::ceil);
        if (!slippage) {
            return failure<SplitPlan>(slippage.error);
        }
        if (slippage.value > policy.maximum_slippage_bps ||
            (policy.side == OrderSide::buy && quote.value.total_cash_flow > policy.quote_budget)) {
            continue;
        }
        // Prefer filled quantity before price for IOC; avoid a cheap one-lot
        // quote displacing a route that satisfies the requested quantity.
        const bool quantity_better = !found || quote.value.filled_quantity > chosen.filled_quantity;
        const bool equal_quantity = found && quote.value.filled_quantity == chosen.filled_quantity;
        const bool flow_better = policy.side == OrderSide::buy
            ? quote.value.total_cash_flow < chosen.total_cash_flow
            : quote.value.total_cash_flow > chosen.total_cash_flow;
        const bool tied = quote.value.total_cash_flow == chosen.total_cash_flow;
        if (quantity_better || (equal_quantity && (flow_better || (tied && venue.venue < chosen.venue)))) {
            found = true;
            chosen = quote.value;
            selected = &venue;
        }
    }
    if (!found || !selected) {
        return failure<SplitPlan>(Error::infeasible);
    }
    SplitPlan plan{};
    plan.count = 1;
    plan.requested_quantity = policy.quantity;
    plan.filled_quantity = chosen.filled_quantity;
    plan.unfilled_quantity = policy.quantity - chosen.filled_quantity;
    plan.total_notional = chosen.notional;
    plan.total_fees = chosen.fee;
    plan.fixed_costs = selected->fixed_cost;
    plan.cash_flow = chosen.total_cash_flow;
    plan.valid_until_ns = chosen.valid_until_ns;
    plan.children[0] = {chosen.venue, chosen.filled_quantity, chosen.worst_price, chosen.notional,
        chosen.fee, selected->fixed_cost, chosen.total_cash_flow, selected->envelope.sequence, chosen.valid_until_ns};
    return success(plan);
}

struct SplitCandidate {
    std::uint64_t id{};
    std::array<std::int64_t, 16> quantities{};
};

struct SplitComparison {
    std::uint64_t candidate_id{};
    SplitPlan plan{};
    std::uint32_t examined{};
    std::uint32_t rejected{};
};

[[nodiscard]] inline Result<SplitPlan> evaluate_split(
    std::span<const VenueDepth> venues,
    const RoutePolicy& policy,
    const SplitCandidate& candidate
) noexcept {
    if (!candidate.id || venues.empty() || venues.size() > 16 || !policy.maximum_venues ||
        policy.maximum_venues > 16 || policy.quantity <= 0 || policy.limit_price <= 0 ||
        policy.reference_price <= 0 || policy.maximum_slippage_bps < 0 ||
        policy.maximum_slippage_bps > bps_scale || policy.quote_budget < 0 ||
        (policy.side != OrderSide::buy && policy.side != OrderSide::sell) ||
        (policy.order_type != OrderType::limit && policy.order_type != OrderType::immediate_or_cancel &&
         policy.order_type != OrderType::fill_or_kill)) {
        return failure<SplitPlan>(Error::invalid);
    }
    SplitPlan plan{};
    plan.requested_quantity = policy.quantity;
    plan.valid_until_ns = UINT64_MAX;
    for (std::size_t i = venues.size(); i < candidate.quantities.size(); ++i) {
        if (candidate.quantities[i]) {
            return failure<SplitPlan>(Error::domain);
        }
    }
    for (std::size_t i = 0; i < venues.size(); ++i) {
        const auto quantity = candidate.quantities[i];
        if (quantity < 0) {
            return failure<SplitPlan>(Error::invalid);
        }
        if (!quantity) {
            continue;
        }
        const auto& venue = venues[i];
        if (venue.instrument != policy.instrument || venue.base_asset != policy.base_asset ||
            venue.quote_asset != policy.quote_asset || venue.side != policy.side) {
            return failure<SplitPlan>(Error::domain);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (candidate.quantities[j] && venues[j].venue == venue.venue) {
                return failure<SplitPlan>(Error::conflict);
            }
        }
        if (plan.count == policy.maximum_venues) {
            return failure<SplitPlan>(Error::capacity);
        }
        const auto quote = depth_quote(venue, quantity, policy.limit_price, false);
        if (!quote) {
            return failure<SplitPlan>(quote.error);
        }
        const auto adverse = policy.side == OrderSide::buy
            ? std::max<std::int64_t>(0, quote.value.worst_price - policy.reference_price)
            : std::max<std::int64_t>(0, policy.reference_price - quote.value.worst_price);
        const auto slippage = divide_wide(static_cast<Wide>(adverse) * bps_scale,
            policy.reference_price, Rounding::ceil);
        if (!slippage) {
            return failure<SplitPlan>(slippage.error);
        }
        if (slippage.value > policy.maximum_slippage_bps) {
            return failure<SplitPlan>(Error::cost);
        }
        const auto total_quantity = add(plan.filled_quantity, quote.value.filled_quantity);
        const auto total_notional = add(plan.total_notional, quote.value.notional);
        const auto total_fee = add(plan.total_fees, quote.value.fee);
        const auto total_fixed = add(plan.fixed_costs, venue.fixed_cost);
        const auto total_flow = add(plan.cash_flow, quote.value.total_cash_flow);
        if (!total_quantity || !total_notional || !total_fee || !total_fixed || !total_flow) {
            return failure<SplitPlan>(Error::overflow);
        }
        plan.filled_quantity = total_quantity.value;
        plan.total_notional = total_notional.value;
        plan.total_fees = total_fee.value;
        plan.fixed_costs = total_fixed.value;
        plan.cash_flow = total_flow.value;
        plan.valid_until_ns = std::min(plan.valid_until_ns, quote.value.valid_until_ns);
        plan.children[plan.count++] = {venue.venue, quantity, quote.value.worst_price, quote.value.notional,
            quote.value.fee, venue.fixed_cost, quote.value.total_cash_flow, venue.envelope.sequence, quote.value.valid_until_ns};
    }
    if (!plan.count || plan.filled_quantity > policy.quantity ||
        (plan.filled_quantity != policy.quantity && policy.order_type != OrderType::immediate_or_cancel)) {
        return failure<SplitPlan>(Error::infeasible);
    }
    if (policy.side == OrderSide::buy && plan.cash_flow > policy.quote_budget) {
        return failure<SplitPlan>(Error::cost);
    }
    plan.unfilled_quantity = policy.quantity - plan.filled_quantity;
    return success(plan);
}

[[nodiscard]] inline Result<SplitComparison> compare_splits(
    std::span<const VenueDepth> venues,
    const RoutePolicy& policy,
    std::span<const SplitCandidate> candidates
) noexcept {
    if (candidates.empty() || candidates.size() > 1024) {
        return failure<SplitComparison>(Error::capacity);
    }
    SplitComparison result{};
    bool found = false;
    for (std::size_t i = 0; i < candidates.size(); ++i) {
        for (std::size_t j = 0; j < i; ++j) {
            if (candidates[j].id == candidates[i].id) {
                return failure<SplitComparison>(Error::conflict);
            }
        }
        ++result.examined;
        const auto plan = evaluate_split(venues, policy, candidates[i]);
        if (!plan) {
            if (plan.error == Error::invalid || plan.error == Error::overflow || plan.error == Error::domain) {
                return failure<SplitComparison>(plan.error);
            }
            ++result.rejected;
            continue;
        }
        const bool more_filled = !found || plan.value.filled_quantity > result.plan.filled_quantity;
        const bool same_fill = found && plan.value.filled_quantity == result.plan.filled_quantity;
        const bool better_cost = policy.side == OrderSide::buy ? plan.value.cash_flow < result.plan.cash_flow
            : plan.value.cash_flow > result.plan.cash_flow;
        const bool tied_cost = plan.value.cash_flow == result.plan.cash_flow;
        if (more_filled || (same_fill && (better_cost || (tied_cost && candidates[i].id < result.candidate_id)))) {
            result.plan = plan.value;
            result.candidate_id = candidates[i].id;
            found = true;
        }
    }
    if (!found) {
        return failure<SplitComparison>(Error::infeasible);
    }
    return success(result);
}

} // namespace machine::economics
