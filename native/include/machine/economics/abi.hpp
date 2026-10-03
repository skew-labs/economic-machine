#pragma once

#include "machine/economics/allocation.hpp"
#include "machine/economics/amm.hpp"
#include "machine/economics/decision.hpp"
#include "machine/economics/routing.hpp"
#include "machine/economics/statistics.hpp"
#include "machine/economics/transition.hpp"
#include "machine/economics/strategy.hpp"
#include "machine/economics/execution.hpp"

namespace machine::economics {

inline constexpr std::uint32_t economics_abi = 1;

struct OracleRequest {
    OraclePolicy policy{};
    std::array<OracleObservation, 16> observations{};
    std::uint32_t count{};
};

struct HealthRequest {
    std::array<CollateralAsset, 32> collateral{};
    std::array<DebtAsset, 32> debts{};
    std::uint32_t collateral_count{};
    std::uint32_t debt_count{};
};

struct DerivativeRecoveryRequest {
    LinearPosition position{};
    RecoveryPolicy policy{};
    std::array<ExecutionVenue, 16> venues{};
    std::uint32_t count{};
    Envelope state{};
};

struct RepaymentRequest {
    Health health{};
    RepayPolicy policy{};
    Envelope state{};
};

struct RebalanceRequest {
    RebalanceTerms terms{};
    Envelope state{};
};

struct SwapRequest {
    ConstantProductPool pool{};
    std::int64_t amount{};
    bool exact_output{};
};

struct AllocationRequest {
    std::array<Product, max_products> products{};
    std::uint32_t count{};
    AllocationPolicy policy{};
    GridRequest grid{};
};

struct DomainDescriptor {
    std::uint32_t operation{};
    std::uint32_t version{};
    std::uint64_t input_size{};
    std::uint64_t output_size{};
};

enum class Operation : std::uint32_t {
    oracle = 1,
    lending_rates,
    yield,
    health,
    derivative_risk,
    funding,
    recovery,
    repayment,
    rebalance,
    swap,
    route,
    allocation,
    venue_route,
    tail_risk,
    return_statistics,
    factor_risk,
    transition,
    state_frame,
    strategy,
    execution,
};

struct StateFrameRequest {
    std::array<FactDelta, 64> deltas{};
    std::array<Dependency, 32> dependencies{};
    std::uint32_t delta_count{};
    std::uint32_t dependency_count{};
    std::uint64_t now_ns{};
    std::uint64_t maximum_skew_ns{};
};

struct StrategyRequest {
    StrategyProgram program{};
    StrategyFrame frame{};
};

struct VenueRouteRequest {
    std::array<VenueDepth, 16> venues{};
    std::uint32_t count{};
    RoutePolicy policy{};
};

struct TailRiskRequest {
    std::array<WeightedScenario, 256> scenarios{};
    std::uint32_t count{};
    std::uint32_t confidence_bps{};
};

struct ReturnStatisticsRequest {
    std::array<PriceSample, 256> samples{};
    std::uint32_t count{};
    std::uint64_t expected_interval_ns{};
    std::uint64_t interval_tolerance_ns{};
};

struct FactorRiskRequest {
    std::array<FactorExposure, 32> assets{};
    std::array<FactorBound, 16> bounds{};
    std::uint32_t asset_count{};
    std::uint32_t factor_count{};
};

struct TransitionRequest {
    EconomicProcess process{};
    TransitionInput event{};
};

struct TransitionOutput {
    EconomicProcess process{};
    TransitionReceipt receipt{};
};

struct FundingRequest {
    std::int64_t signed_quantity{};
    std::int64_t settlement_price{};
    std::int64_t funding_rate{};
};

static_assert(std::is_trivially_copyable_v<DerivativeRecoveryRequest>);
static_assert(std::is_standard_layout_v<DerivativeRecoveryRequest>);
static_assert(std::is_trivially_copyable_v<AllocationRequest>);
static_assert(sizeof(Error) == sizeof(std::uint32_t));

} // namespace machine::economics

extern "C" {
std::uint32_t machine_economics_abi() noexcept;
int machine_economics_descriptor(std::uint32_t, machine::economics::DomainDescriptor*) noexcept;
int machine_economics_evaluate(std::uint32_t, const void*, std::size_t, void*, std::size_t) noexcept;
}
