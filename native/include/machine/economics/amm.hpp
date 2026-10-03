#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

struct ConstantProductPool {
    std::uint64_t pool_id{};
    std::uint64_t asset_in{};
    std::uint64_t asset_out{};
    std::int64_t reserve_in{};
    std::int64_t reserve_out{};
    std::int64_t fee_rate{};
    std::int64_t fixed_cost{};
    Envelope envelope{};
};

struct SwapQuote {
    std::uint64_t pool_id{};
    std::uint64_t asset_in{};
    std::uint64_t asset_out{};
    std::int64_t amount_in{};
    std::int64_t amount_out{};
    std::int64_t fee_in{};
    std::int64_t price_impact_bps{};
    std::int64_t effective_price{};
    std::int64_t fixed_cost{};
    std::int64_t reserve_in_after{};
    std::int64_t reserve_out_after{};
    std::uint64_t valid_until_ns{};
};

[[nodiscard]] inline Error validate_pool(const ConstantProductPool& pool) noexcept {
    if (!pool.pool_id || !pool.asset_in || !pool.asset_out || pool.asset_in == pool.asset_out ||
        pool.reserve_in <= 0 || pool.reserve_out <= 0 || pool.fee_rate < 0 || pool.fee_rate >= scale ||
        pool.fixed_cost < 0) {
        return Error::invalid;
    }
    return validate(pool.envelope);
}

// Fees stay in the pool's input reserve. Taxed/rebasing tokens require a
// different primitive and must not be normalized into this constant-product
// model. Every swap quote is a simulation, not an executable transaction.
[[nodiscard]] inline Result<SwapQuote> exact_input_swap(
    const ConstantProductPool& pool,
    std::int64_t amount_in
) noexcept {
    const auto valid = validate_pool(pool);
    if (valid != Error::okay) {
        return failure<SwapQuote>(valid);
    }
    if (amount_in <= 0) {
        return failure<SwapQuote>(Error::invalid);
    }
    const auto fee = multiply(amount_in, pool.fee_rate, Rounding::ceil);
    if (!fee) {
        return failure<SwapQuote>(fee.error);
    }
    const auto net = amount_in - fee.value;
    if (net <= 0) {
        return failure<SwapQuote>(Error::illiquid);
    }
    const auto denominator = add(pool.reserve_in, net);
    const auto input_after = add(pool.reserve_in, amount_in);
    if (!denominator || !input_after) {
        return failure<SwapQuote>(Error::overflow);
    }
    const auto output = divide_wide(
        static_cast<Wide>(pool.reserve_out) * net,
        denominator.value,
        Rounding::floor
    );
    if (!output) {
        return failure<SwapQuote>(output.error);
    }
    if (!output.value || output.value >= pool.reserve_out) {
        return failure<SwapQuote>(Error::illiquid);
    }
    const auto spot_output = divide_wide(
        static_cast<Wide>(pool.reserve_out) * amount_in,
        pool.reserve_in,
        Rounding::floor
    );
    if (!spot_output || !spot_output.value) {
        return failure<SwapQuote>(spot_output ? Error::illiquid : spot_output.error);
    }
    if (output.value > spot_output.value) {
        return failure<SwapQuote>(Error::invalid);
    }
    const auto impact = divide_wide(
        static_cast<Wide>(spot_output.value - output.value) * bps_scale,
        spot_output.value,
        Rounding::ceil
    );
    const auto price = ratio(amount_in, output.value, Rounding::ceil);
    const auto until = expiry(pool.envelope);
    if (!impact || !price || !until) {
        return failure<SwapQuote>(Error::overflow);
    }
    const auto output_after = pool.reserve_out - output.value;
    if (static_cast<Wide>(input_after.value) * output_after <
        static_cast<Wide>(pool.reserve_in) * pool.reserve_out) {
        return failure<SwapQuote>(Error::invalid);
    }
    return success(SwapQuote{pool.pool_id, pool.asset_in, pool.asset_out, amount_in, output.value,
        fee.value, impact.value, price.value, pool.fixed_cost, input_after.value, output_after, until.value});
}

[[nodiscard]] inline Result<SwapQuote> exact_output_swap(
    const ConstantProductPool& pool,
    std::int64_t amount_out
) noexcept {
    const auto valid = validate_pool(pool);
    if (valid != Error::okay) {
        return failure<SwapQuote>(valid);
    }
    if (amount_out <= 0 || amount_out >= pool.reserve_out) {
        return failure<SwapQuote>(Error::illiquid);
    }
    const auto net = divide_wide(
        static_cast<Wide>(pool.reserve_in) * amount_out,
        pool.reserve_out - amount_out,
        Rounding::ceil
    );
    if (!net) {
        return failure<SwapQuote>(net.error);
    }
    const auto gross = divide_wide(
        static_cast<Wide>(net.value) * scale,
        scale - pool.fee_rate,
        Rounding::ceil
    );
    if (!gross) {
        return failure<SwapQuote>(gross.error);
    }
    // The input fee's upward rounding can require one more micro-unit.
    auto quote = exact_input_swap(pool, gross.value);
    if (quote && quote.value.amount_out < amount_out) {
        const auto adjusted = add(gross.value, 1);
        if (!adjusted) {
            return failure<SwapQuote>(adjusted.error);
        }
        quote = exact_input_swap(pool, adjusted.value);
    }
    if (!quote) {
        return quote;
    }
    if (quote.value.amount_out < amount_out) {
        return failure<SwapQuote>(Error::infeasible);
    }
    // Exact output leaves any conservative surplus in the reserve.
    quote.value.amount_out = amount_out;
    quote.value.reserve_out_after = pool.reserve_out - amount_out;
    const auto price = ratio(quote.value.amount_in, amount_out, Rounding::ceil);
    const auto spot = divide_wide(
        static_cast<Wide>(pool.reserve_out) * quote.value.amount_in,
        pool.reserve_in,
        Rounding::floor
    );
    if (!price || !spot || spot.value < amount_out) {
        return failure<SwapQuote>(Error::overflow);
    }
    const auto impact = divide_wide(
        static_cast<Wide>(spot.value - amount_out) * bps_scale,
        spot.value,
        Rounding::ceil
    );
    if (!impact) {
        return failure<SwapQuote>(impact.error);
    }
    quote.value.effective_price = price.value;
    quote.value.price_impact_bps = impact.value;
    return quote;
}

struct SwapRoute {
    std::array<ConstantProductPool, 4> pools{};
    std::uint32_t count{};
    std::int64_t amount_in{};
    std::int64_t minimum_output{};
    std::int64_t max_cost{};
    std::int64_t max_impact_bps{};
};

struct RouteQuote {
    std::array<SwapQuote, 4> hops{};
    std::uint32_t count{};
    std::int64_t amount_in{};
    std::int64_t amount_out{};
    std::int64_t fixed_cost{};
    std::uint64_t valid_until_ns{};
};

[[nodiscard]] inline Result<RouteQuote> simulate_route(const SwapRoute& route) noexcept {
    if (!route.count || route.count > route.pools.size() || route.amount_in <= 0 ||
        route.minimum_output < 0 || route.max_cost < 0 || route.max_impact_bps < 0 ||
        route.max_impact_bps > bps_scale) {
        return failure<RouteQuote>(Error::invalid);
    }
    RouteQuote result{};
    result.count = route.count;
    result.amount_in = route.amount_in;
    result.valid_until_ns = UINT64_MAX;
    auto amount = route.amount_in;
    for (std::size_t i = 0; i < route.count; ++i) {
        const auto& pool = route.pools[i];
        if (i && route.pools[i - 1].asset_out != pool.asset_in) {
            return failure<RouteQuote>(Error::domain);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (route.pools[j].pool_id == pool.pool_id) {
                // Reusing a pool needs evolving reserves, not independent
                // snapshots; reject instead of overstating route liquidity.
                return failure<RouteQuote>(Error::conflict);
            }
        }
        const auto quote = exact_input_swap(pool, amount);
        if (!quote) {
            return failure<RouteQuote>(quote.error);
        }
        if (quote.value.price_impact_bps > route.max_impact_bps) {
            return failure<RouteQuote>(Error::cost);
        }
        const auto cost = add(result.fixed_cost, quote.value.fixed_cost);
        if (!cost) {
            return failure<RouteQuote>(cost.error);
        }
        if (cost.value > route.max_cost) {
            return failure<RouteQuote>(Error::cost);
        }
        result.fixed_cost = cost.value;
        result.valid_until_ns = std::min(result.valid_until_ns, quote.value.valid_until_ns);
        result.hops[i] = quote.value;
        amount = quote.value.amount_out;
    }
    if (amount < route.minimum_output) {
        return failure<RouteQuote>(Error::infeasible);
    }
    result.amount_out = amount;
    return success(result);
}

struct LiquidityShare {
    std::int64_t shares{};
    std::int64_t amount_a{};
    std::int64_t amount_b{};
};

[[nodiscard]] inline Result<LiquidityShare> redeem_liquidity(
    std::int64_t owned_shares,
    std::int64_t total_shares,
    std::int64_t reserve_a,
    std::int64_t reserve_b
) noexcept {
    if (owned_shares < 0 || total_shares <= 0 || owned_shares > total_shares ||
        reserve_a < 0 || reserve_b < 0) {
        return failure<LiquidityShare>(Error::invalid);
    }
    const auto a = divide_wide(static_cast<Wide>(owned_shares) * reserve_a, total_shares, Rounding::floor);
    const auto b = divide_wide(static_cast<Wide>(owned_shares) * reserve_b, total_shares, Rounding::floor);
    if (!a || !b) {
        return failure<LiquidityShare>(Error::overflow);
    }
    return success(LiquidityShare{owned_shares, a.value, b.value});
}

} // namespace machine::economics
