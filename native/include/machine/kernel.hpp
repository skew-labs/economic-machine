#pragma once

#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <type_traits>

namespace machine {

inline constexpr std::uint32_t abi_version = 1;
inline constexpr std::int64_t amount_scale = 1'000'000;
inline constexpr std::size_t cache_line = 128;

enum class Side : std::uint32_t { buy = 1, sell = 2 };
enum class Code : std::uint32_t {
    admit = 0,
    invalid = 1,
    expired = 2,
    stale = 3,
    sequence = 4,
    increment = 5,
    notional = 6,
    balance = 7,
    exposure = 8,
    slippage = 9,
    turnover = 10,
    disabled = 11,
    overflow = 12,
    unknown = 13,
    capacity = 14,
    conflict = 15,
};

struct Input {
    std::uint32_t version;
    std::uint32_t side;
    std::uint64_t sequence;
    std::uint64_t expected_sequence;
    std::uint64_t now_ns;
    std::uint64_t observed_ns;
    std::uint64_t max_age_ns;
    std::uint64_t deadline_ns;
    std::int64_t quantity;
    std::int64_t price;
    std::int64_t reference_price;
    std::int64_t quantity_step;
    std::int64_t price_tick;
    std::int64_t min_notional;
    std::int64_t max_order;
    std::int64_t available_quote;
    std::int64_t available_base;
    std::int64_t exposure_after;
    std::int64_t max_exposure;
    std::int64_t turnover_remaining;
    std::uint32_t max_slippage_bps;
    std::uint32_t fee_reserve_bps;
    std::uint32_t policy_active;
    std::uint32_t oracle_valid;
};

struct Output {
    std::uint32_t version;
    std::uint32_t code;
    std::uint64_t sequence;
    std::int64_t notional;
    std::int64_t required_quote;
    std::uint64_t valid_until_ns;
    // Always zero. A native decision never grants signing authority.
    std::uint64_t execution_authority;
};

static_assert(std::is_trivially_copyable_v<Input>);
static_assert(std::is_standard_layout_v<Input>);
static_assert(std::is_trivially_copyable_v<Output>);

[[nodiscard]] Output evaluate(const Input& input) noexcept;

// Single producer and single consumer only. Ownership is part of the contract.
// A full queue reports backpressure; it never overwrites an unread event.
template <class T, std::size_t Capacity>
class SpscRing {
    static_assert(std::has_single_bit(Capacity));
    static_assert(Capacity >= 2 && Capacity <= (1ULL << 20));
    static_assert(std::is_trivially_copyable_v<T>);
    alignas(cache_line) std::atomic<std::uint64_t> head_{0};
    alignas(cache_line) std::atomic<std::uint64_t> tail_{0};
    alignas(cache_line) std::array<T, Capacity> slots_{};

public:
    SpscRing() = default;
    SpscRing(const SpscRing&) = delete;
    SpscRing& operator=(const SpscRing&) = delete;

    [[nodiscard]] bool push(const T& value) noexcept {
        const auto head = head_.load(std::memory_order_relaxed);
        const auto tail = tail_.load(std::memory_order_acquire);
        if (head - tail >= Capacity) return false;
        slots_[head & (Capacity - 1)] = value;
        head_.store(head + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] bool pop(T& value) noexcept {
        const auto tail = tail_.load(std::memory_order_relaxed);
        const auto head = head_.load(std::memory_order_acquire);
        if (head == tail) return false;
        value = slots_[tail & (Capacity - 1)];
        tail_.store(tail + 1, std::memory_order_release);
        return true;
    }

    [[nodiscard]] std::uint64_t size_approx() const noexcept {
        const auto tail = tail_.load(std::memory_order_acquire);
        const auto head = head_.load(std::memory_order_acquire);
        return head >= tail ? head - tail : 0;
    }

    [[nodiscard]] static constexpr std::size_t capacity() noexcept { return Capacity; }
};

struct StateDelta {
    std::uint64_t instrument;
    std::uint64_t sequence;
    std::uint64_t observed_ns;
    std::int64_t bid;
    std::int64_t ask;
    std::int64_t bid_size;
    std::int64_t ask_size;
    std::uint64_t policy_epoch;
};

struct StateSlot {
    StateDelta value{};
    bool occupied = false;
    bool valid = false;
};

template <std::size_t Capacity>
class StateBook {
    static_assert(std::has_single_bit(Capacity));
    std::array<StateSlot, Capacity> slots_{};

public:
    [[nodiscard]] Code apply(const StateDelta& delta) noexcept {
        if (!delta.instrument || !delta.sequence || !delta.observed_ns || delta.bid <= 0 ||
            delta.ask < delta.bid || delta.bid_size < 0 || delta.ask_size < 0) return Code::invalid;
        for (std::size_t offset = 0; offset < Capacity; ++offset) {
            auto& slot = slots_[(delta.instrument + offset) & (Capacity - 1)];
            if (!slot.occupied) {
                slot.value = delta;
                slot.occupied = true;
                slot.valid = true;
                return Code::admit;
            }
            if (slot.value.instrument != delta.instrument) continue;
            if (delta.sequence <= slot.value.sequence) return Code::sequence;
            if (delta.observed_ns < slot.value.observed_ns || delta.policy_epoch < slot.value.policy_epoch)
                return Code::sequence;
            if (delta.sequence != slot.value.sequence + 1) {
                slot.valid = false;
                return Code::sequence;
            }
            if (!slot.valid) return Code::unknown;
            slot.value = delta;
            return Code::admit;
        }
        return Code::capacity;
    }

    [[nodiscard]] Code snapshot(const StateDelta& delta) noexcept {
        if (!delta.instrument || !delta.sequence || !delta.observed_ns || delta.bid <= 0 ||
            delta.ask < delta.bid || delta.bid_size < 0 || delta.ask_size < 0) return Code::invalid;
        for (std::size_t offset = 0; offset < Capacity; ++offset) {
            auto& slot = slots_[(delta.instrument + offset) & (Capacity - 1)];
            if (!slot.occupied || slot.value.instrument == delta.instrument) {
                if (slot.occupied && (delta.sequence < slot.value.sequence ||
                    delta.observed_ns < slot.value.observed_ns || delta.policy_epoch < slot.value.policy_epoch))
                    return Code::sequence;
                slot.value = delta;
                slot.occupied = slot.valid = true;
                return Code::admit;
            }
        }
        return Code::capacity;
    }

    [[nodiscard]] const StateDelta* get(std::uint64_t instrument) const noexcept {
        for (std::size_t offset = 0; offset < Capacity; ++offset) {
            const auto& slot = slots_[(instrument + offset) & (Capacity - 1)];
            if (!slot.occupied) return nullptr;
            if (slot.value.instrument == instrument) return slot.valid ? &slot.value : nullptr;
        }
        return nullptr;
    }
};

enum class HoldStatus : std::uint32_t { empty, reserved, unknown, terminal };
struct Hold {
    std::uint64_t id = 0;
    std::uint64_t fingerprint = 0;
    std::int64_t reserved = 0;
    std::int64_t spent = 0;
    HoldStatus status = HoldStatus::empty;
};

// Single-writer budget mirror. Durable accounting remains in the authority layer.
template <std::size_t Capacity>
class CapitalBook {
    std::array<Hold, Capacity> holds_{};
    std::int64_t limit_;
    std::int64_t held_ = 0;
    std::int64_t spent_ = 0;

public:
    explicit CapitalBook(std::int64_t limit) noexcept : limit_(limit) {}

    [[nodiscard]] Code reserve(std::uint64_t id, std::uint64_t fingerprint, std::int64_t amount) noexcept {
        if (!id || !fingerprint || amount <= 0 || limit_ <= 0) return Code::invalid;
        Hold* empty = nullptr;
        for (auto& hold : holds_) {
            if (hold.status == HoldStatus::empty) { if (!empty) empty = &hold; continue; }
            if (hold.id == id) return hold.fingerprint == fingerprint && hold.reserved == amount ? Code::admit : Code::conflict;
        }
        if (!empty) return Code::capacity;
        if (spent_ > limit_ || held_ > limit_ - spent_ || amount > limit_ - spent_ - held_) return Code::turnover;
        *empty = Hold{id, fingerprint, amount, 0, HoldStatus::reserved};
        held_ += amount;
        return Code::admit;
    }

    [[nodiscard]] Code ambiguous(std::uint64_t id) noexcept {
        for (auto& hold : holds_) {
            if (hold.id != id || hold.status == HoldStatus::empty) continue;
            if (hold.status == HoldStatus::terminal) return Code::conflict;
            hold.status = HoldStatus::unknown;
            return Code::admit;
        }
        return Code::unknown;
    }

    [[nodiscard]] Code settle(std::uint64_t id, std::int64_t actual) noexcept {
        if (actual < 0) return Code::invalid;
        for (auto& hold : holds_) {
            if (hold.id != id || hold.status == HoldStatus::empty) continue;
            if (hold.status == HoldStatus::terminal) return actual == hold.spent ? Code::admit : Code::conflict;
            if (actual > hold.reserved) return Code::turnover;
            held_ -= hold.reserved;
            spent_ += actual;
            hold.spent = actual;
            hold.status = HoldStatus::terminal;
            return Code::admit;
        }
        return Code::unknown;
    }

    [[nodiscard]] std::int64_t held() const noexcept { return held_; }
    [[nodiscard]] std::int64_t spent() const noexcept { return spent_; }
};

} // namespace machine

extern "C" {
std::uint32_t machine_abi_version() noexcept;
std::size_t machine_input_size() noexcept;
std::size_t machine_output_size() noexcept;
int machine_evaluate(const machine::Input*, machine::Output*) noexcept;
}
