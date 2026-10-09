#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

enum class FactUnit : std::uint32_t {
    none,
    quantity,
    price,
    money,
    rate,
    duration_ns,
    count,
    boolean,
    signed_money,
    signed_quantity,
};

enum class FactStatus : std::uint32_t {
    absent,
    coherent,
    gap,
    invalidated,
};

enum class FactOperation : std::uint32_t {
    snapshot,
    replacement,
    increment,
    invalidate,
};

struct FactKey {
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t domain{};
    std::uint64_t instrument{};
    std::uint64_t field{};
};

[[nodiscard]] inline bool operator==(const FactKey& a, const FactKey& b) noexcept {
    return a.account == b.account && a.network == b.network && a.domain == b.domain &&
        a.instrument == b.instrument && a.field == b.field;
}

[[nodiscard]] inline bool valid_key(const FactKey& key) noexcept {
    // A domain can represent public markets as well as a user account. Zero
    // account is reserved for public observations; the other IDs are required.
    return key.network && key.domain && key.instrument && key.field;
}

struct TypedFact {
    FactKey key{};
    FactUnit unit{};
    std::uint64_t asset{};
    std::uint64_t quote_asset{};
    std::int64_t value{};
    std::uint64_t source{};
    std::uint64_t source_generation{};
    std::uint64_t sequence{};
    std::uint64_t observed_ns{};
    std::uint64_t valid_for_ns{};
    FactStatus status{FactStatus::absent};
};

struct FactDelta {
    std::uint64_t event{};
    std::uint64_t fingerprint{};
    FactOperation operation{};
    TypedFact fact{};
};

struct FactCommit {
    Error error{Error::unknown};
    std::uint64_t generation{};
    std::uint64_t event{};
    std::uint32_t changed{};
    bool replay{};
};

[[nodiscard]] inline bool asset_unit(FactUnit unit) noexcept {
    return unit == FactUnit::quantity || unit == FactUnit::price || unit == FactUnit::money ||
        unit == FactUnit::signed_money || unit == FactUnit::signed_quantity;
}

[[nodiscard]] inline Error validate_fact(const TypedFact& fact) noexcept {
    const auto unit = static_cast<std::uint32_t>(fact.unit);
    if (!valid_key(fact.key) || unit == 0 || unit > static_cast<std::uint32_t>(FactUnit::signed_quantity) ||
        !fact.source || !fact.source_generation || !fact.sequence || !fact.valid_for_ns ||
        (asset_unit(fact.unit) && !fact.asset) || (!asset_unit(fact.unit) && fact.asset) ||
        (fact.unit == FactUnit::price ? (!fact.quote_asset || fact.asset == fact.quote_asset) : fact.quote_asset != 0)) {
        return Error::invalid;
    }
    if (fact.unit == FactUnit::boolean && fact.value != 0 && fact.value != 1) {
        return Error::invalid;
    }
    if (fact.value < 0 && fact.unit != FactUnit::signed_money && fact.unit != FactUnit::signed_quantity &&
        fact.unit != FactUnit::rate) {
        return Error::invalid;
    }
    return Error::okay;
}

[[nodiscard]] inline Error fact_freshness(const TypedFact& fact, std::uint64_t now_ns) noexcept {
    if (fact.status != FactStatus::coherent) {
        return Error::unknown;
    }
    if (fact.observed_ns > now_ns) {
        return Error::future;
    }
    if (now_ns - fact.observed_ns >= fact.valid_for_ns) {
        return Error::stale;
    }
    return Error::okay;
}

[[nodiscard]] inline std::uint64_t fact_key_hash(const FactKey& key) noexcept {
    Fingerprint hash;
    hash.append(key.account);
    hash.append(key.network);
    hash.append(key.domain);
    hash.append(key.instrument);
    hash.append(key.field);
    return hash.value();
}

// Caller labels are not a content identity. Derive the replay comparison from
// every semantic field locally; this diagnostic checksum is not an attestation.
[[nodiscard]] inline std::uint64_t delta_fingerprint(const FactDelta& delta) noexcept {
    Fingerprint hash;
    hash.append(delta.event);
    hash.append(delta.fingerprint);
    hash.append(static_cast<std::uint32_t>(delta.operation));
    hash.append(fact_key_hash(delta.fact.key));
    hash.append(static_cast<std::uint32_t>(delta.fact.unit));
    hash.append(delta.fact.asset);
    hash.append(delta.fact.quote_asset);
    hash.append(static_cast<std::uint64_t>(delta.fact.value));
    hash.append(delta.fact.source);
    hash.append(delta.fact.source_generation);
    hash.append(delta.fact.sequence);
    hash.append(delta.fact.observed_ns);
    hash.append(delta.fact.valid_for_ns);
    hash.append(static_cast<std::uint32_t>(delta.fact.status));
    return hash.value();
}

template <std::size_t Capacity, std::size_t ReplayCapacity = Capacity>
class EconomicStateStore {
    static_assert(Capacity > 0 && Capacity <= 4096);
    static_assert(ReplayCapacity > 0 && ReplayCapacity <= 65536);

    struct Slot {
        TypedFact fact{};
        bool occupied{};
    };

    struct Replay {
        std::uint64_t event{};
        std::uint64_t fingerprint{};
        FactCommit commit{};
    };

    std::array<Slot, Capacity> slots_{};
    std::array<Replay, ReplayCapacity> replay_{};
    std::size_t replay_count_{};
    std::uint64_t generation_{};
    std::uint64_t event_floor_{};

    [[nodiscard]] Slot* find(const FactKey& key) noexcept {
        const auto hash = fact_key_hash(key);
        for (std::size_t offset = 0; offset < Capacity; ++offset) {
            auto& slot = slots_[(hash + offset) % Capacity];
            if (!slot.occupied || slot.fact.key == key) {
                return &slot;
            }
        }
        return nullptr;
    }

    [[nodiscard]] FactCommit apply_unrecorded(const FactDelta& delta) noexcept {
        FactCommit commit{Error::unknown, generation_, delta.event, 0, false};
        auto validation_fact = delta.fact;
        if (delta.operation == FactOperation::increment) {
            validation_fact.value = 0;
        }
        if (!delta.event || !delta.fingerprint || validate_fact(validation_fact) != Error::okay ||
            delta.fact.status != FactStatus::coherent) {
            commit.error = Error::invalid;
            return commit;
        }
        if (generation_ == UINT64_MAX) {
            commit.error = Error::overflow;
            return commit;
        }
        auto* slot = find(delta.fact.key);
        if (!slot) {
            commit.error = Error::capacity;
            return commit;
        }
        auto incoming = delta.fact;
        if (!slot->occupied) {
            if (delta.operation != FactOperation::snapshot) {
                commit.error = Error::missing;
                return commit;
            }
            slot->fact = incoming;
            slot->occupied = true;
        } else {
            const auto& old = slot->fact;
            if (incoming.unit != old.unit || incoming.asset != old.asset ||
                incoming.quote_asset != old.quote_asset || incoming.source != old.source) {
                // A different source requires an explicit new fact identity;
                // aggregation must not overwrite source provenance silently.
                commit.error = Error::domain;
                return commit;
            }
            if (incoming.source_generation < old.source_generation || incoming.observed_ns < old.observed_ns) {
                commit.error = Error::sequence;
                return commit;
            }
            if (incoming.source_generation == old.source_generation && incoming.sequence <= old.sequence) {
                commit.error = Error::sequence;
                return commit;
            }
            if (delta.operation != FactOperation::snapshot && incoming.source_generation != old.source_generation) {
                slot->fact.status = FactStatus::gap;
                ++generation_;
                commit.error = Error::sequence;
                commit.generation = generation_;
                commit.changed = 1;
                return commit;
            }
            if (delta.operation == FactOperation::invalidate) {
                incoming.status = FactStatus::invalidated;
            } else if (delta.operation == FactOperation::replacement || delta.operation == FactOperation::increment) {
                if (old.status != FactStatus::coherent) {
                    commit.error = Error::unknown;
                    return commit;
                }
                if (old.sequence == UINT64_MAX || incoming.sequence != old.sequence + 1) {
                    slot->fact.status = FactStatus::gap;
                    ++generation_;
                    commit.error = Error::sequence;
                    commit.generation = generation_;
                    commit.changed = 1;
                    return commit;
                }
                if (delta.operation == FactOperation::increment) {
                    const auto incremented = add(old.value, incoming.value);
                    if (!incremented) {
                        commit.error = incremented.error;
                        return commit;
                    }
                    incoming.value = incremented.value;
                    if (validate_fact(incoming) != Error::okay) {
                        commit.error = Error::invalid;
                        return commit;
                    }
                }
            } else if (delta.operation != FactOperation::snapshot) {
                commit.error = Error::unsupported;
                return commit;
            }
            slot->fact = incoming;
        }
        ++generation_;
        commit.error = Error::okay;
        commit.generation = generation_;
        commit.changed = 1;
        return commit;
    }

public:
    // Replay IDs are retained until the authority journal checkpoints and
    // explicitly compacts them. There is no silent eviction and re-execution.
    [[nodiscard]] FactCommit apply(const FactDelta& delta) noexcept {
        if (delta.event <= event_floor_) {
            return {Error::sequence, generation_, delta.event, 0, false};
        }
        for (std::size_t i = 0; i < replay_count_; ++i) {
            const auto& replay = replay_[i];
            if (replay.event == delta.event) {
                if (replay.fingerprint != delta_fingerprint(delta)) {
                    return {Error::conflict, generation_, delta.event, 0, false};
                }
                auto commit = replay.commit;
                commit.replay = true;
                commit.changed = 0;
                return commit;
            }
        }
        if (replay_count_ == ReplayCapacity) {
            return {Error::capacity, generation_, delta.event, 0, false};
        }
        const auto commit = apply_unrecorded(delta);
        if (delta.event && delta.fingerprint) {
            replay_[replay_count_++] = {delta.event, delta_fingerprint(delta), commit};
        }
        return commit;
    }

    // Atomic batch: a rejected batch leaves both state and replay table intact.
    // The fixed capacity makes the copy bounded; bulk planning is a warm path,
    // not a per-packet operation. Gaps in single deltas invalidate their fact.
    [[nodiscard]] FactCommit apply_batch(std::span<const FactDelta> deltas) noexcept {
        if (deltas.empty() || deltas.size() > 64) {
            return {Error::invalid, generation_, 0, 0, false};
        }
        auto staged = *this;
        std::uint32_t changes = 0;
        bool all_replays = true;
        for (const auto& delta : deltas) {
            const auto commit = staged.apply(delta);
            if (commit.error != Error::okay) {
                return {commit.error, generation_, delta.event, 0, false};
            }
            changes += commit.changed;
            all_replays &= commit.replay;
        }
        *this = staged;
        return {Error::okay, generation_, deltas.back().event, changes, all_replays};
    }

    [[nodiscard]] Result<TypedFact> read(
        const FactKey& key,
        FactUnit unit,
        std::uint64_t asset,
        std::uint64_t now_ns,
        std::uint64_t quote_asset = 0
    ) const noexcept {
        if (!valid_key(key)) {
            return failure<TypedFact>(Error::invalid);
        }
        const auto hash = fact_key_hash(key);
        for (std::size_t offset = 0; offset < Capacity; ++offset) {
            const auto& slot = slots_[(hash + offset) % Capacity];
            if (!slot.occupied) {
                return failure<TypedFact>(Error::missing);
            }
            if (!(slot.fact.key == key)) {
                continue;
            }
            if (slot.fact.unit != unit || slot.fact.asset != asset || slot.fact.quote_asset != quote_asset) {
                return failure<TypedFact>(Error::domain);
            }
            const auto freshness = fact_freshness(slot.fact, now_ns);
            if (freshness != Error::okay) {
                return failure<TypedFact>(freshness);
            }
            return success(slot.fact);
        }
        return failure<TypedFact>(Error::missing);
    }

    [[nodiscard]] Error invalidate_source(
        std::uint64_t source,
        std::uint64_t generation,
        std::uint64_t now_ns
    ) noexcept {
        if (!source || !generation || generation_ == UINT64_MAX) {
            return Error::invalid;
        }
        bool changed = false;
        for (auto& slot : slots_) {
            if (!slot.occupied || slot.fact.source != source) {
                continue;
            }
            if (slot.fact.source_generation > generation || slot.fact.observed_ns > now_ns) {
                continue;
            }
            slot.fact.status = FactStatus::invalidated;
            changed = true;
        }
        if (changed) {
            ++generation_;
        }
        return Error::okay;
    }

    [[nodiscard]] std::uint64_t generation() const noexcept {
        return generation_;
    }

    [[nodiscard]] Error compact_replays(
        std::uint64_t cutoff_event,
        std::uint64_t expected_state_generation,
        bool journal_checkpoint_verified
    ) noexcept {
        if (!journal_checkpoint_verified || !cutoff_event || cutoff_event < event_floor_ ||
            expected_state_generation != generation_) {
            return Error::unauthorized;
        }
        std::size_t retained = 0;
        for (std::size_t i = 0; i < replay_count_; ++i) {
            if (replay_[i].event > cutoff_event) {
                replay_[retained++] = replay_[i];
            }
        }
        replay_count_ = retained;
        event_floor_ = cutoff_event;
        return Error::okay;
    }

    [[nodiscard]] std::uint64_t diagnostic_fingerprint() const noexcept {
        std::array<std::size_t, Capacity> sorted{};
        std::size_t count = 0;
        for (std::size_t i = 0; i < Capacity; ++i) {
            if (slots_[i].occupied) {
                sorted[count++] = i;
            }
        }
        std::sort(sorted.begin(), sorted.begin() + count, [&](auto a, auto b) {
            const auto& x = slots_[a].fact.key;
            const auto& y = slots_[b].fact.key;
            if (x.account != y.account) return x.account < y.account;
            if (x.network != y.network) return x.network < y.network;
            if (x.domain != y.domain) return x.domain < y.domain;
            if (x.instrument != y.instrument) return x.instrument < y.instrument;
            return x.field < y.field;
        });
        Fingerprint fingerprint;
        for (std::size_t i = 0; i < count; ++i) {
            const auto& fact = slots_[sorted[i]].fact;
            fingerprint.append(fact.key.account);
            fingerprint.append(fact.key.network);
            fingerprint.append(fact.key.domain);
            fingerprint.append(fact.key.instrument);
            fingerprint.append(fact.key.field);
            fingerprint.append(static_cast<std::uint32_t>(fact.unit));
            fingerprint.append(fact.asset);
            fingerprint.append(fact.quote_asset);
            fingerprint.append(static_cast<std::uint64_t>(fact.value));
            fingerprint.append(fact.source);
            fingerprint.append(fact.source_generation);
            fingerprint.append(fact.sequence);
            fingerprint.append(fact.observed_ns);
            fingerprint.append(fact.valid_for_ns);
            fingerprint.append(static_cast<std::uint32_t>(fact.status));
        }
        return fingerprint.value();
    }
};

struct Dependency {
    FactKey key{};
    FactUnit unit{};
    std::uint64_t asset{};
    std::uint64_t quote_asset{};
};

struct DependencyFrame {
    std::array<TypedFact, 32> facts{};
    std::uint32_t count{};
    std::uint64_t state_generation{};
    std::uint64_t valid_until_ns{};
    std::uint64_t newest_ns{};
    std::uint64_t oldest_ns{};
};

template <std::size_t Capacity, std::size_t ReplayCapacity>
[[nodiscard]] Result<DependencyFrame> resolve_dependencies(
    const EconomicStateStore<Capacity, ReplayCapacity>& state,
    std::span<const Dependency> dependencies,
    std::uint64_t now_ns,
    std::uint64_t max_skew_ns
) noexcept {
    if (dependencies.empty() || dependencies.size() > 32) {
        return failure<DependencyFrame>(Error::invalid);
    }
    DependencyFrame frame{};
    frame.count = static_cast<std::uint32_t>(dependencies.size());
    frame.state_generation = state.generation();
    frame.oldest_ns = UINT64_MAX;
    frame.valid_until_ns = UINT64_MAX;
    for (std::size_t i = 0; i < dependencies.size(); ++i) {
        const auto& dependency = dependencies[i];
        for (std::size_t j = 0; j < i; ++j) {
            if (dependencies[j].key == dependency.key) {
                return failure<DependencyFrame>(Error::conflict);
            }
        }
        const auto fact = state.read(dependency.key, dependency.unit, dependency.asset, now_ns, dependency.quote_asset);
        if (!fact) {
            return failure<DependencyFrame>(fact.error);
        }
        const auto until = add_time(fact.value.observed_ns, fact.value.valid_for_ns);
        if (!until) {
            return failure<DependencyFrame>(until.error);
        }
        frame.facts[i] = fact.value;
        frame.valid_until_ns = std::min(frame.valid_until_ns, until.value);
        frame.newest_ns = std::max(frame.newest_ns, fact.value.observed_ns);
        frame.oldest_ns = std::min(frame.oldest_ns, fact.value.observed_ns);
    }
    if (frame.newest_ns - frame.oldest_ns > max_skew_ns) {
        return failure<DependencyFrame>(Error::stale);
    }
    return success(frame);
}

} // namespace machine::economics
