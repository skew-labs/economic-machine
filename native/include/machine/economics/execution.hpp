#pragma once

#include "machine/economics/lending.hpp"

namespace machine::economics {

enum class ExecutionPrimitive : std::uint32_t {
    transfer = 1,
    swap,
    supply,
    redeem,
    borrow,
    repay,
    cancel,
    assert_balance,
};

struct ExecutionAsset {
    std::uint64_t asset{};
    std::int64_t available{};
    std::int64_t reserved{};
    std::int64_t liability{};
    std::int64_t price_low{};
    std::int64_t price_high{};
    std::int64_t collateral_factor{};
    std::int64_t liquidation_factor{};
    std::int64_t cash_floor{};
};

struct ExecutionStep {
    std::uint64_t id{};
    ExecutionPrimitive primitive{};
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t protocol{};
    std::uint64_t destination{};
    std::uint64_t dependencies{};
    std::uint64_t asset_in{};
    std::uint64_t asset_out{};
    std::uint64_t fee_asset{};
    std::uint64_t expires_ns{};
    std::int64_t debit{};
    std::int64_t minimum_credit{};
    std::int64_t maximum_credit{};
    std::int64_t maximum_fee{};
    std::int64_t maximum_cost_value{};
    std::int64_t quantity_step{};
    std::int64_t assert_minimum{};
    std::int64_t assert_maximum{};
    bool destination_allowed{};
    bool quote_verified{};
};

struct ExecutionGraph {
    std::uint64_t plan{};
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t policy_version{};
    std::uint64_t expected_policy_version{};
    std::uint64_t state_generation{};
    std::uint64_t now_ns{};
    std::uint64_t observed_ns{};
    std::uint64_t max_age_ns{};
    std::uint64_t expires_ns{};
    std::array<ExecutionAsset, 32> assets{};
    std::array<ExecutionStep, 64> steps{};
    std::uint32_t asset_count{};
    std::uint32_t step_count{};
    std::array<std::uint64_t, 16> allowed_protocols{};
    std::uint32_t protocol_count{};
    std::int64_t maximum_cost_value{};
    std::int64_t maximum_turnover_value{};
    std::int64_t maximum_debt_value{};
    std::int64_t minimum_health_factor{};
    struct Target {
        std::uint64_t asset{};
        std::int64_t minimum_available{};
        std::int64_t maximum_available{};
        std::int64_t minimum_reserved{};
        std::int64_t maximum_reserved{};
        std::int64_t maximum_liability{};
    };
    std::array<Target, 32> targets{};
    std::uint32_t target_count{};
};

struct ExecutionProjection {
    std::uint64_t plan{};
    std::array<std::uint32_t, 64> order{};
    std::array<ExecutionAsset, 32> assets_after{};
    std::uint32_t count{};
    std::uint32_t asset_count{};
    std::int64_t total_cost_value{};
    std::int64_t turnover_value{};
    std::int64_t liability_value{};
    std::int64_t liquidation_capacity{};
    std::int64_t borrow_capacity{};
    std::int64_t health_factor{};
    std::uint64_t valid_until_ns{};
    std::uint64_t diagnostic_fingerprint{};
    bool debt_free{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline bool allowed_protocol(const ExecutionGraph& graph, std::uint64_t protocol) noexcept {
    for (std::size_t i = 0; i < graph.protocol_count; ++i) {
        if (graph.allowed_protocols[i] == protocol) return true;
    }
    return false;
}

[[nodiscard]] inline Error validate_execution_asset(const ExecutionAsset& asset) noexcept {
    if (!asset.asset || asset.available < 0 || asset.reserved < 0 || asset.liability < 0 ||
        asset.price_low <= 0 || asset.price_high < asset.price_low || asset.cash_floor < 0 ||
        asset.available < asset.cash_floor || asset.collateral_factor < 0 ||
        asset.collateral_factor > asset.liquidation_factor || asset.liquidation_factor > scale) {
        return Error::invalid;
    }
    return Error::okay;
}

[[nodiscard]] inline Result<std::array<std::uint32_t, 64>> execution_order(const ExecutionGraph& graph) noexcept {
    if (!graph.step_count || graph.step_count > graph.steps.size()) {
        return failure<std::array<std::uint32_t, 64>>(Error::invalid);
    }
    const auto mask = graph.step_count == 64 ? UINT64_MAX : (1ULL << graph.step_count) - 1;
    for (std::size_t i = 0; i < graph.step_count; ++i) {
        const auto& step = graph.steps[i];
        if (!step.id || (step.dependencies & ~mask) || (step.dependencies & (1ULL << i))) {
            return failure<std::array<std::uint32_t, 64>>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (step.id == graph.steps[j].id) return failure<std::array<std::uint32_t, 64>>(Error::conflict);
        }
    }
    std::uint64_t finished = 0;
    std::array<std::uint32_t, 64> order{};
    for (std::size_t at = 0; at < graph.step_count; ++at) {
        std::size_t selected = graph.step_count;
        for (std::size_t i = 0; i < graph.step_count; ++i) {
            const auto bit = 1ULL << i;
            if ((finished & bit) || (graph.steps[i].dependencies & ~finished)) continue;
            if (selected == graph.step_count || graph.steps[i].id < graph.steps[selected].id) selected = i;
        }
        if (selected == graph.step_count) return failure<std::array<std::uint32_t, 64>>(Error::infeasible);
        order[at] = static_cast<std::uint32_t>(selected);
        finished |= 1ULL << selected;
    }
    return success(order);
}

// Conservative single-account planning model: supplied quote floors determine
// credited balances. Every intermediate state must remain solvent. This emits
// a typed instruction order, not calldata or an authorization. Adapter quote
// verification, destination ownership and price inputs are caller assertions.
[[nodiscard]] inline Result<ExecutionProjection> compile_execution_graph(const ExecutionGraph& graph) noexcept {
    if (!graph.plan || !graph.account || !graph.network || !graph.state_generation ||
        !graph.policy_version || graph.policy_version != graph.expected_policy_version ||
        !graph.asset_count || graph.asset_count > graph.assets.size() ||
        !graph.protocol_count || graph.protocol_count > graph.allowed_protocols.size() ||
        graph.maximum_cost_value < 0 || graph.maximum_turnover_value < 0 || graph.maximum_debt_value < 0 ||
        graph.minimum_health_factor < scale || graph.target_count != graph.asset_count ||
        graph.now_ns < graph.observed_ns || !graph.max_age_ns ||
        graph.now_ns - graph.observed_ns >= graph.max_age_ns || graph.now_ns >= graph.expires_ns) {
        return failure<ExecutionProjection>(Error::invalid);
    }
    for (std::size_t i = 0; i < graph.protocol_count; ++i) {
        if (!graph.allowed_protocols[i]) return failure<ExecutionProjection>(Error::invalid);
        for (std::size_t j = 0; j < i; ++j) {
            if (graph.allowed_protocols[i] == graph.allowed_protocols[j]) return failure<ExecutionProjection>(Error::conflict);
        }
    }
    ExecutionProjection result{};
    result.plan = graph.plan;
    result.assets_after = graph.assets;
    result.asset_count = graph.asset_count;
    result.count = graph.step_count;
    const auto state_expiry = add_time(graph.observed_ns, graph.max_age_ns);
    if (!state_expiry) return failure<ExecutionProjection>(state_expiry.error);
    result.valid_until_ns = std::min(graph.expires_ns, state_expiry.value);
    for (std::size_t i = 0; i < graph.asset_count; ++i) {
        if (validate_execution_asset(graph.assets[i]) != Error::okay) return failure<ExecutionProjection>(Error::invalid);
        for (std::size_t j = 0; j < i; ++j) {
            if (graph.assets[i].asset == graph.assets[j].asset) return failure<ExecutionProjection>(Error::conflict);
        }
    }
    const auto ordered = execution_order(graph);
    if (!ordered) return failure<ExecutionProjection>(ordered.error);
    result.order = ordered.value;
    const auto locate = [&](std::uint64_t asset) -> ExecutionAsset* {
        for (std::size_t i = 0; i < graph.asset_count; ++i) {
            if (result.assets_after[i].asset == asset) return &result.assets_after[i];
        }
        return nullptr;
    };
    const auto check_health = [&]() -> Error {
        Wide debt = 0;
        Wide liquidation = 0;
        Wide borrowing = 0;
        for (std::size_t i = 0; i < graph.asset_count; ++i) {
            const auto& asset = result.assets_after[i];
            const auto collateral = multiply(asset.reserved, asset.price_low, Rounding::floor);
            const auto owed = multiply(asset.liability, asset.price_high, Rounding::ceil);
            if (!collateral || !owed) return Error::overflow;
            const auto capacity = multiply(collateral.value, asset.liquidation_factor, Rounding::floor);
            const auto borrow_capacity = multiply(collateral.value, asset.collateral_factor, Rounding::floor);
            if (!capacity) return capacity.error;
            if (!borrow_capacity) return borrow_capacity.error;
            liquidation += capacity.value;
            borrowing += borrow_capacity.value;
            debt += owed.value;
        }
        const auto narrowed_debt = narrow(debt);
        const auto narrowed_capacity = narrow(liquidation);
        const auto narrowed_borrowing = narrow(borrowing);
        if (!narrowed_debt || !narrowed_capacity || !narrowed_borrowing) return Error::overflow;
        result.liability_value = narrowed_debt.value;
        result.liquidation_capacity = narrowed_capacity.value;
        result.borrow_capacity = narrowed_borrowing.value;
        result.debt_free = debt == 0;
        result.health_factor = 0;
        if (debt > graph.maximum_debt_value) return Error::leverage;
        if (debt > 0) {
            const auto health = ratio(narrowed_capacity.value, narrowed_debt.value, Rounding::floor);
            if (!health) return health.error;
            result.health_factor = health.value;
            if (health.value < graph.minimum_health_factor) return Error::collateral;
        }
        return Error::okay;
    };
    const auto initial_health = check_health();
    if (initial_health != Error::okay) return failure<ExecutionProjection>(initial_health);
    Fingerprint trace;
    trace.append(graph.plan);
    trace.append(graph.policy_version);
    trace.append(graph.state_generation);
    for (std::size_t at = 0; at < graph.step_count; ++at) {
        const auto& step = graph.steps[result.order[at]];
        if (step.account != graph.account || step.network != graph.network || !step.destination ||
            !step.destination_allowed || !allowed_protocol(graph, step.protocol)) {
            return failure<ExecutionProjection>(Error::unauthorized);
        }
        if (graph.now_ns >= step.expires_ns) return failure<ExecutionProjection>(Error::expired);
        result.valid_until_ns = std::min(result.valid_until_ns, step.expires_ns);
        if (step.debit < 0 || step.minimum_credit < 0 || step.maximum_credit < step.minimum_credit ||
            step.maximum_fee < 0 || step.maximum_cost_value < 0 || step.quantity_step <= 0 ||
            step.debit % step.quantity_step || step.assert_minimum < 0 || step.assert_maximum < step.assert_minimum) {
            return failure<ExecutionProjection>(Error::invalid);
        }
        auto* input = locate(step.asset_in);
        auto* output = locate(step.asset_out);
        auto* fee = locate(step.fee_asset);
        if (!input || !output || (step.maximum_fee && !fee)) return failure<ExecutionProjection>(Error::missing);
        if (step.maximum_fee) {
            if (fee->available < step.maximum_fee || fee->available - step.maximum_fee < fee->cash_floor)
                return failure<ExecutionProjection>(Error::cost);
            fee->available -= step.maximum_fee;
            const auto fee_value = multiply(step.maximum_fee, fee->price_high, Rounding::ceil);
            if (!fee_value) return failure<ExecutionProjection>(fee_value.error);
            if (fee_value.value > step.maximum_cost_value) return failure<ExecutionProjection>(Error::cost);
        }
        const auto cost = add(result.total_cost_value, step.maximum_cost_value);
        if (!cost || cost.value > graph.maximum_cost_value) return failure<ExecutionProjection>(Error::cost);
        result.total_cost_value = cost.value;
        const auto debit_value = multiply(step.debit, input->price_high, Rounding::ceil);
        if (!debit_value) return failure<ExecutionProjection>(debit_value.error);
        const auto turnover = add(result.turnover_value, debit_value.value);
        if (!turnover || turnover.value > graph.maximum_turnover_value) return failure<ExecutionProjection>(Error::cost);
        result.turnover_value = turnover.value;
        const auto spend = [&]() -> Error {
            if (input->available < step.debit || input->available - step.debit < input->cash_floor) return Error::illiquid;
            input->available -= step.debit;
            return Error::okay;
        };
        const auto credit = [&](ExecutionAsset& asset, std::int64_t value) -> Error {
            const auto next = add(asset.available, value);
            if (!next) return next.error;
            asset.available = next.value;
            return Error::okay;
        };
        Error operation = Error::okay;
        switch (step.primitive) {
        case ExecutionPrimitive::transfer:
            // This model tracks one account, so an outbound transfer has no
            // internal credit. A different account requires a separate graph.
            if (!step.debit || step.minimum_credit || step.maximum_credit || input != output) operation = Error::invalid;
            else operation = spend();
            break;
        case ExecutionPrimitive::swap:
            if (!step.quote_verified || !step.debit || !step.minimum_credit || input == output) operation = Error::invalid;
            else {
                operation = spend();
                if (operation == Error::okay) operation = credit(*output, step.minimum_credit);
            }
            break;
        case ExecutionPrimitive::supply:
            if (!step.debit || input != output || step.minimum_credit != step.debit || step.maximum_credit != step.debit)
                operation = Error::invalid;
            else {
                operation = spend();
                const auto next = add(input->reserved, step.debit);
                if (!next) operation = next.error;
                else if (operation == Error::okay) input->reserved = next.value;
            }
            break;
        case ExecutionPrimitive::redeem:
            if (!step.quote_verified || !step.debit || input != output || input->reserved < step.debit ||
                step.maximum_credit > step.debit) operation = Error::invalid;
            else {
                input->reserved -= step.debit;
                operation = credit(*output, step.minimum_credit);
            }
            break;
        case ExecutionPrimitive::borrow:
            if (!step.quote_verified || !step.debit || input != output || step.minimum_credit != step.debit ||
                step.maximum_credit != step.debit) operation = Error::invalid;
            else {
                const auto liability = add(input->liability, step.debit);
                if (!liability) operation = liability.error;
                else {
                    input->liability = liability.value;
                    operation = credit(*input, step.debit);
                }
            }
            break;
        case ExecutionPrimitive::repay:
            if (!step.debit || input != output || step.minimum_credit || step.maximum_credit || input->liability < step.debit)
                operation = Error::invalid;
            else {
                operation = spend();
                if (operation == Error::okay) input->liability -= step.debit;
            }
            break;
        case ExecutionPrimitive::cancel:
            if (step.debit || step.minimum_credit || step.maximum_credit) operation = Error::invalid;
            break;
        case ExecutionPrimitive::assert_balance:
            if (step.debit || step.minimum_credit || step.maximum_credit || step.maximum_fee || input != output)
                operation = Error::invalid;
            else if (input->available < step.assert_minimum || input->available > step.assert_maximum)
                operation = Error::infeasible;
            break;
        default:
            operation = Error::unsupported;
            break;
        }
        if (operation != Error::okay) return failure<ExecutionProjection>(operation);
        const auto health = check_health();
        if (health != Error::okay) return failure<ExecutionProjection>(health);
        if (step.primitive == ExecutionPrimitive::borrow && result.liability_value > result.borrow_capacity)
            return failure<ExecutionProjection>(Error::collateral);
        trace.append(step.id);
        trace.append(static_cast<std::uint32_t>(step.primitive));
        trace.append(step.asset_in);
        trace.append(step.asset_out);
        trace.append(static_cast<std::uint64_t>(step.debit));
        trace.append(static_cast<std::uint64_t>(step.minimum_credit));
    }
    for (std::size_t i = 0; i < graph.target_count; ++i) {
        const auto& target = graph.targets[i];
        const auto* asset = locate(target.asset);
        if (!asset || target.minimum_available < 0 || target.maximum_available < target.minimum_available ||
            target.minimum_reserved < 0 || target.maximum_reserved < target.minimum_reserved || target.maximum_liability < 0)
            return failure<ExecutionProjection>(Error::invalid);
        for (std::size_t j = 0; j < i; ++j) {
            if (target.asset == graph.targets[j].asset) return failure<ExecutionProjection>(Error::conflict);
        }
        if (asset->available < target.minimum_available || asset->available > target.maximum_available ||
            asset->reserved < target.minimum_reserved || asset->reserved > target.maximum_reserved ||
            asset->liability > target.maximum_liability) return failure<ExecutionProjection>(Error::infeasible);
    }
    result.diagnostic_fingerprint = trace.value();
    return success(result);
}

struct ExecutionAlternative {
    std::uint64_t plan{};
    Error error{Error::unknown};
    std::int64_t cost_value{};
    std::int64_t turnover_value{};
    std::uint64_t valid_until_ns{};
};

struct ExecutionComparison {
    ExecutionProjection best{};
    std::array<ExecutionAlternative, 8> alternatives{};
    std::uint32_t examined{};
    std::uint32_t feasible{};
};

[[nodiscard]] inline Result<ExecutionComparison> compare_execution_graphs(std::span<const ExecutionGraph> graphs) noexcept {
    if (graphs.empty() || graphs.size() > 8) return failure<ExecutionComparison>(Error::invalid);
    ExecutionComparison result{};
    for (std::size_t i = 0; i < graphs.size(); ++i) {
        const auto& graph = graphs[i];
        if (!graph.asset_count || graph.asset_count > graph.assets.size() ||
            graph.target_count > graph.targets.size()) return failure<ExecutionComparison>(Error::capacity);
        // Comparing unrelated accounts or state versions as route alternatives
        // would produce a cost ranking with no executable interpretation.
        if (graph.account != graphs[0].account || graph.network != graphs[0].network ||
            graph.policy_version != graphs[0].policy_version || graph.state_generation != graphs[0].state_generation ||
            graph.asset_count != graphs[0].asset_count || graph.target_count != graphs[0].target_count ||
            graph.maximum_cost_value != graphs[0].maximum_cost_value ||
            graph.maximum_turnover_value != graphs[0].maximum_turnover_value ||
            graph.maximum_debt_value != graphs[0].maximum_debt_value ||
            graph.minimum_health_factor != graphs[0].minimum_health_factor)
            return failure<ExecutionComparison>(Error::domain);
        for (std::size_t at = 0; at < graph.asset_count; ++at) {
            const auto& asset = graph.assets[at];
            const auto& base = graphs[0].assets[at];
            if (asset.asset != base.asset || asset.available != base.available || asset.reserved != base.reserved ||
                asset.liability != base.liability || asset.price_low != base.price_low || asset.price_high != base.price_high ||
                asset.collateral_factor != base.collateral_factor || asset.liquidation_factor != base.liquidation_factor ||
                asset.cash_floor != base.cash_floor) return failure<ExecutionComparison>(Error::domain);
        }
        for (std::size_t at = 0; at < graph.target_count; ++at) {
            const auto& target = graph.targets[at];
            const auto& base = graphs[0].targets[at];
            if (target.asset != base.asset || target.minimum_available != base.minimum_available ||
                target.maximum_available != base.maximum_available || target.minimum_reserved != base.minimum_reserved ||
                target.maximum_reserved != base.maximum_reserved || target.maximum_liability != base.maximum_liability)
                return failure<ExecutionComparison>(Error::domain);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (graph.plan == graphs[j].plan) return failure<ExecutionComparison>(Error::conflict);
        }
        const auto candidate = compile_execution_graph(graph);
        auto& alternative = result.alternatives[i];
        alternative.plan = graph.plan;
        alternative.error = candidate.error;
        ++result.examined;
        if (!candidate) continue;
        alternative.cost_value = candidate.value.total_cost_value;
        alternative.turnover_value = candidate.value.turnover_value;
        alternative.valid_until_ns = candidate.value.valid_until_ns;
        if (!result.feasible || candidate.value.total_cost_value < result.best.total_cost_value ||
            (candidate.value.total_cost_value == result.best.total_cost_value &&
             (candidate.value.turnover_value < result.best.turnover_value ||
              (candidate.value.turnover_value == result.best.turnover_value && graph.plan < result.best.plan)))) {
            result.best = candidate.value;
        }
        ++result.feasible;
    }
    if (!result.feasible) return failure<ExecutionComparison>(Error::infeasible);
    return success(result);
}

} // namespace machine::economics
