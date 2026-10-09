#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

inline constexpr std::size_t max_products = 16;
inline constexpr std::size_t max_scenarios = 16;
inline constexpr std::size_t max_groups = 16;

struct Product {
    std::uint64_t id{};
    std::uint32_t group{};
    std::int64_t current_value{};
    std::int64_t expected_net_return_bps{};
    std::int64_t min_weight_bps{};
    std::int64_t max_weight_bps{};
    std::int64_t available_capacity{};
    std::int64_t exit_capacity{};
    std::int64_t exit_delay_seconds{};
    std::int64_t turnover_cost_bps{};
    std::int64_t fixed_entry_cost{};
    std::int64_t fixed_exit_cost{};
    std::array<std::int64_t, max_scenarios> scenario_loss_bps{};
    bool liquid{};
    bool enabled{};
};

struct AllocationPolicy {
    std::int64_t capital{};
    std::int64_t cash_floor{};
    std::int64_t max_turnover{};
    std::int64_t max_cost{};
    std::int64_t max_stress_loss{};
    std::int64_t horizon_seconds{};
    std::array<std::int64_t, max_groups> group_caps_bps{};
    std::uint32_t group_count{};
    std::uint32_t scenario_count{};
};

struct AllocationCandidate {
    std::uint64_t id{};
    std::array<std::int64_t, max_products> weights_bps{};
};

struct AllocationScore {
    std::uint64_t candidate_id{};
    std::array<std::int64_t, max_products> amounts{};
    std::array<std::int64_t, max_scenarios> scenario_losses{};
    std::array<std::int64_t, max_groups> group_weights{};
    std::int64_t liquid_value{};
    std::int64_t turnover{};
    std::int64_t expected_income{};
    std::int64_t execution_cost{};
    std::int64_t expected_net_income{};
    std::int64_t worst_stress_loss{};
    std::uint32_t worst_scenario{};
};

[[nodiscard]] inline Error validate_allocation_domain(
    std::span<const Product> products,
    const AllocationPolicy& policy
) noexcept {
    if (products.empty() || products.size() > max_products || policy.capital <= 0 ||
        policy.cash_floor < 0 || policy.cash_floor > policy.capital || policy.max_turnover < 0 ||
        policy.max_cost < 0 || policy.max_stress_loss < 0 || policy.horizon_seconds <= 0 ||
        !policy.group_count || policy.group_count > max_groups ||
        !policy.scenario_count || policy.scenario_count > max_scenarios) {
        return Error::invalid;
    }
    for (std::size_t i = 0; i < policy.group_count; ++i) {
        if (policy.group_caps_bps[i] < 0 || policy.group_caps_bps[i] > bps_scale) {
            return Error::invalid;
        }
    }
    Wide current = 0;
    std::int64_t lower = 0;
    std::int64_t upper = 0;
    for (std::size_t i = 0; i < products.size(); ++i) {
        const auto& product = products[i];
        if (!product.id || product.group >= policy.group_count || product.current_value < 0 ||
            product.expected_net_return_bps < -bps_scale || product.expected_net_return_bps > 10 * bps_scale ||
            product.min_weight_bps < 0 || product.max_weight_bps < product.min_weight_bps ||
            product.max_weight_bps > bps_scale || product.available_capacity < 0 || product.exit_capacity < 0 ||
            product.exit_delay_seconds < 0 || product.turnover_cost_bps < 0 ||
            product.turnover_cost_bps > bps_scale || product.fixed_entry_cost < 0 || product.fixed_exit_cost < 0) {
            return Error::invalid;
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (products[j].id == product.id) {
                return Error::conflict;
            }
        }
        for (std::size_t scenario = 0; scenario < policy.scenario_count; ++scenario) {
            if (product.scenario_loss_bps[scenario] < -bps_scale ||
                product.scenario_loss_bps[scenario] > bps_scale) {
                return Error::invalid;
            }
        }
        if (!product.enabled && product.min_weight_bps) {
            return Error::infeasible;
        }
        current += product.current_value;
        lower += product.min_weight_bps;
        upper += product.enabled ? product.max_weight_bps : 0;
    }
    if (current != policy.capital) {
        return Error::domain;
    }
    if (lower > bps_scale || upper < bps_scale) {
        return Error::infeasible;
    }
    return Error::okay;
}

// All products are denominated in the same declared base asset. Prices and
// prospective returns are caller assumptions; this function only proves
// feasibility and deterministic ranking under those assumptions.
[[nodiscard]] inline Result<AllocationScore> score_allocation(
    std::span<const Product> products,
    const AllocationPolicy& policy,
    const AllocationCandidate& candidate
) noexcept {
    const auto domain = validate_allocation_domain(products, policy);
    if (domain != Error::okay) {
        return failure<AllocationScore>(domain);
    }
    if (!candidate.id) {
        return failure<AllocationScore>(Error::invalid);
    }
    std::int64_t sum = 0;
    for (std::size_t i = 0; i < products.size(); ++i) {
        const auto weight = candidate.weights_bps[i];
        const auto& product = products[i];
        if (weight < product.min_weight_bps || weight > product.max_weight_bps ||
            (!product.enabled && weight)) {
            return failure<AllocationScore>(Error::infeasible);
        }
        sum += weight;
    }
    for (std::size_t i = products.size(); i < max_products; ++i) {
        if (candidate.weights_bps[i]) {
            return failure<AllocationScore>(Error::domain);
        }
    }
    if (sum != bps_scale) {
        return failure<AllocationScore>(Error::infeasible);
    }
    AllocationScore result{};
    result.candidate_id = candidate.id;
    std::array<std::int64_t, max_products> remainders{};
    std::int64_t allocated = 0;
    for (std::size_t i = 0; i < products.size(); ++i) {
        const auto product = static_cast<Wide>(policy.capital) * candidate.weights_bps[i];
        result.amounts[i] = static_cast<std::int64_t>(product / bps_scale);
        remainders[i] = static_cast<std::int64_t>(product % bps_scale);
        allocated += result.amounts[i];
    }
    // Largest-remainder apportionment conserves every micro-unit. Ties use
    // the immutable product ID and therefore do not depend on input ordering.
    std::array<std::size_t, max_products> order{};
    for (std::size_t i = 0; i < products.size(); ++i) {
        order[i] = i;
    }
    std::sort(order.begin(), order.begin() + products.size(), [&](auto a, auto b) {
        return remainders[a] > remainders[b] ||
            (remainders[a] == remainders[b] && products[a].id < products[b].id);
    });
    const auto leftover = policy.capital - allocated;
    if (leftover < 0 || leftover >= static_cast<std::int64_t>(products.size())) {
        return failure<AllocationScore>(Error::invalid);
    }
    for (std::int64_t i = 0; i < leftover; ++i) {
        ++result.amounts[order[static_cast<std::size_t>(i)]];
    }
    for (std::size_t i = 0; i < products.size(); ++i) {
        const auto& product = products[i];
        const auto amount = result.amounts[i];
        result.group_weights[product.group] += candidate.weights_bps[i];
        const auto movement = absolute(amount - product.current_value);
        if (!movement) {
            return failure<AllocationScore>(movement.error);
        }
        if (amount > product.current_value && amount - product.current_value > product.available_capacity) {
            return failure<AllocationScore>(Error::illiquid);
        }
        if (amount < product.current_value && (product.current_value - amount > product.exit_capacity ||
            product.exit_delay_seconds > policy.horizon_seconds)) {
            return failure<AllocationScore>(Error::illiquid);
        }
        const auto turnover = add(result.turnover, movement.value);
        const auto income = basis_points(amount, product.expected_net_return_bps, Rounding::floor);
        const auto cost = basis_points(movement.value, product.turnover_cost_bps, Rounding::ceil);
        if (!turnover || !income || !cost) {
            return failure<AllocationScore>(Error::overflow);
        }
        result.turnover = turnover.value;
        const auto total_income = add(result.expected_income, income.value);
        const auto fixed = amount > product.current_value ? product.fixed_entry_cost
            : amount < product.current_value ? product.fixed_exit_cost : 0;
        const auto total_cost = narrow(static_cast<Wide>(result.execution_cost) + cost.value + fixed);
        if (!total_income || !total_cost) {
            return failure<AllocationScore>(Error::overflow);
        }
        result.expected_income = total_income.value;
        result.execution_cost = total_cost.value;
        if (product.liquid) {
            const auto liquid = add(result.liquid_value, amount);
            if (!liquid) {
                return failure<AllocationScore>(liquid.error);
            }
            result.liquid_value = liquid.value;
        }
        for (std::size_t scenario = 0; scenario < policy.scenario_count; ++scenario) {
            const auto loss = basis_points(amount, product.scenario_loss_bps[scenario], Rounding::ceil);
            if (!loss) {
                return failure<AllocationScore>(loss.error);
            }
            const auto total = add(result.scenario_losses[scenario], loss.value);
            if (!total) {
                return failure<AllocationScore>(total.error);
            }
            result.scenario_losses[scenario] = total.value;
        }
    }
    if (result.liquid_value < policy.cash_floor || result.turnover > policy.max_turnover) {
        return failure<AllocationScore>(Error::infeasible);
    }
    if (result.execution_cost > policy.max_cost) {
        return failure<AllocationScore>(Error::cost);
    }
    for (std::size_t group = 0; group < policy.group_count; ++group) {
        if (result.group_weights[group] > policy.group_caps_bps[group]) {
            return failure<AllocationScore>(Error::concentration);
        }
    }
    result.worst_stress_loss = INT64_MIN;
    for (std::size_t scenario = 0; scenario < policy.scenario_count; ++scenario) {
        if (result.scenario_losses[scenario] > result.worst_stress_loss) {
            result.worst_stress_loss = result.scenario_losses[scenario];
            result.worst_scenario = static_cast<std::uint32_t>(scenario);
        }
    }
    if (result.worst_stress_loss > policy.max_stress_loss) {
        return failure<AllocationScore>(Error::collateral);
    }
    const auto net = subtract(result.expected_income, result.execution_cost);
    if (!net) {
        return failure<AllocationScore>(net.error);
    }
    result.expected_net_income = net.value;
    return success(result);
}

[[nodiscard]] inline bool better_allocation(
    const AllocationScore& a,
    const AllocationScore& b
) noexcept {
    if (a.expected_net_income != b.expected_net_income) {
        return a.expected_net_income > b.expected_net_income;
    }
    if (a.worst_stress_loss != b.worst_stress_loss) {
        return a.worst_stress_loss < b.worst_stress_loss;
    }
    if (a.turnover != b.turnover) {
        return a.turnover < b.turnover;
    }
    return a.candidate_id < b.candidate_id;
}

struct CandidateComparison {
    std::array<AllocationScore, 3> plans{};
    std::uint32_t count{};
    std::uint32_t examined{};
    std::uint32_t rejected{};
    bool complete{};
};

[[nodiscard]] inline Result<CandidateComparison> compare_allocations(
    std::span<const Product> products,
    const AllocationPolicy& policy,
    std::span<const AllocationCandidate> candidates
) noexcept {
    if (candidates.empty() || candidates.size() > 4096) {
        return failure<CandidateComparison>(Error::capacity);
    }
    const auto domain = validate_allocation_domain(products, policy);
    if (domain != Error::okay) {
        return failure<CandidateComparison>(domain);
    }
    CandidateComparison result{};
    for (std::size_t i = 0; i < candidates.size(); ++i) {
        if (!candidates[i].id) {
            return failure<CandidateComparison>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (candidates[i].id == candidates[j].id) {
                return failure<CandidateComparison>(Error::conflict);
            }
        }
        ++result.examined;
        const auto score = score_allocation(products, policy, candidates[i]);
        if (!score) {
            if (score.error == Error::invalid || score.error == Error::domain || score.error == Error::overflow) {
                return failure<CandidateComparison>(score.error);
            }
            ++result.rejected;
            continue;
        }
        std::size_t at = result.count;
        for (std::size_t j = 0; j < result.count; ++j) {
            if (better_allocation(score.value, result.plans[j])) {
                at = j;
                break;
            }
        }
        if (at >= result.plans.size()) {
            continue;
        }
        const auto last = std::min<std::size_t>(result.count, result.plans.size() - 1);
        for (auto j = last; j > at; --j) {
            result.plans[j] = result.plans[j - 1];
        }
        result.plans[at] = score.value;
        result.count = std::min<std::uint32_t>(result.count + 1, result.plans.size());
    }
    result.complete = true;
    if (!result.count) {
        return failure<CandidateComparison>(Error::infeasible);
    }
    return success(result);
}

struct GridRequest {
    std::int64_t step_bps{};
    std::uint64_t maximum_nodes{};
};

struct GridSearch {
    CandidateComparison comparison{};
    std::uint64_t nodes{};
    std::uint64_t scored{};
    std::int64_t step_bps{};
};

// Iterative, fixed-stack simplex enumeration. Completeness is restricted to
// this explicitly declared finite grid. Reaching the work cap returns no plan;
// a partially searched grid cannot claim an optimum.
[[nodiscard]] inline Result<GridSearch> search_allocations(
    std::span<const Product> products,
    const AllocationPolicy& policy,
    const GridRequest& request
) noexcept {
    const auto domain = validate_allocation_domain(products, policy);
    if (domain != Error::okay) {
        return failure<GridSearch>(domain);
    }
    if (request.step_bps <= 0 || request.step_bps > bps_scale || bps_scale % request.step_bps ||
        !request.maximum_nodes || request.maximum_nodes > 1'000'000) {
        return failure<GridSearch>(Error::invalid);
    }
    for (const auto& product : products) {
        if (product.min_weight_bps % request.step_bps || product.max_weight_bps % request.step_bps) {
            return failure<GridSearch>(Error::domain);
        }
    }
    GridSearch result{};
    result.step_bps = request.step_bps;
    AllocationCandidate candidate{};
    std::array<std::int64_t, max_products> next{};
    std::array<std::int64_t, max_products> remaining{};
    std::size_t depth = 0;
    remaining[0] = bps_scale;
    next[0] = products[0].min_weight_bps;
    std::uint64_t candidate_id = 0;
    while (true) {
        if (++result.nodes > request.maximum_nodes) {
            return failure<GridSearch>(Error::incomplete);
        }
        const auto& product = products[depth];
        const auto upper = product.enabled ? product.max_weight_bps : 0;
        const auto weight = next[depth];
        if (weight > upper || weight > remaining[depth]) {
            if (!depth) {
                break;
            }
            --depth;
            next[depth] += request.step_bps;
            continue;
        }
        candidate.weights_bps[depth] = weight;
        if (depth + 1 == products.size()) {
            if (weight == remaining[depth]) {
                candidate.id = ++candidate_id;
                ++result.scored;
                ++result.comparison.examined;
                const auto score = score_allocation(products, policy, candidate);
                if (!score) {
                    if (score.error == Error::overflow || score.error == Error::invalid || score.error == Error::domain) {
                        return failure<GridSearch>(score.error);
                    }
                    ++result.comparison.rejected;
                } else {
                    std::size_t at = result.comparison.count;
                    for (std::size_t i = 0; i < result.comparison.count; ++i) {
                        if (better_allocation(score.value, result.comparison.plans[i])) {
                            at = i;
                            break;
                        }
                    }
                    if (at < result.comparison.plans.size()) {
                        const auto last = std::min<std::size_t>(result.comparison.count, 2);
                        for (auto i = last; i > at; --i) {
                            result.comparison.plans[i] = result.comparison.plans[i - 1];
                        }
                        result.comparison.plans[at] = score.value;
                        result.comparison.count = std::min<std::uint32_t>(result.comparison.count + 1, 3);
                    }
                }
            }
            next[depth] += request.step_bps;
            continue;
        }
        remaining[depth + 1] = remaining[depth] - weight;
        ++depth;
        next[depth] = products[depth].min_weight_bps;
    }
    if (!result.comparison.count) {
        return failure<GridSearch>(Error::infeasible);
    }
    result.comparison.complete = true;
    return success(result);
}

} // namespace machine::economics
