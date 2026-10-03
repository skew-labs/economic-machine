#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

struct OracleObservation {
    std::uint64_t source{};
    std::uint64_t instrument{};
    std::uint64_t sequence{};
    std::uint64_t observed_ns{};
    std::int64_t price{};
    std::int64_t confidence_radius{};
    bool adapter_verified{};
};

struct OraclePolicy {
    std::uint64_t instrument{};
    std::uint64_t now_ns{};
    std::uint64_t max_age_ns{};
    std::uint64_t max_skew_ns{};
    std::uint32_t min_sources{};
    std::int64_t max_dispersion_bps{};
    std::int64_t max_confidence_bps{};
    std::array<std::uint64_t, 16> allowed_sources{};
    std::uint32_t source_count{};
};

struct OraclePrice {
    std::int64_t median{};
    std::int64_t low{};
    std::int64_t high{};
    std::int64_t dispersion_bps{};
    std::uint32_t sources{};
    std::uint64_t oldest_ns{};
    std::uint64_t newest_ns{};
    std::uint64_t valid_until_ns{};
    std::uint64_t diagnostic_fingerprint{};
};

// The adapter_verified flag is an input assertion by the trusted boundary.
// This function verifies coherence/freshness; it does not verify signatures.
[[nodiscard]] inline Result<OraclePrice> aggregate_price(
    std::span<const OracleObservation> observations,
    const OraclePolicy& policy
) noexcept {
    if (!policy.instrument || !policy.max_age_ns || !policy.min_sources ||
        policy.source_count == 0 || policy.source_count > policy.allowed_sources.size() ||
        policy.min_sources > policy.source_count || policy.max_skew_ns > policy.max_age_ns ||
        policy.max_dispersion_bps < 0 || policy.max_dispersion_bps > bps_scale ||
        policy.max_confidence_bps < 0 || policy.max_confidence_bps > bps_scale ||
        observations.empty() || observations.size() > 16) {
        return failure<OraclePrice>(Error::invalid);
    }
    for (std::size_t i = 0; i < policy.source_count; ++i) {
        if (!policy.allowed_sources[i]) {
            return failure<OraclePrice>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (policy.allowed_sources[j] == policy.allowed_sources[i]) {
                return failure<OraclePrice>(Error::conflict);
            }
        }
    }
    std::array<OracleObservation, 16> retained{};
    std::size_t count = 0;
    for (const auto& observation : observations) {
        bool allowed = false;
        for (std::size_t i = 0; i < policy.source_count; ++i) {
            allowed |= observation.source == policy.allowed_sources[i];
        }
        if (!allowed || observation.instrument != policy.instrument ||
            !observation.source || !observation.sequence || observation.price <= 0 ||
            observation.confidence_radius < 0 || !observation.adapter_verified) {
            return failure<OraclePrice>(Error::invalid);
        }
        for (std::size_t i = 0; i < count; ++i) {
            if (retained[i].source == observation.source) {
                return failure<OraclePrice>(Error::conflict);
            }
        }
        if (observation.observed_ns > policy.now_ns) {
            return failure<OraclePrice>(Error::future);
        }
        if (policy.now_ns - observation.observed_ns > policy.max_age_ns) {
            return failure<OraclePrice>(Error::stale);
        }
        const auto radius_bps = divide_wide(
            static_cast<Wide>(observation.confidence_radius) * bps_scale,
            observation.price,
            Rounding::ceil
        );
        if (!radius_bps) {
            return failure<OraclePrice>(radius_bps.error);
        }
        if (radius_bps.value > policy.max_confidence_bps ||
            observation.confidence_radius >= observation.price) {
            return failure<OraclePrice>(Error::dispersion);
        }
        retained[count++] = observation;
    }
    if (count < policy.min_sources) {
        return failure<OraclePrice>(Error::quorum);
    }
    std::sort(retained.begin(), retained.begin() + count,
        [](const auto& a, const auto& b) {
            return a.price < b.price || (a.price == b.price && a.source < b.source);
        });
    OraclePrice price{};
    price.sources = static_cast<std::uint32_t>(count);
    price.median = retained[count / 2].price;
    if (count % 2 == 0) {
        const auto middle = divide_wide(
            static_cast<Wide>(retained[count / 2 - 1].price) + retained[count / 2].price,
            2,
            Rounding::floor
        );
        if (!middle) {
            return failure<OraclePrice>(middle.error);
        }
        price.median = middle.value;
    }
    price.oldest_ns = UINT64_MAX;
    price.newest_ns = 0;
    price.low = INT64_MAX;
    price.high = 0;
    Fingerprint fingerprint;
    fingerprint.append(policy.instrument);
    // Sort by source for a stable identity independent of packet arrival order.
    std::sort(retained.begin(), retained.begin() + count,
        [](const auto& a, const auto& b) { return a.source < b.source; });
    for (std::size_t i = 0; i < count; ++i) {
        const auto& observation = retained[i];
        const auto high = add(observation.price, observation.confidence_radius);
        if (!high) {
            return failure<OraclePrice>(high.error);
        }
        price.low = std::min(price.low, observation.price - observation.confidence_radius);
        price.high = std::max(price.high, high.value);
        price.oldest_ns = std::min(price.oldest_ns, observation.observed_ns);
        price.newest_ns = std::max(price.newest_ns, observation.observed_ns);
        fingerprint.append(observation.source);
        fingerprint.append(observation.sequence);
        fingerprint.append(observation.observed_ns);
        fingerprint.append(static_cast<std::uint64_t>(observation.price));
        fingerprint.append(static_cast<std::uint64_t>(observation.confidence_radius));
    }
    if (price.newest_ns - price.oldest_ns > policy.max_skew_ns) {
        return failure<OraclePrice>(Error::stale);
    }
    const auto dispersion = divide_wide(
        static_cast<Wide>(price.high - price.low) * bps_scale,
        price.median,
        Rounding::ceil
    );
    if (!dispersion) {
        return failure<OraclePrice>(dispersion.error);
    }
    if (dispersion.value > policy.max_dispersion_bps) {
        return failure<OraclePrice>(Error::dispersion);
    }
    const auto until = add_time(price.oldest_ns, policy.max_age_ns);
    if (!until) {
        return failure<OraclePrice>(until.error);
    }
    price.dispersion_bps = dispersion.value;
    price.valid_until_ns = until.value;
    price.diagnostic_fingerprint = fingerprint.value();
    return success(price);
}

template <std::size_t Capacity>
class OracleCache {
    std::array<OracleObservation, Capacity> values_{};

public:
    [[nodiscard]] Error update(const OracleObservation& observation) noexcept {
        if (!observation.source || !observation.instrument || !observation.sequence ||
            observation.price <= 0 || observation.confidence_radius < 0 || !observation.adapter_verified) {
            return Error::invalid;
        }
        OracleObservation* empty = nullptr;
        for (auto& value : values_) {
            if (!value.source) {
                if (!empty) {
                    empty = &value;
                }
                continue;
            }
            if (value.source != observation.source || value.instrument != observation.instrument) {
                continue;
            }
            if (observation.sequence < value.sequence || observation.observed_ns < value.observed_ns) {
                return Error::sequence;
            }
            if (observation.sequence == value.sequence) {
                return observation.price == value.price &&
                    observation.confidence_radius == value.confidence_radius &&
                    observation.observed_ns == value.observed_ns
                    ? Error::okay : Error::conflict;
            }
            value = observation;
            return Error::okay;
        }
        if (!empty) {
            return Error::capacity;
        }
        *empty = observation;
        return Error::okay;
    }

    [[nodiscard]] Result<Bounded<OracleObservation, Capacity>> for_instrument(
        std::uint64_t instrument
    ) const noexcept {
        if (!instrument) {
            return failure<Bounded<OracleObservation, Capacity>>(Error::invalid);
        }
        Bounded<OracleObservation, Capacity> result;
        for (const auto& value : values_) {
            if (value.instrument == instrument && value.source) {
                if (!result.push(value)) {
                    return failure<Bounded<OracleObservation, Capacity>>(Error::capacity);
                }
            }
        }
        if (!result.size()) {
            return failure<Bounded<OracleObservation, Capacity>>(Error::missing);
        }
        return success(result);
    }
};

} // namespace machine::economics
