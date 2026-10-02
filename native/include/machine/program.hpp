#pragma once

#include "machine/kernel.hpp"
#include <algorithm>

namespace machine {

inline constexpr std::uint32_t program_version = 1;
inline constexpr std::size_t register_count = 32;
inline constexpr std::size_t field_count = 32;
inline constexpr std::size_t instruction_count = 128;

// Values use signed micro-units except count, duration, and boolean. Time in
// the envelope uses nanoseconds; durations in registers use milliseconds.
enum class Unit : std::uint32_t {
    none, scalar, money, quantity, price, rate, duration, boolean, count
};
enum class Op : std::uint32_t {
    constant = 1, load, add, subtract, minimum, maximum, multiply_scaled,
    divide_scaled, less, less_equal, equal, logical_and, logical_or,
    logical_not, select, assert_true, candidate, abstain,
    absolute, negate, clamp, floor_step, ceil_step, notional_buy,
    notional_sell, fee_reserve
};
enum class ProgramCode : std::uint32_t {
    okay, malformed, uninitialized, unit_mismatch, overflow, divide_zero,
    assertion_failed, state_invalid, stale, expired, explicit_abstention,
    no_candidate, future_state
};
enum class Candidate : std::uint32_t { hold = 1, reduce, hedge, cancel, escalate };

struct Instruction {
    std::uint32_t opcode;
    std::uint32_t destination;
    std::uint32_t a;
    std::uint32_t b;
    std::uint32_t c;
    std::uint32_t unit;
    std::int64_t immediate;
};

struct Program {
    std::uint32_t version;
    std::uint32_t count;
    std::uint32_t field_units[field_count];
    Instruction instructions[instruction_count];
};

struct Frame {
    std::uint32_t version;
    std::uint32_t valid;
    std::uint64_t sequence;
    std::uint64_t expected_sequence;
    std::uint64_t observed_ns;
    std::uint64_t now_ns;
    std::uint64_t max_age_ns;
    std::uint64_t deadline_ns;
    std::int64_t values[field_count];
};

struct ProgramOutput {
    std::uint32_t version;
    std::uint32_t code;
    std::uint32_t failed_instruction;
    std::uint32_t candidate;
    std::uint64_t sequence;
    std::uint64_t valid_until_ns;
    std::int64_t score;
    std::uint64_t execution_authority;
};

struct Validation {
    ProgramCode code;
    std::uint32_t instruction;
};

Validation validate_program(const Program& program) noexcept;
ProgramOutput run_program(const Program& program, const Frame& frame) noexcept;

// Fixed-depth orderbook. No eviction or guessed snapshot recovery on a gap.
struct Level { std::int64_t price{}, quantity{}; };
struct Quote {
    Code code{Code::unknown};
    std::int64_t quantity{}, notional{}, average_price{}, worst_price{};
    std::uint64_t sequence{};
};

template <std::size_t Depth>
class DepthBook {
    std::array<Level, Depth> bids_{}, asks_{};
    std::uint64_t sequence_{}, observed_ns_{};
    bool valid_{};

    static bool sane(const std::array<Level, Depth>& levels, bool bids) noexcept {
        bool end = false;
        std::int64_t last = bids ? std::numeric_limits<std::int64_t>::max() : 0;
        for (const auto& level : levels) {
            if (level.price == 0 && level.quantity == 0) { end = true; continue; }
            if (end || level.price <= 0 || level.quantity <= 0 ||
                (bids ? level.price >= last : level.price <= last)) return false;
            last = level.price;
        }
        return levels[0].price > 0;
    }

public:
    Code snapshot(const std::array<Level, Depth>& bids,
                  const std::array<Level, Depth>& asks,
                  std::uint64_t sequence, std::uint64_t observed) noexcept {
        if (!sequence || (sequence_ && sequence <= sequence_) || observed < observed_ns_)
            return Code::sequence;
        if (!sane(bids, true) || !sane(asks, false) || bids[0].price >= asks[0].price) {
            valid_ = false;
            return Code::invalid;
        }
        bids_ = bids; asks_ = asks; sequence_ = sequence; observed_ns_ = observed;
        valid_ = true;
        return Code::admit;
    }

    Code update(bool bid, std::int64_t price, std::int64_t quantity,
                std::uint64_t sequence, std::uint64_t observed) noexcept {
        if (!valid_) return Code::unknown;
        if (sequence <= sequence_) return Code::sequence;
        if (sequence_ == UINT64_MAX || sequence != sequence_ + 1 || observed < observed_ns_) {
            valid_ = false;
            return Code::sequence;
        }
        if (price <= 0 || quantity < 0) { valid_ = false; return Code::invalid; }
        auto levels = bid ? bids_ : asks_;
        std::size_t at = Depth;
        for (std::size_t i = 0; i < Depth; ++i) {
            if (levels[i].price == price) { at = i; break; }
        }
        if (at < Depth) {
            if (quantity) levels[at].quantity = quantity;
            else {
                for (auto i = at; i + 1 < Depth; ++i) levels[i] = levels[i + 1];
                levels.back() = {};
            }
        } else if (quantity) {
            for (std::size_t i = 0; i < Depth; ++i) {
                if (!levels[i].price || (bid ? price > levels[i].price : price < levels[i].price)) {
                    at = i; break;
                }
            }
            if (at == Depth) {
                // Bounded book cannot prove liquidity outside retained depth.
                // A worse out-of-range level is intentionally ignored.
            } else {
                for (auto i = Depth - 1; i > at; --i) levels[i] = levels[i - 1];
                levels[at] = {price, quantity};
            }
        }
        if (!sane(levels, bid)) { valid_ = false; return Code::invalid; }
        const auto best_bid = bid ? levels[0].price : bids_[0].price;
        const auto best_ask = bid ? asks_[0].price : levels[0].price;
        if (best_bid >= best_ask) { valid_ = false; return Code::invalid; }
        (bid ? bids_ : asks_) = levels;
        sequence_ = sequence; observed_ns_ = observed;
        return Code::admit;
    }

    Quote quote(Side side, std::int64_t quantity, std::uint64_t now,
                std::uint64_t max_age) const noexcept {
        Quote result{}; result.sequence = sequence_;
        if (!valid_) return result;
        if ((side != Side::buy && side != Side::sell) || quantity <= 0) {
            result.code = Code::invalid; return result;
        }
        if (!max_age || now < observed_ns_ || now - observed_ns_ > max_age) {
            result.code = Code::stale; return result;
        }
        auto remaining = quantity;
        __int128 weighted = 0;
        const auto& levels = side == Side::buy ? asks_ : bids_;
        for (const auto& level : levels) {
            if (!remaining || !level.price) break;
            const auto take = std::min(remaining, level.quantity);
            const auto term = static_cast<__int128>(take) * level.price;
            // Every positive partial sum is checked before adding; overflow
            // cannot be hidden by later cancellation or a wide accumulator.
            if (term > static_cast<__int128>(INT64_MAX) * amount_scale - weighted) {
                result.code = Code::overflow; return result;
            }
            weighted += term; remaining -= take; result.worst_price = level.price;
        }
        if (remaining) { result.code = Code::capacity; return result; }
        result.quantity = quantity;
        const auto rounding = side == Side::buy ? amount_scale - 1 : 0;
        result.notional = static_cast<std::int64_t>((weighted + rounding) / amount_scale);
        const auto price_rounding = side == Side::buy ? quantity - 1 : 0;
        const auto average = (weighted + price_rounding) / quantity;
        if (average > INT64_MAX) { result.code = Code::overflow; return result; }
        result.average_price = static_cast<std::int64_t>(average);
        result.code = Code::admit;
        return result;
    }
    bool valid() const noexcept { return valid_; }
};

} // namespace machine

extern "C" {
std::size_t machine_program_size() noexcept;
std::size_t machine_frame_size() noexcept;
std::size_t machine_program_output_size() noexcept;
int machine_program_validate(const machine::Program*, std::uint32_t*, std::uint32_t*) noexcept;
int machine_program_run(const machine::Program*, const machine::Frame*, machine::ProgramOutput*) noexcept;
}
