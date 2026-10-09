#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

struct WeightedScenario {
    std::uint64_t id{};
    std::int64_t loss{};
    std::uint32_t probability_bps{};
};

struct TailRisk {
    std::int64_t expected_loss{};
    std::int64_t value_at_risk{};
    std::int64_t conditional_value_at_risk{};
    std::int64_t maximum_loss{};
    std::int64_t minimum_loss{};
    std::uint32_t confidence_bps{};
    std::uint32_t tail_probability_bps{};
    std::uint32_t tail_scenarios{};
};

// Exact discrete distribution under caller-supplied scenario probabilities.
// Probabilities must sum to one. Expected shortfall consumes exactly the top
// (1-confidence) probability mass, splitting the boundary scenario as needed.
// It is neither a calibrated forecasting model nor a normal-distribution VaR.
[[nodiscard]] inline Result<TailRisk> scenario_tail_risk(
    std::span<const WeightedScenario> scenarios,
    std::uint32_t confidence_bps
) noexcept {
    if (scenarios.empty() || scenarios.size() > 256 || !confidence_bps || confidence_bps >= bps_scale) {
        return failure<TailRisk>(Error::invalid);
    }
    std::array<WeightedScenario, 256> sorted{};
    std::uint32_t total_probability = 0;
    Wide expected = 0;
    for (std::size_t i = 0; i < scenarios.size(); ++i) {
        const auto& scenario = scenarios[i];
        if (!scenario.id || !scenario.probability_bps || scenario.probability_bps > bps_scale) {
            return failure<TailRisk>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (scenarios[j].id == scenario.id) {
                return failure<TailRisk>(Error::conflict);
            }
        }
        total_probability += scenario.probability_bps;
        expected += static_cast<Wide>(scenario.loss) * scenario.probability_bps;
        sorted[i] = scenario;
    }
    if (total_probability != bps_scale) {
        return failure<TailRisk>(Error::domain);
    }
    std::sort(sorted.begin(), sorted.begin() + scenarios.size(), [](const auto& a, const auto& b) {
        return a.loss < b.loss || (a.loss == b.loss && a.id < b.id);
    });
    TailRisk result{};
    result.confidence_bps = confidence_bps;
    result.tail_probability_bps = bps_scale - confidence_bps;
    result.minimum_loss = sorted[0].loss;
    result.maximum_loss = sorted[scenarios.size() - 1].loss;
    const auto mean = divide_wide(expected, bps_scale, Rounding::ceil);
    if (!mean) {
        return failure<TailRisk>(mean.error);
    }
    result.expected_loss = mean.value;
    std::uint32_t cumulative = 0;
    for (std::size_t i = 0; i < scenarios.size(); ++i) {
        cumulative += sorted[i].probability_bps;
        if (cumulative >= confidence_bps) {
            result.value_at_risk = sorted[i].loss;
            break;
        }
    }
    auto remaining = result.tail_probability_bps;
    Wide tail_loss = 0;
    for (auto i = scenarios.size(); i > 0 && remaining; --i) {
        const auto& scenario = sorted[i - 1];
        const auto take = std::min(remaining, scenario.probability_bps);
        tail_loss += static_cast<Wide>(scenario.loss) * take;
        remaining -= take;
        ++result.tail_scenarios;
    }
    if (remaining) {
        return failure<TailRisk>(Error::incomplete);
    }
    const auto conditional = divide_wide(tail_loss, result.tail_probability_bps, Rounding::ceil);
    if (!conditional) {
        return failure<TailRisk>(conditional.error);
    }
    result.conditional_value_at_risk = conditional.value;
    return success(result);
}

[[nodiscard]] inline std::uint64_t integer_sqrt(std::uint64_t value) noexcept {
    std::uint64_t result = 0;
    std::uint64_t bit = 1ULL << 62;
    while (bit > value) {
        bit >>= 2;
    }
    while (bit) {
        if (value >= result + bit) {
            value -= result + bit;
            result = (result >> 1) + bit;
        } else {
            result >>= 1;
        }
        bit >>= 2;
    }
    return result;
}

struct PriceSample {
    std::uint64_t timestamp_ns{};
    std::int64_t price{};
};

struct ReturnStatistics {
    std::int64_t total_return{};
    std::int64_t average_simple_return{};
    std::int64_t variance{};
    std::int64_t standard_deviation{};
    std::int64_t maximum_drawdown{};
    std::int64_t minimum_price{};
    std::int64_t maximum_price{};
    std::int64_t time_weighted_price{};
    std::uint64_t duration_ns{};
    std::uint32_t intervals{};
};

// Population moments of consecutive simple returns. No annualization, log
// approximation, resampling or treatment of missing prices as zero is hidden
// here. The input must have a fixed sampling cadence within the caller's skew.
[[nodiscard]] inline Result<ReturnStatistics> return_statistics(
    std::span<const PriceSample> samples,
    std::uint64_t expected_interval_ns,
    std::uint64_t interval_tolerance_ns
) noexcept {
    if (samples.size() < 2 || samples.size() > 256 || !expected_interval_ns ||
        interval_tolerance_ns >= expected_interval_ns) {
        return failure<ReturnStatistics>(Error::invalid);
    }
    ReturnStatistics result{};
    result.minimum_price = INT64_MAX;
    result.maximum_price = 0;
    std::array<std::int64_t, 255> returns{};
    Wide sum = 0;
    Wide weighted_price = 0;
    std::int64_t peak = 0;
    std::uint64_t total_time = 0;
    for (std::size_t i = 0; i < samples.size(); ++i) {
        const auto& sample = samples[i];
        if (!sample.timestamp_ns || sample.price <= 0) {
            return failure<ReturnStatistics>(Error::invalid);
        }
        result.minimum_price = std::min(result.minimum_price, sample.price);
        result.maximum_price = std::max(result.maximum_price, sample.price);
        peak = std::max(peak, sample.price);
        const auto drawdown = ratio(peak - sample.price, peak, Rounding::ceil);
        if (!drawdown) {
            return failure<ReturnStatistics>(drawdown.error);
        }
        result.maximum_drawdown = std::max(result.maximum_drawdown, drawdown.value);
        if (!i) {
            continue;
        }
        const auto& previous = samples[i - 1];
        if (sample.timestamp_ns <= previous.timestamp_ns) {
            return failure<ReturnStatistics>(Error::sequence);
        }
        const auto elapsed = sample.timestamp_ns - previous.timestamp_ns;
        if (elapsed < expected_interval_ns - interval_tolerance_ns ||
            elapsed - (elapsed >= expected_interval_ns ? expected_interval_ns : elapsed) > interval_tolerance_ns) {
            return failure<ReturnStatistics>(Error::stale);
        }
        if (elapsed > UINT64_MAX - total_time) {
            return failure<ReturnStatistics>(Error::overflow);
        }
        total_time += elapsed;
        // Keep the time-weighted accumulator below signed 128-bit capacity.
        const auto term = static_cast<Wide>(previous.price) * elapsed;
        constexpr auto wide_max = (static_cast<unsigned __int128>(1) << 127) - 1;
        if (static_cast<unsigned __int128>(term) > wide_max - static_cast<unsigned __int128>(weighted_price)) {
            return failure<ReturnStatistics>(Error::overflow);
        }
        weighted_price += term;
        const auto change = ratio(sample.price - previous.price, previous.price, Rounding::toward_zero);
        if (!change) {
            return failure<ReturnStatistics>(change.error);
        }
        // Financial price ratios larger than 1000x are outside this sample
        // domain; this cap bounds later squared moments independently.
        if (change.value < -scale || change.value > 1000 * scale) {
            return failure<ReturnStatistics>(Error::domain);
        }
        returns[i - 1] = change.value;
        sum += change.value;
    }
    const auto count = static_cast<std::int64_t>(samples.size() - 1);
    const auto mean = divide_wide(sum, count, Rounding::toward_zero);
    if (!mean) {
        return failure<ReturnStatistics>(mean.error);
    }
    Wide squares = 0;
    for (std::int64_t i = 0; i < count; ++i) {
        const auto difference = static_cast<Wide>(returns[i]) - mean.value;
        squares += difference * difference;
    }
    const auto variance = divide_wide(squares, count * scale, Rounding::ceil);
    const auto total_return = ratio(samples.back().price - samples.front().price,
        samples.front().price, Rounding::toward_zero);
    if (!variance || !total_return || total_time > INT64_MAX) {
        return failure<ReturnStatistics>(Error::overflow);
    }
    const auto twap = divide_wide(weighted_price, static_cast<std::int64_t>(total_time), Rounding::floor);
    if (!twap) {
        return failure<ReturnStatistics>(twap.error);
    }
    const auto squared_deviation = static_cast<Wide>(variance.value) * scale;
    if (squared_deviation > UINT64_MAX) {
        return failure<ReturnStatistics>(Error::overflow);
    }
    const auto root = integer_sqrt(static_cast<std::uint64_t>(squared_deviation));
    // Risk deviation rounds upward to avoid underestimating dispersion.
    result.standard_deviation = static_cast<std::int64_t>(root +
        (static_cast<Wide>(root) * root < squared_deviation ? 1 : 0));
    result.variance = variance.value;
    result.total_return = total_return.value;
    result.average_simple_return = mean.value;
    result.time_weighted_price = twap.value;
    result.duration_ns = total_time;
    result.intervals = static_cast<std::uint32_t>(count);
    return success(result);
}

struct FactorExposure {
    std::uint64_t asset{};
    std::int64_t signed_value{};
    std::array<std::int64_t, 16> loadings{};
};

struct FactorBound {
    std::uint64_t factor{};
    std::int64_t maximum_gross{};
    std::int64_t minimum_net{};
    std::int64_t maximum_net{};
};

struct FactorRisk {
    std::array<std::int64_t, 16> gross{};
    std::array<std::int64_t, 16> net{};
    std::uint32_t count{};
    std::uint32_t violated{};
};

[[nodiscard]] inline Result<FactorRisk> factor_risk(
    std::span<const FactorExposure> assets,
    std::span<const FactorBound> bounds
) noexcept {
    if (assets.size() > 32 || bounds.empty() || bounds.size() > 16) {
        return failure<FactorRisk>(Error::invalid);
    }
    FactorRisk result{};
    result.count = static_cast<std::uint32_t>(bounds.size());
    for (std::size_t i = 0; i < bounds.size(); ++i) {
        if (!bounds[i].factor || bounds[i].maximum_gross < 0 ||
            bounds[i].minimum_net > bounds[i].maximum_net) {
            return failure<FactorRisk>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (bounds[j].factor == bounds[i].factor) {
                return failure<FactorRisk>(Error::conflict);
            }
        }
    }
    for (std::size_t i = 0; i < assets.size(); ++i) {
        if (!assets[i].asset) {
            return failure<FactorRisk>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (assets[i].asset == assets[j].asset) {
                return failure<FactorRisk>(Error::conflict);
            }
        }
        for (std::size_t factor = 0; factor < bounds.size(); ++factor) {
            const auto loading = assets[i].loadings[factor];
            if (loading < -10 * scale || loading > 10 * scale) {
                return failure<FactorRisk>(Error::invalid);
            }
            const auto exposure = multiply(assets[i].signed_value, loading, Rounding::away_zero);
            if (!exposure) {
                return failure<FactorRisk>(exposure.error);
            }
            const auto magnitude = absolute(exposure.value);
            if (!magnitude) {
                return failure<FactorRisk>(magnitude.error);
            }
            const auto net = add(result.net[factor], exposure.value);
            const auto gross = add(result.gross[factor], magnitude.value);
            if (!net || !gross) {
                return failure<FactorRisk>(Error::overflow);
            }
            result.net[factor] = net.value;
            result.gross[factor] = gross.value;
        }
    }
    for (std::size_t factor = 0; factor < bounds.size(); ++factor) {
        if (result.gross[factor] > bounds[factor].maximum_gross ||
            result.net[factor] < bounds[factor].minimum_net || result.net[factor] > bounds[factor].maximum_net) {
            result.violated |= 1U << factor;
        }
    }
    return success(result);
}

} // namespace machine::economics
