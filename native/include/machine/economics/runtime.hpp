#pragma once

#include "machine/economics/strategy.hpp"
#include "machine/kernel.hpp"

namespace machine::economics {

struct ProgramMandate {
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t policy{};
    std::uint64_t version{};
    std::uint64_t expires_ns{};
    std::uint64_t minimum_interval_ns{};
    std::uint64_t maximum_skew_ns{};
    bool active{};
};

struct ProgramRegistration {
    StrategyProgram program{};
    std::array<Dependency, 32> dependencies{};
    ProgramMandate mandate{};
};

struct RuntimeDecision {
    std::uint64_t sequence{};
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t policy{};
    std::uint64_t policy_version{};
    std::uint64_t emitted_ns{};
    StrategyResult candidate{};
};

struct RuntimeCounters {
    std::uint64_t observations{};
    std::uint64_t replays{};
    std::uint64_t rejected{};
    std::uint64_t evaluations{};
    std::uint64_t escalations{};
    std::uint64_t emitted{};
    std::uint64_t backpressure{};
    std::uint64_t execution_authority{};
};

// One writer owns feed ingestion, policy updates and scheduling. One consumer
// can drain decisions through the existing SPSC queue. This is a numeric event
// runtime, not a network driver, signer, authority ledger or prediction model.
// No LLM calls, JSON parsing or dynamic allocation occur in these methods.
template <std::size_t FactCapacity, std::size_t ProgramCapacity, std::size_t QueueCapacity>
class EconomicRuntime {
    static_assert(ProgramCapacity > 0 && ProgramCapacity <= 128);

    struct ProgramSlot {
        ProgramRegistration registration{};
        RuntimeDecision pending{};
        std::uint64_t last_evaluated_ns{};
        std::uint64_t next_expiry_ns{};
        std::uint64_t last_state_generation{};
        Error last_error{Error::unknown};
        bool occupied{};
        bool dirty{};
        bool evaluated{};
        bool has_pending{};
    };

    EconomicStateStore<FactCapacity, FactCapacity * 4> state_{};
    std::array<ProgramSlot, ProgramCapacity> programs_{};
    machine::SpscRing<RuntimeDecision, QueueCapacity> outputs_{};
    RuntimeCounters counters_{};
    std::uint64_t sequence_{};
    std::uint64_t last_clock_ns_{};
    std::size_t cursor_{};

    [[nodiscard]] ProgramSlot* find_program(std::uint64_t program) noexcept {
        for (auto& slot : programs_) {
            if (slot.occupied && slot.registration.program.id == program) return &slot;
        }
        return nullptr;
    }

    [[nodiscard]] bool pending() const noexcept {
        for (const auto& slot : programs_) {
            if (slot.occupied && slot.has_pending) return true;
        }
        return false;
    }

    [[nodiscard]] Error check_clock(std::uint64_t now_ns) noexcept {
        if (!now_ns || now_ns < last_clock_ns_) return Error::sequence;
        last_clock_ns_ = now_ns;
        return Error::okay;
    }

    void mark_dependents(const FactKey& key) noexcept {
        // Deliberately bounded linear index in v1. Each match marks a program
        // once; there is no per-tick token inference or unbounded agent fanout.
        for (auto& slot : programs_) {
            if (!slot.occupied) continue;
            for (std::size_t i = 0; i < slot.registration.program.dependency_count; ++i) {
                if (slot.registration.dependencies[i].key == key) {
                    slot.dirty = true;
                    break;
                }
            }
        }
    }

    [[nodiscard]] Error validate_registration(const ProgramRegistration& registration) const noexcept {
        const auto& mandate = registration.mandate;
        if (!mandate.account || !mandate.network || !mandate.policy || !mandate.version ||
            !mandate.expires_ns || mandate.version != registration.program.policy_version ||
            !registration.program.dependency_count) return Error::invalid;
        const auto validation = validate_strategy(registration.program);
        if (validation.error != Error::okay) return validation.error;
        for (std::size_t i = 0; i < registration.program.dependency_count; ++i) {
            const auto& dependency = registration.dependencies[i];
            if (!valid_key(dependency.key) || dependency.key.network != mandate.network ||
                (dependency.key.account && dependency.key.account != mandate.account) ||
                !same_type(registration.program.dependency_types[i],
                    {dependency.unit, dependency.asset, dependency.quote_asset})) {
                return Error::domain;
            }
            for (std::size_t j = 0; j < i; ++j) {
                if (registration.dependencies[j].key == dependency.key) return Error::conflict;
            }
        }
        return Error::okay;
    }

    [[nodiscard]] bool flush(ProgramSlot& slot) noexcept {
        if (!slot.has_pending) return true;
        if (!outputs_.push(slot.pending)) {
            ++counters_.backpressure;
            return false;
        }
        slot.has_pending = false;
        ++counters_.emitted;
        return true;
    }

    void evaluate_slot(ProgramSlot& slot, std::uint64_t now_ns) noexcept {
        const auto& registration = slot.registration;
        const auto& mandate = registration.mandate;
        StrategyResult candidate{};
        candidate.program = registration.program.id;
        candidate.program_version = registration.program.version;
        candidate.state_generation = state_.generation();
        auto error = Error::okay;
        if (!mandate.active) error = Error::unauthorized;
        else if (now_ns >= mandate.expires_ns) error = Error::expired;
        Result<DependencyFrame> frame{};
        if (error == Error::okay) {
            frame = resolve_dependencies(state_,
                {registration.dependencies.data(), registration.program.dependency_count},
                now_ns, mandate.maximum_skew_ns);
            error = frame.error;
        }
        if (error == Error::okay) {
            StrategyFrame execution{frame.value, registration.program.version,
                mandate.version, now_ns, mandate.active};
            candidate = run_strategy(registration.program, execution);
            error = candidate.error;
            candidate.valid_until_ns = std::min(candidate.valid_until_ns, mandate.expires_ns);
        } else {
            candidate.error = error;
            candidate.action = EconomicAction::escalate;
        }
        if (candidate.error != Error::okay) {
            candidate.action = EconomicAction::escalate;
            candidate.amount = 0;
            ++counters_.escalations;
        }
        candidate.execution_authority = 0;
        ++counters_.evaluations;
        slot.dirty = false;
        slot.evaluated = true;
        slot.last_evaluated_ns = now_ns;
        slot.last_state_generation = state_.generation();
        slot.last_error = error;
        // Expiry is an interrupt even without another market event. An error
        // doesn't re-emit on every idle poll; feed/policy changes wake it again.
        slot.next_expiry_ns = candidate.error == Error::okay ? candidate.valid_until_ns : 0;
        slot.pending = {++sequence_, mandate.account, mandate.network, mandate.policy,
            mandate.version, now_ns, candidate};
        slot.has_pending = true;
    }

public:
    [[nodiscard]] Error install(const ProgramRegistration& registration, std::uint64_t now_ns) noexcept {
        const auto valid = validate_registration(registration);
        if (valid != Error::okay) return valid;
        if (now_ns >= registration.mandate.expires_ns) return Error::expired;
        if (check_clock(now_ns) != Error::okay) return Error::sequence;
        auto* slot = find_program(registration.program.id);
        if (slot) {
            const auto& old = slot->registration;
            if (slot->has_pending) return Error::backpressure;
            if (old.mandate.account != registration.mandate.account || old.mandate.network != registration.mandate.network ||
                old.mandate.policy != registration.mandate.policy) return Error::domain;
            if (registration.program.version <= old.program.version || registration.mandate.version < old.mandate.version)
                return Error::sequence;
        } else {
            for (auto& available : programs_) {
                if (!available.occupied) {
                    slot = &available;
                    break;
                }
            }
        }
        if (!slot) return Error::capacity;
        *slot = {};
        slot->registration = registration;
        slot->occupied = true;
        slot->dirty = true;
        return Error::okay;
    }

    [[nodiscard]] Error pause(std::uint64_t program, std::uint64_t policy_version, std::uint64_t now_ns) noexcept {
        auto* slot = find_program(program);
        if (!slot) return Error::missing;
        if (slot->registration.mandate.version != policy_version) return Error::unauthorized;
        if (check_clock(now_ns) != Error::okay) return Error::sequence;
        slot->registration.mandate.active = false;
        if (slot->has_pending) {
            slot->pending.candidate.error = Error::unauthorized;
            slot->pending.candidate.action = EconomicAction::escalate;
            slot->pending.candidate.amount = 0;
            slot->pending.candidate.valid_until_ns = 0;
        }
        slot->dirty = true;
        return Error::okay;
    }

    [[nodiscard]] Error remove(std::uint64_t program, std::uint64_t policy_version) noexcept {
        auto* slot = find_program(program);
        if (!slot) return Error::missing;
        if (slot->registration.mandate.version != policy_version) return Error::unauthorized;
        if (slot->has_pending) return Error::backpressure;
        // Removing a calculator doesn't release authority-ledger holds.
        *slot = {};
        return Error::okay;
    }

    [[nodiscard]] FactCommit observe(const FactDelta& delta, std::uint64_t now_ns) noexcept {
        if (pending()) {
            ++counters_.backpressure;
            return {Error::backpressure, state_.generation(), delta.event, 0, false};
        }
        if (check_clock(now_ns) != Error::okay || delta.fact.observed_ns > now_ns) {
            ++counters_.rejected;
            return {delta.fact.observed_ns > now_ns ? Error::future : Error::sequence,
                state_.generation(), delta.event, 0, false};
        }
        const auto commit = state_.apply(delta);
        if (commit.replay) ++counters_.replays;
        else if (commit.error == Error::okay) ++counters_.observations;
        else ++counters_.rejected;
        if (commit.changed) mark_dependents(delta.fact.key);
        return commit;
    }

    [[nodiscard]] Error disconnect(std::uint64_t source, std::uint64_t generation, std::uint64_t now_ns) noexcept {
        if (pending()) return Error::backpressure;
        if (check_clock(now_ns) != Error::okay) return Error::sequence;
        const auto result = state_.invalidate_source(source, generation, now_ns);
        if (result != Error::okay) return result;
        for (auto& slot : programs_) {
            if (slot.occupied) slot.dirty = true;
        }
        return Error::okay;
    }

    // work_bound counts slots inspected, not only runnable slots. An operator
    // chooses its core/timer cadence. No polling loop hides unbounded work.
    [[nodiscard]] Error poll(std::uint64_t now_ns, std::size_t work_bound) noexcept {
        if (!work_bound || work_bound > ProgramCapacity) return Error::invalid;
        if (check_clock(now_ns) != Error::okay) return Error::sequence;
        for (std::size_t inspected = 0; inspected < work_bound; ++inspected) {
            auto& slot = programs_[cursor_];
            cursor_ = (cursor_ + 1) % ProgramCapacity;
            if (!slot.occupied) continue;
            if (!flush(slot)) return Error::backpressure;
            const auto expired = slot.next_expiry_ns && now_ns >= slot.next_expiry_ns;
            if (!slot.dirty && !expired) continue;
            const auto elapsed = slot.evaluated ? now_ns - slot.last_evaluated_ns : UINT64_MAX;
            if (!expired && slot.evaluated && elapsed < slot.registration.mandate.minimum_interval_ns) continue;
            if (sequence_ == UINT64_MAX) return Error::overflow;
            evaluate_slot(slot, now_ns);
            if (!flush(slot)) return Error::backpressure;
        }
        return Error::okay;
    }

    [[nodiscard]] bool pop(RuntimeDecision& decision) noexcept { return outputs_.pop(decision); }

    [[nodiscard]] RuntimeCounters counters() const noexcept { return counters_; }

    [[nodiscard]] Error checkpoint(std::uint64_t event, std::uint64_t generation, bool journal_verified) noexcept {
        if (pending()) return Error::backpressure;
        return state_.compact_replays(event, generation, journal_verified);
    }

    [[nodiscard]] std::uint64_t state_generation() const noexcept { return state_.generation(); }

    [[nodiscard]] std::uint64_t state_fingerprint() const noexcept { return state_.diagnostic_fingerprint(); }

    [[nodiscard]] std::uint64_t queued_approx() const noexcept { return outputs_.size_approx(); }
};

} // namespace machine::economics
