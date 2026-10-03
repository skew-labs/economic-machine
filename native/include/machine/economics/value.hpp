#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <type_traits>

namespace machine::economics {

// A single scale applies to quantities, prices, ratios and monetary values.
// External decimal assets are normalized at the boundary, never by floating
// point. Timestamps are monotonic nanoseconds supplied by the caller.
inline constexpr std::int64_t scale = 1'000'000;
inline constexpr std::int64_t bps_scale = 10'000;
using Wide = __int128;

enum class Error : std::uint32_t {
    okay,
    invalid,
    overflow,
    divide_zero,
    capacity,
    missing,
    stale,
    future,
    sequence,
    conflict,
    unsupported,
    unauthorized,
    expired,
    illiquid,
    concentration,
    leverage,
    collateral,
    cost,
    no_improvement,
    infeasible,
    unknown,
    dispersion,
    quorum,
    domain,
    reorg,
    backpressure,
    incomplete,
};

enum class Rounding : std::uint32_t {
    floor,
    ceil,
    toward_zero,
    away_zero,
};

template <class T>
struct Result {
    Error error{Error::unknown};
    T value{};

    [[nodiscard]] explicit operator bool() const noexcept {
        return error == Error::okay;
    }
};

template <class T>
[[nodiscard]] constexpr Result<T> success(T value) noexcept {
    return {Error::okay, value};
}

template <class T>
[[nodiscard]] constexpr Result<T> failure(Error error) noexcept {
    return {error, {}};
}

[[nodiscard]] inline Result<std::int64_t> narrow(Wide value) noexcept {
    if (value > INT64_MAX || value < INT64_MIN) {
        return failure<std::int64_t>(Error::overflow);
    }
    return success(static_cast<std::int64_t>(value));
}

[[nodiscard]] inline Result<std::int64_t> add(std::int64_t a, std::int64_t b) noexcept {
    return narrow(static_cast<Wide>(a) + b);
}

[[nodiscard]] inline Result<std::int64_t> subtract(std::int64_t a, std::int64_t b) noexcept {
    return narrow(static_cast<Wide>(a) - b);
}

[[nodiscard]] inline Result<std::int64_t> absolute(std::int64_t value) noexcept {
    if (value == INT64_MIN) {
        return failure<std::int64_t>(Error::overflow);
    }
    return success(value < 0 ? -value : value);
}

// Positive denominator makes floor/ceil semantics independent of the sign
// of a financial cash flow. C++ integer division alone rounds toward zero.
[[nodiscard]] inline Result<std::int64_t> divide_wide(
    Wide numerator,
    std::int64_t denominator,
    Rounding rounding = Rounding::toward_zero
) noexcept {
    if (denominator == 0) {
        return failure<std::int64_t>(Error::divide_zero);
    }
    if (denominator < 0) {
        return failure<std::int64_t>(Error::invalid);
    }
    auto quotient = numerator / denominator;
    const auto remainder = numerator % denominator;
    if (remainder != 0) {
        if (rounding == Rounding::floor && numerator < 0) {
            --quotient;
        } else if (rounding == Rounding::ceil && numerator > 0) {
            ++quotient;
        } else if (rounding == Rounding::away_zero) {
            quotient += numerator < 0 ? -1 : 1;
        }
    }
    return narrow(quotient);
}

[[nodiscard]] inline Result<std::int64_t> multiply(
    std::int64_t a,
    std::int64_t b,
    Rounding rounding = Rounding::toward_zero
) noexcept {
    return divide_wide(static_cast<Wide>(a) * b, scale, rounding);
}

[[nodiscard]] inline Result<std::int64_t> ratio(
    std::int64_t numerator,
    std::int64_t denominator,
    Rounding rounding = Rounding::toward_zero
) noexcept {
    return divide_wide(static_cast<Wide>(numerator) * scale, denominator, rounding);
}

[[nodiscard]] inline Result<std::int64_t> basis_points(
    std::int64_t value,
    std::int64_t rate,
    Rounding rounding = Rounding::ceil
) noexcept {
    return divide_wide(static_cast<Wide>(value) * rate, bps_scale, rounding);
}

[[nodiscard]] inline Result<std::int64_t> step_round(
    std::int64_t value,
    std::int64_t step,
    Rounding rounding
) noexcept {
    if (value < 0 || step <= 0) {
        return failure<std::int64_t>(Error::invalid);
    }
    auto lots = value / step;
    if (value % step && (rounding == Rounding::ceil || rounding == Rounding::away_zero)) {
        if (lots == INT64_MAX) {
            return failure<std::int64_t>(Error::overflow);
        }
        ++lots;
    }
    return narrow(static_cast<Wide>(lots) * step);
}

[[nodiscard]] inline Result<std::uint64_t> add_time(
    std::uint64_t at,
    std::uint64_t duration
) noexcept {
    if (duration > UINT64_MAX - at) {
        return failure<std::uint64_t>(Error::overflow);
    }
    return success(at + duration);
}

struct Envelope {
    std::uint64_t sequence{};
    std::uint64_t expected_sequence{};
    std::uint64_t observed_ns{};
    std::uint64_t now_ns{};
    std::uint64_t max_age_ns{};
    std::uint64_t deadline_ns{};
    std::uint64_t policy_version{};
    std::uint64_t expected_policy_version{};
};

[[nodiscard]] inline Error validate(const Envelope& envelope) noexcept {
    if (!envelope.sequence || !envelope.max_age_ns || !envelope.policy_version) {
        return Error::invalid;
    }
    if (envelope.sequence != envelope.expected_sequence ||
        envelope.policy_version != envelope.expected_policy_version) {
        return Error::sequence;
    }
    if (envelope.now_ns >= envelope.deadline_ns) {
        return Error::expired;
    }
    if (envelope.observed_ns > envelope.now_ns) {
        return Error::future;
    }
    if (envelope.now_ns - envelope.observed_ns > envelope.max_age_ns) {
        return Error::stale;
    }
    return Error::okay;
}

[[nodiscard]] inline Result<std::uint64_t> expiry(const Envelope& envelope) noexcept {
    const auto error = validate(envelope);
    if (error != Error::okay) {
        return failure<std::uint64_t>(error);
    }
    const auto age_limit = add_time(envelope.observed_ns, envelope.max_age_ns);
    if (!age_limit) {
        // Saturation of an age limit is safe only because the caller supplied
        // an independently bounded, already validated decision deadline.
        return success(envelope.deadline_ns);
    }
    return success(std::min(age_limit.value, envelope.deadline_ns));
}

template <class T, std::size_t Capacity>
class Bounded {
    std::array<T, Capacity> values_{};
    std::size_t count_{};

public:
    [[nodiscard]] bool push(const T& value) noexcept {
        if (count_ == Capacity) {
            return false;
        }
        values_[count_++] = value;
        return true;
    }

    [[nodiscard]] std::span<const T> values() const noexcept {
        return {values_.data(), count_};
    }

    [[nodiscard]] std::span<T> values() noexcept {
        return {values_.data(), count_};
    }

    [[nodiscard]] std::size_t size() const noexcept {
        return count_;
    }

    void clear() noexcept {
        count_ = 0;
    }
};

// This fingerprint is a deterministic diagnostic identity, not a cryptographic
// commitment, oracle attestation or authorization. The authority journal hashes
// canonical JSON with SHA-256 outside the native arithmetic loop.
class Fingerprint {
    std::uint64_t value_{14695981039346656037ULL};

public:
    void append(std::uint64_t value) noexcept {
        for (unsigned i = 0; i != 8; ++i) {
            value_ ^= (value >> (8 * i)) & 255;
            value_ *= 1099511628211ULL;
        }
    }

    [[nodiscard]] std::uint64_t value() const noexcept {
        return value_;
    }
};

} // namespace machine::economics
