#include "machine/economics/abi.hpp"

#include <cstring>

namespace {

using namespace machine::economics;

// A C caller may supply arbitrary bytes, unlike the JSON bridge. Loading an
// invalid C++ bool representation is undefined behavior; inspect bytes before
// any evaluator reads an input flag. Output bools are created by native code.
bool binary_flag(const bool& flag) noexcept {
    static_assert(sizeof(bool) == 1);
    unsigned char byte{};
    std::memcpy(&byte, &flag, sizeof(byte));
    return byte <= 1;
}

template <class T>
bool valid_flags(const T&) noexcept { return true; }

bool valid_flags(const OracleRequest& request) noexcept {
    for (const auto& observation : request.observations) {
        if (!binary_flag(observation.adapter_verified)) return false;
    }
    return true;
}

bool valid_flags(const DerivativeRecoveryRequest& request) noexcept {
    for (const auto& venue : request.venues) {
        if (!binary_flag(venue.allowed)) return false;
    }
    return true;
}

bool valid_flags(const RepaymentRequest& request) noexcept {
    return binary_flag(request.health.debt_free) && binary_flag(request.health.liquidatable);
}

bool valid_flags(const SwapRequest& request) noexcept { return binary_flag(request.exact_output); }

bool valid_flags(const AllocationRequest& request) noexcept {
    for (const auto& product : request.products) {
        if (!binary_flag(product.liquid) || !binary_flag(product.enabled)) return false;
    }
    return true;
}

bool valid_flags(const ActionEnvelope& input) noexcept { return binary_flag(input.destination_allowed); }
bool valid_flags(const SimulationEvidence& input) noexcept {
    return binary_flag(input.adapter_verified) && binary_flag(input.successful);
}
bool valid_flags(const AuthorizationEvidence& input) noexcept { return binary_flag(input.externally_verified); }
bool valid_flags(const SettlementEvidence& input) noexcept {
    return binary_flag(input.successful) && binary_flag(input.independent_sources_agree) && binary_flag(input.adapter_verified);
}

bool valid_flags(const TransitionRequest& request) noexcept {
    return valid_flags(request.process.action) && valid_flags(request.process.simulation) &&
        valid_flags(request.process.authorization) && valid_flags(request.process.settlement) &&
        binary_flag(request.process.policy_paused) && binary_flag(request.process.needs_resolver) &&
        valid_flags(request.event.action) && valid_flags(request.event.simulation) &&
        valid_flags(request.event.authorization) && valid_flags(request.event.settlement);
}

bool valid_flags(const StrategyRequest& request) noexcept { return binary_flag(request.frame.policy_active); }

bool valid_flags(const ExecutionGraph& graph) noexcept {
    for (const auto& step : graph.steps) {
        if (!binary_flag(step.destination_allowed) || !binary_flag(step.quote_verified)) return false;
    }
    return true;
}

template <class Input, class Output>
DomainDescriptor descriptor(Operation operation) noexcept {
    return {static_cast<std::uint32_t>(operation), economics_abi, sizeof(Input), sizeof(Output)};
}

template <class Input, class Output, class Evaluator>
int invoke(
    const void* input,
    std::size_t input_size,
    void* output,
    std::size_t output_size,
    Evaluator evaluate
) noexcept {
    if (!input || !output || input_size != sizeof(Input) || output_size != sizeof(Output)) {
        return static_cast<int>(Error::invalid);
    }
    // memcpy handles an unaligned external pointer without undefined behavior.
    // Input and output may alias: evaluation uses the independent fixed buffer.
    Input request{};
    std::memcpy(&request, input, sizeof(request));
    Output result{};
    if (!valid_flags(request)) {
        std::memcpy(output, &result, sizeof(result));
        return static_cast<int>(Error::invalid);
    }
    const auto evaluated = evaluate(request);
    if (evaluated) {
        result = evaluated.value;
    }
    std::memcpy(output, &result, sizeof(result));
    return static_cast<int>(evaluated.error);
}

Result<OraclePrice> oracle(const OracleRequest& request) noexcept {
    if (request.count > request.observations.size()) {
        return failure<OraclePrice>(Error::capacity);
    }
    return aggregate_price({request.observations.data(), request.count}, request.policy);
}

Result<Health> health(const HealthRequest& request) noexcept {
    if (request.collateral_count > request.collateral.size() || request.debt_count > request.debts.size()) {
        return failure<Health>(Error::capacity);
    }
    return lending_health({request.collateral.data(), request.collateral_count},
        {request.debts.data(), request.debt_count});
}

Result<RecoveryChoice> recovery(const DerivativeRecoveryRequest& request) noexcept {
    if (request.count > request.venues.size()) {
        return failure<RecoveryChoice>(Error::capacity);
    }
    return decide_derivative_recovery(request.position, request.policy,
        {request.venues.data(), request.count}, request.state);
}

Result<GridSearch> allocation(const AllocationRequest& request) noexcept {
    if (request.count > request.products.size()) {
        return failure<GridSearch>(Error::capacity);
    }
    return search_allocations({request.products.data(), request.count}, request.policy, request.grid);
}

} // namespace

extern "C" {

std::uint32_t machine_economics_abi() noexcept {
    return machine::economics::economics_abi;
}

int machine_economics_descriptor(
    std::uint32_t operation,
    machine::economics::DomainDescriptor* output
) noexcept {
    if (!output) {
        return static_cast<int>(Error::invalid);
    }
    switch (static_cast<Operation>(operation)) {
    case Operation::oracle:
        *output = descriptor<OracleRequest, OraclePrice>(Operation::oracle);
        break;
    case Operation::lending_rates:
        *output = descriptor<LendingMarket, LendingRates>(Operation::lending_rates);
        break;
    case Operation::yield:
        *output = descriptor<YieldTerms, YieldEstimate>(Operation::yield);
        break;
    case Operation::health:
        *output = descriptor<HealthRequest, Health>(Operation::health);
        break;
    case Operation::derivative_risk:
        *output = descriptor<LinearPosition, PositionRisk>(Operation::derivative_risk);
        break;
    case Operation::funding:
        *output = descriptor<FundingRequest, FundingCashFlow>(Operation::funding);
        break;
    case Operation::recovery:
        *output = descriptor<DerivativeRecoveryRequest, RecoveryChoice>(Operation::recovery);
        break;
    case Operation::repayment:
        *output = descriptor<RepaymentRequest, RepayChoice>(Operation::repayment);
        break;
    case Operation::rebalance:
        *output = descriptor<RebalanceRequest, RebalanceChoice>(Operation::rebalance);
        break;
    case Operation::swap:
        *output = descriptor<SwapRequest, SwapQuote>(Operation::swap);
        break;
    case Operation::route:
        *output = descriptor<SwapRoute, RouteQuote>(Operation::route);
        break;
    case Operation::allocation:
        *output = descriptor<AllocationRequest, GridSearch>(Operation::allocation);
        break;
    case Operation::venue_route:
        *output = descriptor<VenueRouteRequest, SplitPlan>(Operation::venue_route);
        break;
    case Operation::tail_risk:
        *output = descriptor<TailRiskRequest, TailRisk>(Operation::tail_risk);
        break;
    case Operation::return_statistics:
        *output = descriptor<ReturnStatisticsRequest, ReturnStatistics>(Operation::return_statistics);
        break;
    case Operation::factor_risk:
        *output = descriptor<FactorRiskRequest, FactorRisk>(Operation::factor_risk);
        break;
    case Operation::transition:
        *output = descriptor<TransitionRequest, TransitionOutput>(Operation::transition);
        break;
    case Operation::state_frame:
        *output = descriptor<StateFrameRequest, DependencyFrame>(Operation::state_frame);
        break;
    case Operation::strategy:
        *output = descriptor<StrategyRequest, StrategyResult>(Operation::strategy);
        break;
    case Operation::execution:
        *output = descriptor<ExecutionGraph, ExecutionProjection>(Operation::execution);
        break;
    default:
        *output = {};
        return static_cast<int>(Error::unsupported);
    }
    return static_cast<int>(Error::okay);
}

int machine_economics_evaluate(
    std::uint32_t operation,
    const void* input,
    std::size_t input_size,
    void* output,
    std::size_t output_size
) noexcept {
    switch (static_cast<Operation>(operation)) {
    case Operation::oracle:
        return invoke<OracleRequest, OraclePrice>(input, input_size, output, output_size, oracle);
    case Operation::lending_rates:
        return invoke<LendingMarket, LendingRates>(input, input_size, output, output_size, lending_rates);
    case Operation::yield:
        return invoke<YieldTerms, YieldEstimate>(input, input_size, output, output_size, forward_yield);
    case Operation::health:
        return invoke<HealthRequest, Health>(input, input_size, output, output_size, health);
    case Operation::derivative_risk:
        return invoke<LinearPosition, PositionRisk>(input, input_size, output, output_size, linear_risk);
    case Operation::funding:
        return invoke<FundingRequest, FundingCashFlow>(input, input_size, output, output_size,
            [](const FundingRequest& request) {
                return linear_funding(request.signed_quantity, request.settlement_price, request.funding_rate);
            });
    case Operation::recovery:
        return invoke<DerivativeRecoveryRequest, RecoveryChoice>(input, input_size, output, output_size, recovery);
    case Operation::repayment:
        return invoke<RepaymentRequest, RepayChoice>(input, input_size, output, output_size,
            [](const RepaymentRequest& request) {
                return decide_repayment(request.health, request.policy, request.state);
            });
    case Operation::rebalance:
        return invoke<RebalanceRequest, RebalanceChoice>(input, input_size, output, output_size,
            [](const RebalanceRequest& request) {
                return decide_rebalance(request.terms, request.state);
            });
    case Operation::swap:
        return invoke<SwapRequest, SwapQuote>(input, input_size, output, output_size,
            [](const SwapRequest& request) {
                return request.exact_output ? exact_output_swap(request.pool, request.amount)
                    : exact_input_swap(request.pool, request.amount);
            });
    case Operation::route:
        return invoke<SwapRoute, RouteQuote>(input, input_size, output, output_size, simulate_route);
    case Operation::allocation:
        return invoke<AllocationRequest, GridSearch>(input, input_size, output, output_size, allocation);
    case Operation::venue_route:
        return invoke<VenueRouteRequest, SplitPlan>(input, input_size, output, output_size,
            [](const VenueRouteRequest& request) {
                if (request.count > request.venues.size()) {
                    return failure<SplitPlan>(Error::capacity);
                }
                return best_single_venue({request.venues.data(), request.count}, request.policy);
            });
    case Operation::tail_risk:
        return invoke<TailRiskRequest, TailRisk>(input, input_size, output, output_size,
            [](const TailRiskRequest& request) {
                if (request.count > request.scenarios.size()) {
                    return failure<TailRisk>(Error::capacity);
                }
                return scenario_tail_risk({request.scenarios.data(), request.count}, request.confidence_bps);
            });
    case Operation::return_statistics:
        return invoke<ReturnStatisticsRequest, ReturnStatistics>(input, input_size, output, output_size,
            [](const ReturnStatisticsRequest& request) {
                if (request.count > request.samples.size()) {
                    return failure<ReturnStatistics>(Error::capacity);
                }
                return return_statistics({request.samples.data(), request.count},
                    request.expected_interval_ns, request.interval_tolerance_ns);
            });
    case Operation::factor_risk:
        return invoke<FactorRiskRequest, FactorRisk>(input, input_size, output, output_size,
            [](const FactorRiskRequest& request) {
                if (request.asset_count > request.assets.size() || request.factor_count > request.bounds.size()) {
                    return failure<FactorRisk>(Error::capacity);
                }
                return factor_risk({request.assets.data(), request.asset_count},
                    {request.bounds.data(), request.factor_count});
            });
    case Operation::transition:
        return invoke<TransitionRequest, TransitionOutput>(input, input_size, output, output_size,
            [](const TransitionRequest& request) {
                TransitionOutput result{};
                result.process = request.process;
                result.receipt = advance_process(result.process, request.event);
                return Result<TransitionOutput>{result.receipt.error, result};
            });
    case Operation::state_frame:
        return invoke<StateFrameRequest, DependencyFrame>(input, input_size, output, output_size,
            [](const StateFrameRequest& request) {
                if (request.delta_count > request.deltas.size() ||
                    request.dependency_count > request.dependencies.size()) {
                    return failure<DependencyFrame>(Error::capacity);
                }
                EconomicStateStore<64, 64> state;
                const auto commit = state.apply_batch({request.deltas.data(), request.delta_count});
                if (commit.error != Error::okay) return failure<DependencyFrame>(commit.error);
                return resolve_dependencies(state, {request.dependencies.data(), request.dependency_count},
                    request.now_ns, request.maximum_skew_ns);
            });
    case Operation::strategy:
        return invoke<StrategyRequest, StrategyResult>(input, input_size, output, output_size,
            [](const StrategyRequest& request) {
                const auto result = run_strategy(request.program, request.frame);
                return Result<StrategyResult>{result.error, result};
            });
    case Operation::execution:
        return invoke<ExecutionGraph, ExecutionProjection>(input, input_size, output, output_size, compile_execution_graph);
    default:
        return static_cast<int>(Error::unsupported);
    }
}

} // extern C
