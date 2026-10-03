#include "machine/economics/abi.hpp"
#include "machine/economics/runtime.hpp"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>

using namespace machine::economics;

namespace {
unsigned checks = 0;

void check(bool value, const char* label) {
    ++checks;
    if (!value) {
        std::cerr << "Failed: " << label << '\n';
        std::exit(1);
    }
}

Envelope envelope() {
    return {1, 1, 100, 200, 1000, 1000, 1, 1};
}

void arithmetic() {
    check(!add(INT64_MAX, 1), "checked sum overflow");
    check(!subtract(INT64_MIN, 1), "checked difference overflow");
    check(!absolute(INT64_MIN), "absolute minimum overflow");
    check(divide_wide(-3, 2, Rounding::floor).value == -2, "negative floor");
    check(divide_wide(-3, 2, Rounding::ceil).value == -1, "negative ceil");
    check(divide_wide(3, 2, Rounding::ceil).value == 2, "positive ceil");
    check(divide_wide(3, 2, Rounding::floor).value == 1, "positive floor");
    check(!ratio(1, 0), "zero denominator");
    check(!divide_wide(1, -1), "negative denominator");
    check(multiply(INT64_MAX, scale).value == INT64_MAX, "wide product exact boundary");
    check(!multiply(INT64_MAX, scale + 1), "wide product overflow");
    check(step_round(101, 10, Rounding::floor).value == 100, "quantity floor");
    check(step_round(101, 10, Rounding::ceil).value == 110, "quantity ceil");
    check(!step_round(INT64_MAX, 2, Rounding::ceil), "quantity rounding overflow");
    check(!add_time(UINT64_MAX, 1), "time overflow");
    auto state = envelope();
    check(validate(state) == Error::okay, "state envelope");
    state.expected_sequence = 2;
    check(validate(state) == Error::sequence, "state sequence");
    state = envelope();
    state.observed_ns = 201;
    check(validate(state) == Error::future, "future timestamp");
    state = envelope();
    state.now_ns = 1000;
    check(validate(state) == Error::expired, "exact deadline expires");
}

void oracle() {
    OraclePolicy policy{};
    policy.instrument = 7;
    policy.now_ns = 200;
    policy.max_age_ns = 200;
    policy.max_skew_ns = 50;
    policy.min_sources = 3;
    policy.max_dispersion_bps = 100;
    policy.max_confidence_bps = 20;
    policy.allowed_sources = {1, 2, 3};
    policy.source_count = 3;
    std::array<OracleObservation, 3> quotes{{
        {1, 7, 1, 150, 100 * scale, 1000, true},
        {2, 7, 1, 155, 100 * scale + 10000, 1000, true},
        {3, 7, 1, 160, 100 * scale - 10000, 1000, true},
    }};
    auto result = aggregate_price(quotes, policy);
    check(static_cast<bool>(result), "median aggregation");
    check(result.value.median == 100 * scale, "median value");
    check(result.value.valid_until_ns == 350, "oldest source TTL");
    const auto hash = result.value.diagnostic_fingerprint;
    std::swap(quotes[0], quotes[2]);
    check(aggregate_price(quotes, policy).value.diagnostic_fingerprint == hash, "source order invariance");
    quotes[0].source = quotes[1].source;
    check(aggregate_price(quotes, policy).error == Error::conflict, "duplicate source no quorum");
    quotes[0].source = 3;
    quotes[0].adapter_verified = false;
    check(aggregate_price(quotes, policy).error == Error::invalid, "unverified adapter");
    quotes[0].adapter_verified = true;
    quotes[0].observed_ns = 201;
    check(aggregate_price(quotes, policy).error == Error::future, "future price");
    quotes[0].observed_ns = 160;
    quotes[0].price = 110 * scale;
    check(aggregate_price(quotes, policy).error == Error::dispersion, "source disagreement");
    quotes[0].price = 100 * scale;
    OracleCache<3> cache;
    check(cache.update(quotes[0]) == Error::okay, "cache admission");
    check(cache.update(quotes[0]) == Error::okay, "cache exact replay");
    ++quotes[0].price;
    check(cache.update(quotes[0]) == Error::conflict, "cache sequence conflict");
}

void lending() {
    const LendingMarket market{100 * scale, 100 * scale, 0, 20000, 100000, 300000, 800000, 100000};
    const auto rates = lending_rates(market);
    check(static_cast<bool>(rates), "lending rates");
    check(rates.value.utilization == 500000, "utilization");
    check(rates.value.borrow_apr == 70000, "borrow APR below kink");
    check(rates.value.supply_apr == 31500, "supply APR after reserves");
    auto bad = market;
    bad.reserves = 300 * scale;
    check(!lending_rates(bad), "reserves exceed assets");
    check(simple_interest(100 * scale, 100000, 365 * 86400, false).value == 10 * scale, "annual interest");
    const YieldTerms terms{100 * scale, 50000, 100000, 500000, 0, scale, scale, 365 * 86400, 0, 100 * scale};
    const auto estimate = forward_yield(terms);
    check(estimate.value.net_income == 8 * scale, "retained incentive net yield");
    auto locked = terms;
    locked.lock_seconds = locked.horizon_seconds + 1;
    check(forward_yield(locked).error == Error::illiquid, "locked exit unavailable");
    const std::array<CollateralAsset, 1> collateral{{{1, 100 * scale, scale, 700000, 800000}}};
    const std::array<DebtAsset, 1> debts{{{2, 60 * scale, 0, scale}}};
    const auto health = lending_health(collateral, debts);
    check(health.value.debt_value == 60 * scale, "debt value");
    check(health.value.health_factor == 1333333, "health factor floors");
    check(health.value.headroom == 10 * scale, "borrow headroom");
    const auto paid = repay(50 * scale, 10 * scale, 20 * scale);
    check(paid.value.interest_paid == 10 * scale && paid.value.principal_paid == 10 * scale, "interest first");
    const auto choice = decide_repayment(health.value, {2 * scale, 30 * scale, 50 * scale, 10 * scale, scale, scale}, envelope());
    check(choice.value.action == EconomicAction::repay, "repayment decision");
    check(choice.value.payment_value == 20 * scale && choice.value.health_factor_after == 2 * scale, "minimum target repayment");
    const auto blocked = decide_repayment(health.value, {2 * scale, 30 * scale, 25 * scale, 10 * scale, scale, scale}, envelope());
    check(blocked.value.action == EconomicAction::escalate, "cash reserve blocks repayment");
    check(blocked.value.execution_authority == 0, "repay no authority");
}

void derivatives() {
    const LinearPosition position{1, 10 * scale, 100 * scale, 100 * scale, 100 * scale, 0, 0, 100000, 50000};
    const auto risk = linear_risk(position);
    check(risk.value.notional == 1000 * scale, "linear notional");
    check(risk.value.margin_headroom == 50 * scale && risk.value.leverage == 10 * scale, "margin leverage");
    check(linear_funding(10 * scale, 100 * scale, 1000).value.signed_payment == scale, "long pays positive funding");
    check(linear_funding(-10 * scale, 100 * scale, 1000).value.signed_payment == -scale, "short receives positive funding");
    const auto closed = simulate_reduction(position, 5 * scale, 100 * scale, 1000);
    check(closed.value.collateral_after == 99500000, "execution fee charged");
    check(closed.value.signed_quantity_after == 5 * scale, "partial close size");
    const RecoveryPolicy policy{10 * scale, 70 * scale, 6 * scale, 10 * scale, 10 * scale, 100, scale, 20, 900};
    const std::array<ExecutionVenue, 2> venues{{
        {2, 100 * scale, 10 * scale, scale, 1000, 0, scale, true, envelope()},
        {1, 100 * scale, 10 * scale, scale, 2000, 0, scale, true, envelope()},
    }};
    const auto chosen = decide_derivative_recovery(position, policy, venues, envelope());
    check(static_cast<bool>(chosen), "recovery search");
    check(chosen.value.action == EconomicAction::reduce && chosen.value.venue_id == 2, "cheaper route recovery");
    check(chosen.value.close_quantity == 5 * scale, "least costly feasible reduction");
    check(chosen.value.execution_authority == 0, "native recovery cannot execute");
    auto capped = policy;
    capped.maximum_lots_examined = 1;
    check(decide_derivative_recovery(position, capped, venues, envelope()).error == Error::incomplete, "partial search no optimality");
    const std::array<std::int64_t, 2> shocks{{-1000, 1000}};
    const auto stress = stress_linear(position, shocks);
    check(stress.value.any_liquidatable, "adverse stress liquidation");
    check(stress.value.worst_scenario == 0, "worst scenario stable");
    check(isolated_liquidation_price(position).value == 94736843, "isolated liquidation threshold");
    const RebalanceTerms rebalance{10 * scale, 20 * scale, scale, scale, scale, 2 * scale, scale, 0, 0};
    check(decide_rebalance(rebalance, envelope()).value.action == EconomicAction::allocate, "net improvement rebalance");
    auto expensive = rebalance;
    expensive.trading_cost = 10 * scale;
    check(decide_rebalance(expensive, envelope()).value.action == EconomicAction::hold, "rebalance cost exceeds benefit");
}

void amm() {
    const ConstantProductPool pool{1, 1, 2, 1000 * scale, 1000 * scale, 3000, scale, envelope()};
    const auto quote = exact_input_swap(pool, 10 * scale);
    check(static_cast<bool>(quote), "constant-product quote");
    check(quote.value.amount_out == 9871580, "integer exact input quote");
    check(quote.value.fee_in == 30000, "input fee rounds up");
    check(static_cast<Wide>(quote.value.reserve_in_after) * quote.value.reserve_out_after >=
        static_cast<Wide>(pool.reserve_in) * pool.reserve_out, "pool invariant preserved");
    const auto exact = exact_output_swap(pool, 10 * scale);
    check(static_cast<bool>(exact) && exact.value.amount_out == 10 * scale, "exact output quote");
    check(exact_input_swap(pool, exact.value.amount_in).value.amount_out >= exact.value.amount_out, "inverse quote sufficiency");
    check(exact_input_swap(pool, exact.value.amount_in - 1).value.amount_out < exact.value.amount_out, "minimal inverse input");
    check(exact_output_swap(pool, pool.reserve_out).error == Error::illiquid, "reserve exhaustion");
    const auto redeemed = redeem_liquidity(10, 100, 1000, 2000);
    check(redeemed.value.amount_a == 100 && redeemed.value.amount_b == 200, "pro rata LP redemption");
    SwapRoute route{};
    route.pools[0] = pool;
    route.pools[1] = {2, 2, 3, 1000 * scale, 2000 * scale, 3000, scale, envelope()};
    route.count = 2;
    route.amount_in = 10 * scale;
    route.minimum_output = 15 * scale;
    route.max_cost = 2 * scale;
    route.max_impact_bps = 1000;
    check(static_cast<bool>(simulate_route(route)), "two hop simulation");
    route.pools[1].asset_in = 7;
    check(simulate_route(route).error == Error::domain, "disconnected route");
}

void allocation() {
    std::array<Product, 3> products{};
    for (std::size_t i = 0; i < products.size(); ++i) {
        auto& product = products[i];
        product.id = i + 1;
        product.group = static_cast<std::uint32_t>(i);
        product.max_weight_bps = bps_scale;
        product.available_capacity = 100 * scale;
        product.exit_capacity = 100 * scale;
        product.enabled = true;
    }
    products[0].current_value = 100 * scale;
    products[0].liquid = true;
    products[1].expected_net_return_bps = 1000;
    products[1].scenario_loss_bps[0] = 1000;
    products[2].expected_net_return_bps = 1500;
    products[2].scenario_loss_bps[0] = 3000;
    AllocationPolicy policy{};
    policy.capital = 100 * scale;
    policy.cash_floor = 25 * scale;
    policy.max_turnover = 200 * scale;
    policy.max_cost = 5 * scale;
    policy.max_stress_loss = 20 * scale;
    policy.horizon_seconds = 86400;
    policy.group_caps_bps = {10000, 5000, 5000};
    policy.group_count = 3;
    policy.scenario_count = 1;
    const auto search = search_allocations(products, policy, {2500, 1000});
    check(static_cast<bool>(search) && search.value.comparison.complete, "complete finite grid");
    check(search.value.comparison.plans[0].amounts[0] == 25 * scale, "cash floor respected");
    check(search.value.comparison.plans[0].amounts[2] == 50 * scale, "group concentration cap");
    check(search.value.comparison.plans[0].expected_net_income == 10 * scale, "candidate net income");
    check(search_allocations(products, policy, {2500, 1}).error == Error::incomplete, "allocation work cap");
    policy.cash_floor = 75 * scale;
    const auto liquid = search_allocations(products, policy, {2500, 1000});
    check(liquid.value.comparison.plans[0].amounts[0] == 75 * scale, "counterfactual condition changes plan");
    check(liquid.value.comparison.plans[0].expected_net_income < search.value.comparison.plans[0].expected_net_income,
        "counterfactual yield tradeoff");
    AllocationCandidate invalid{};
    invalid.id = 1;
    invalid.weights_bps = {5000, 2500, 0};
    check(score_allocation(products, policy, invalid).error == Error::infeasible, "nonconserving weights");
    products[1].current_value = 1;
    check(validate_allocation_domain(products, policy) == Error::domain, "capital total coherent");
}

void abi() {
    DomainDescriptor domain{};
    check(machine_economics_abi() == 1, "economics ABI version");
    check(machine_economics_descriptor(7, &domain) == 0 && domain.input_size == sizeof(DerivativeRecoveryRequest),
        "native descriptor agrees");
    check(machine_economics_descriptor(99, &domain) == static_cast<int>(Error::unsupported), "unknown domain rejected");
    FundingRequest request{scale, 100 * scale, 1000};
    FundingCashFlow output{};
    check(machine_economics_evaluate(6, &request, sizeof(request), &output, sizeof(output)) == 0 &&
        output.signed_payment == 100000, "actual C ABI result");
    check(machine_economics_evaluate(6, &request, 0, &output, sizeof(output)) == static_cast<int>(Error::invalid),
        "ABI size guard");
}

void routing() {
    VenueDepth first{};
    first.venue = 1;
    first.instrument = 7;
    first.base_asset = 1;
    first.quote_asset = 2;
    first.levels[0] = {100 * scale, scale};
    first.levels[1] = {102 * scale, 10 * scale};
    first.count = 2;
    first.side = OrderSide::buy;
    first.quantity_step = scale;
    first.minimum_quantity = scale;
    first.minimum_notional = 10 * scale;
    first.maximum_notional = 10000 * scale;
    first.envelope = envelope();
    auto second = first;
    second.venue = 2;
    second.count = 1;
    second.levels[0] = {101 * scale, 10 * scale};
    const std::array<VenueDepth, 2> venues{{first, second}};
    const RoutePolicy policy{7, 1, 2, OrderSide::buy, OrderType::fill_or_kill,
        5 * scale, 103 * scale, 1000 * scale, 300, 100 * scale, 2};
    const auto chosen = best_single_venue(venues, policy);
    check(chosen.value.children[0].venue == 2, "depth outweighs top-of-book price");
    check(chosen.value.cash_flow == 505 * scale, "whole order cash flow");
    auto costly = venues;
    costly[1].fixed_cost = 10 * scale;
    check(best_single_venue(costly, policy).value.children[0].venue == 1, "fixed cost changes best venue");
    auto limited = policy;
    limited.quote_budget = 504 * scale;
    check(best_single_venue(venues, limited).error == Error::infeasible, "quote budget includes execution cost");
    limited = policy;
    limited.maximum_slippage_bps = 50;
    check(best_single_venue(venues, limited).error == Error::infeasible, "slippage guard");
    std::array<SplitCandidate, 2> candidates{};
    candidates[0].id = 1;
    candidates[0].quantities = {scale, 4 * scale};
    candidates[1].id = 2;
    candidates[1].quantities = {0, 5 * scale};
    const auto split = compare_splits(venues, policy, candidates);
    check(split.value.candidate_id == 1 && split.value.plan.cash_flow == 504 * scale, "candidate split beats single");
    auto minimum = venues;
    minimum[0].minimum_quantity = 2 * scale;
    check(compare_splits(minimum, policy, candidates).value.candidate_id == 2, "child minimum excludes attractive split");
    check(split.value.plan.execution_authority == 0, "route plan no transmission");
}

void statistics() {
    const std::array<WeightedScenario, 3> scenarios{{
        {1, -10 * scale, 1000}, {2, 10 * scale, 8000}, {3, 100 * scale, 1000}
    }};
    const auto risk = scenario_tail_risk(scenarios, 9500);
    check(risk.value.expected_loss == 17 * scale, "weighted expected loss");
    check(risk.value.value_at_risk == 100 * scale, "discrete quantile");
    check(risk.value.conditional_value_at_risk == 100 * scale, "partial tail mass");
    const auto broad = scenario_tail_risk(scenarios, 8000);
    check(broad.value.conditional_value_at_risk == 55 * scale, "tail boundary scenario split");
    auto invalid = scenarios;
    invalid[0].probability_bps = 2000;
    check(scenario_tail_risk(invalid, 9500).error == Error::domain, "probability conservation");
    const std::array<PriceSample, 3> prices{{{100, 100 * scale}, {200, 110 * scale}, {300, 99 * scale}}};
    const auto returns = return_statistics(prices, 100, 0);
    check(returns.value.total_return == -10000, "total simple return");
    check(returns.value.average_simple_return == 0, "mean interval return");
    check(returns.value.variance == 10000, "population variance");
    check(returns.value.standard_deviation == 100000, "fixed point standard deviation");
    check(returns.value.maximum_drawdown == 100000, "running peak drawdown");
    check(returns.value.time_weighted_price == 105 * scale, "left-continuous time weighted price");
    auto gap = prices;
    gap[2].timestamp_ns = 400;
    check(return_statistics(gap, 100, 0).error == Error::stale, "missing sample interval");
    check(integer_sqrt(UINT64_MAX) == 4294967295ULL, "integer sqrt full unsigned range");
    const std::array<FactorExposure, 2> exposures{{
        {1, 100 * scale, {scale}}, {2, -90 * scale, {scale}}
    }};
    const std::array<FactorBound, 1> bounds{{{1, 150 * scale, -20 * scale, 20 * scale}}};
    const auto factor = factor_risk(exposures, bounds);
    check(factor.value.net[0] == 10 * scale && factor.value.gross[0] == 190 * scale, "gross versus net exposure");
    check(factor.value.violated == 1, "hedge cannot hide gross risk limit");
}

void transition() {
    EconomicProcess process{};
    process.identity = {1, 2, 1, 3, 1, 4, 421614, 5};
    process.capital = {100 * scale, 0, 0, 50 * scale, 5 * scale, 10 * scale, 50 * scale, 10 * scale};
    TransitionInput input{};
    input.identity = process.identity;
    input.state = envelope();
    input.event = 1;
    input.fingerprint = 11;
    input.kind = TransitionEvent::observe;
    check(advance_process(process, input).error == Error::okay, "observe transition");
    const auto sequence = process.sequence;
    check(advance_process(process, input).replay && process.sequence == sequence, "exact transition replay");
    input.fingerprint = 12;
    check(advance_process(process, input).error == Error::conflict, "event identity changed payload");
    input.event = 2;
    input.kind = TransitionEvent::propose;
    input.action = {EconomicAction::swap, 6, 7, 8, 9, 900, 20 * scale, scale, scale,
        20 * scale, 20 * scale, true};
    check(advance_process(process, input).error == Error::okay, "proposal invariants");
    input.event = 3;
    input.kind = TransitionEvent::simulate;
    input.simulation = {6, 7, 8, 10, 150, 20 * scale, scale, 0, 20 * scale, 20 * scale, true, true};
    check(advance_process(process, input).error == Error::okay, "simulation verifies bound intent");
    input.event = 4;
    input.kind = TransitionEvent::request_authorization;
    check(advance_process(process, input).error == Error::okay && process.capital.reserved == 21 * scale,
        "capital reserved before signature");
    input.event = 5;
    input.kind = TransitionEvent::authorize;
    input.authorization = {11, 6, 7, 4, 421614, 1, 150, 800, true};
    process.policy_paused = true;
    check(advance_process(process, input).error == Error::unauthorized, "pause blocks fresh authority");
    process.policy_paused = false;
    check(advance_process(process, input).error == Error::okay, "external authorization binding");
    input.event = 6;
    input.kind = TransitionEvent::transmit;
    check(advance_process(process, input).error == Error::okay, "transmission state only");
    input.event = 7;
    input.kind = TransitionEvent::ambiguous;
    check(advance_process(process, input).error == Error::okay && process.capital.reserved == 21 * scale,
        "unknown retains encumbrance");
    input.event = 8;
    input.kind = TransitionEvent::withdraw_unsent;
    check(advance_process(process, input).error == Error::unauthorized, "unknown cannot release funds");
    input.kind = TransitionEvent::escalate;
    check(advance_process(process, input).error == Error::okay && process.phase == EconomicPhase::unknown,
        "resolver preserves recoverable unknown state");
    input.event = 9;
    input.kind = TransitionEvent::include;
    input.settlement = {6, 12, 13, 100, 99, 14, 15, 16, 20 * scale, 500000, 0,
        20 * scale, 20 * scale, true, true, true};
    check(advance_process(process, input).error == Error::okay, "independent inclusion evidence");
    input.event = 10;
    input.kind = TransitionEvent::finalize;
    check(advance_process(process, input).error == Error::incomplete, "inclusion is not finality");
    input.settlement.finalized_height = 101;
    input.settlement.block_commitment = 99;
    check(advance_process(process, input).error == Error::reorg, "block mismatch cannot finalize");
    input.settlement.block_commitment = 13;
    check(advance_process(process, input).error == Error::okay, "finalized observation");
    input.event = 11;
    input.kind = TransitionEvent::verify_post_state;
    input.settlement.actual_cash = 0;
    check(advance_process(process, input).error == Error::infeasible, "post-state cash floor");
    input.settlement.actual_cash = 20 * scale;
    check(advance_process(process, input).error == Error::okay, "post-state verification");
    input.event = 12;
    input.kind = TransitionEvent::settle;
    const auto receipt = advance_process(process, input);
    check(receipt.error == Error::okay && process.capital.reserved == 0 &&
        process.capital.spent == 20500000, "actual cost charged once");
    check(receipt.execution_authority == 0, "transition mirror cannot sign");
    check(advance_process(process, input).replay && process.capital.spent == 20500000, "settlement replay no double charge");
    input.settlement.actual_cost = 0;
    check(advance_process(process, input).error == Error::conflict && process.capital.spent == 20500000,
        "same event label with changed settlement cannot replay");
}

FactDelta balance_delta(std::uint64_t event, std::uint64_t sequence, std::int64_t value) {
    FactDelta delta{};
    delta.event = event;
    delta.fingerprint = event * 101;
    delta.operation = FactOperation::snapshot;
    delta.fact.key = {1, 421614, 2, 3, 4};
    delta.fact.unit = FactUnit::money;
    delta.fact.asset = 10;
    delta.fact.value = value;
    delta.fact.source = 20;
    delta.fact.source_generation = 1;
    delta.fact.sequence = sequence;
    delta.fact.observed_ns = 100;
    delta.fact.valid_for_ns = 500;
    delta.fact.status = FactStatus::coherent;
    return delta;
}

void economic_state() {
    EconomicStateStore<4, 16> state;
    auto delta = balance_delta(1, 1, 100 * scale);
    check(state.apply(delta).error == Error::okay, "typed state snapshot");
    check(state.read(delta.fact.key, FactUnit::money, 10, 200).value.value == 100 * scale,
        "read exact account currency");
    check(state.apply(delta).replay && state.generation() == 1, "snapshot replay cannot advance state");
    delta.fact.value = 90 * scale;
    check(state.apply(delta).error == Error::conflict, "changed body with same caller label conflicts");
    delta = balance_delta(2, 2, -10 * scale);
    delta.operation = FactOperation::increment;
    check(state.apply(delta).error == Error::okay &&
        state.read(delta.fact.key, FactUnit::money, 10, 200).value.value == 90 * scale,
        "signed increment preserves unsigned balance");
    auto other_account = delta.fact.key;
    other_account.account = 99;
    check(state.read(other_account, FactUnit::money, 10, 200).error == Error::missing,
        "fact keys isolate accounts");
    check(state.read(delta.fact.key, FactUnit::quantity, 10, 200).error == Error::domain,
        "fact units cannot be reinterpreted");
    check(state.read(delta.fact.key, FactUnit::money, 11, 200).error == Error::domain,
        "currencies cannot be reinterpreted");
    check(state.read(delta.fact.key, FactUnit::money, 10, 600).error == Error::stale,
        "freshness boundary exclusive");
    check(state.read(delta.fact.key, FactUnit::money, 10, 99).error == Error::future,
        "future observation rejected");
    delta = balance_delta(3, 4, 80 * scale);
    delta.operation = FactOperation::replacement;
    check(state.apply(delta).error == Error::sequence && state.generation() == 3,
        "sequence gap invalidates state");
    check(state.read(delta.fact.key, FactUnit::money, 10, 200).error == Error::unknown,
        "gap state cannot be used");
    delta = balance_delta(4, 5, 70 * scale);
    check(state.apply(delta).error == Error::okay, "authoritative snapshot repairs gap");
    delta = balance_delta(5, 6, -80 * scale);
    delta.operation = FactOperation::increment;
    check(state.apply(delta).error == Error::invalid &&
        state.read(delta.fact.key, FactUnit::money, 10, 200).value.value == 70 * scale,
        "negative balance rejected atomically");
    delta = balance_delta(6, 6, 50 * scale);
    delta.fact.source = 21;
    check(state.apply(delta).error == Error::domain, "source cannot be silently replaced");
    const std::array<Dependency, 1> deps{{{delta.fact.key, FactUnit::money, 10, 0}}};
    auto frame = resolve_dependencies(state, deps, 200, 10);
    check(frame && frame.value.valid_until_ns == 600 && frame.value.facts[0].value == 70 * scale,
        "coherent dependency frame");
    const std::array<Dependency, 2> duplicates{{deps[0], deps[0]}};
    check(resolve_dependencies(state, duplicates, 200, 10).error == Error::conflict,
        "duplicate dependencies rejected");
    std::array<FactDelta, 2> batch{{balance_delta(7, 6, 60 * scale), balance_delta(8, 7, 50 * scale)}};
    batch[1].fact.asset = 11;
    const auto generation = state.generation();
    check(state.apply_batch(batch).error == Error::domain && state.generation() == generation &&
        state.read(batch[0].fact.key, FactUnit::money, 10, 200).value.value == 70 * scale,
        "batch failure rolls back earlier delta and replay");
    batch[1].fact.asset = 10;
    check(state.apply_batch(batch).error == Error::okay &&
        state.read(batch[0].fact.key, FactUnit::money, 10, 200).value.value == 50 * scale,
        "atomic batch commits both deltas");
    check(state.compact_replays(8, state.generation(), false) == Error::unauthorized,
        "compaction needs trusted journal checkpoint");
    check(state.compact_replays(8, state.generation(), true) == Error::okay &&
        state.apply(batch[1]).error == Error::sequence, "compacted replay cannot re-execute");
    check(state.invalidate_source(20, 1, 200) == Error::okay &&
        state.read(batch[0].fact.key, FactUnit::money, 10, 200).error == Error::unknown,
        "source disconnect invalidates observations");
    EconomicStateStore<1, 1> tiny;
    check(tiny.apply(balance_delta(1, 1, scale)).error == Error::okay &&
        tiny.apply(balance_delta(2, 2, scale)).error == Error::capacity,
        "replay table uses backpressure rather than silent eviction");
}

StrategyProgram safety_program() {
    StrategyProgram program{};
    program.id = 1;
    program.version = 2;
    program.policy_version = 3;
    program.ttl_ns = 100;
    program.dependency_count = 1;
    program.dependency_types[0] = {FactUnit::money, 10, 0};
    program.count = 7;
    const RegisterType money{FactUnit::money, 10, 0};
    const RegisterType boolean{FactUnit::boolean, 0, 0};
    program.instructions[0] = {StrategyOpcode::observe, 0, 0, 0, 0, 0, money};
    program.instructions[1] = {StrategyOpcode::constant, 1, 0, 0, 0, 150 * scale, money};
    program.instructions[2] = {StrategyOpcode::compare, 2, 0, 1, 0,
        static_cast<std::int64_t>(Comparator::less), boolean};
    program.instructions[3] = {StrategyOpcode::branch, 3, 2, 0, 0, 0, boolean, 4, 6};
    program.instructions[4] = {StrategyOpcode::constant, 4, 0, 0, 0, 5 * scale,
        {FactUnit::quantity, 8, 0}};
    program.instructions[5] = {StrategyOpcode::reduce, 5, 4, 0, 0, 0, boolean};
    program.instructions[6] = {StrategyOpcode::hold, 6, 0, 0, 0, 0, boolean};
    return program;
}

void economic_strategy() {
    auto program = safety_program();
    check(validate_strategy(program).error == Error::okay, "validate typed forward branch program");
    EconomicStateStore<4, 8> state;
    auto delta = balance_delta(1, 1, 100 * scale);
    check(state.apply(delta).error == Error::okay, "program input committed");
    const std::array<Dependency, 1> deps{{{delta.fact.key, FactUnit::money, 10, 0}}};
    auto resolved = resolve_dependencies(state, deps, 200, 0);
    StrategyFrame frame{resolved.value, 2, 3, 200, true};
    auto result = run_strategy(program, frame);
    check(result.error == Error::okay && result.action == EconomicAction::reduce && result.amount == 5 * scale &&
        result.valid_until_ns == 300 && result.execution_authority == 0,
        "economic program reduces bounded candidate without signing");
    check(run_strategy(program, frame).diagnostic_fingerprint == result.diagnostic_fingerprint,
        "same program and state reproduce decision");
    frame.dependencies.facts[0].value = 200 * scale;
    check(run_strategy(program, frame).action == EconomicAction::hold, "collateral counterfactual changes path");
    frame.policy_active = false;
    check(run_strategy(program, frame).error == Error::unauthorized, "paused mandate cannot run");
    frame.policy_active = true;
    frame.policy_version = 4;
    check(run_strategy(program, frame).error == Error::unauthorized, "policy version bound");
    frame.policy_version = 3;
    frame.now_ns = 600;
    check(run_strategy(program, frame).error == Error::expired, "dependency deadline enforced");
    frame.now_ns = 200;
    frame.dependencies.facts[0].key.account = 99;
    // The supplied frame is deliberately an assumption frame, not an account
    // authentication artifact. Account binding belongs to the workspace feed.
    program.instructions[3].true_target = 2;
    check(validate_strategy(program).error == Error::invalid, "back edges rejected");
    program = safety_program();
    program.instructions[2].a = 63;
    check(validate_strategy(program).error == Error::domain, "uninitialized register rejected");
    program = safety_program();
    program.instructions[1].type.asset = 11;
    check(validate_strategy(program).error == Error::domain, "cross currency comparison rejected");
    program = safety_program();
    program.instructions[4].immediate = 0;
    frame.dependencies.facts[0].value = 100 * scale;
    check(run_strategy(program, frame).error == Error::invalid, "zero-sized action rejected");
    program = safety_program();
    program.instructions[5].opcode = StrategyOpcode::settle;
    check(run_strategy(program, frame).action == EconomicAction::escalate,
        "settle opcode requests verification instead of fabricating settlement");
    check(product_type({FactUnit::quantity, 8, 0}, {FactUnit::price, 8, 10}, false).value.asset == 10,
        "base quantity times quote per base produces quote money");
    check(product_type({FactUnit::quantity, 9, 0}, {FactUnit::price, 8, 10}, false).error == Error::domain,
        "notional rejects mismatched base assets");
    program = safety_program();
    // A branch register is itself initialized on both paths: use it in a
    // second branch to detect static/runtime assignment disagreement.
    program.count = 8;
    const RegisterType boolean{FactUnit::boolean, 0, 0};
    program.instructions[4] = {StrategyOpcode::branch, 4, 3, 0, 0, 0, boolean, 5, 7};
    program.instructions[5] = {StrategyOpcode::constant, 5, 0, 0, 0, 5 * scale,
        {FactUnit::quantity, 8, 0}};
    program.instructions[6] = {StrategyOpcode::reduce, 6, 5, 0, 0, 0, boolean};
    program.instructions[7] = {StrategyOpcode::hold, 7, 0, 0, 0, 0, boolean};
    program.instructions[3].false_target = 7;
    check(run_strategy(program, frame).action == EconomicAction::reduce,
        "branch destination assignment agrees with validation");
    StateFrameRequest request{};
    request.deltas[0] = delta;
    request.dependencies[0] = deps[0];
    request.delta_count = request.dependency_count = 1;
    request.now_ns = 200;
    DependencyFrame output{};
    check(machine_economics_evaluate(18, &request, sizeof(request), &output, sizeof(output)) == 0 &&
        output.state_generation == 1, "state dependency frame exported ABI");
    StrategyRequest execution{program, frame};
    StrategyResult candidate{};
    check(machine_economics_evaluate(19, &execution, sizeof(execution), &candidate, sizeof(candidate)) == 0 &&
        candidate.action == EconomicAction::reduce, "economic ISA exported ABI");
    auto* flag_byte = reinterpret_cast<unsigned char*>(&execution.frame.policy_active);
    *flag_byte = 7;
    check(machine_economics_evaluate(19, &execution, sizeof(execution), &candidate, sizeof(candidate)) ==
        static_cast<int>(Error::invalid), "malformed external bool rejected before evaluation");
    check(candidate.execution_authority == 0 && candidate.amount == 0,
        "invalid C input clears candidate output");
}

void economic_runtime() {
    EconomicRuntime<8, 4, 2> runtime;
    ProgramRegistration registration{};
    registration.program = safety_program();
    registration.dependencies[0] = {{1, 421614, 2, 3, 4}, FactUnit::money, 10, 0};
    registration.mandate = {1, 421614, 40, 3, 1000, 50, 0, true};
    check(runtime.install(registration, 100) == Error::okay, "event runtime install scoped program");
    check(runtime.poll(100, 4) == Error::okay, "missing state creates resolver request");
    RuntimeDecision receipt{};
    check(runtime.pop(receipt) && receipt.candidate.action == EconomicAction::escalate &&
        receipt.candidate.error == Error::missing && receipt.candidate.execution_authority == 0,
        "missing dependencies explicitly escalate without LLM in loop");
    const auto first = runtime.counters();
    for (std::uint64_t now = 101; now < 150; ++now) {
        check(runtime.poll(now, 4) == Error::okay, "idle numeric poll remains bounded");
    }
    check(runtime.counters().evaluations == first.evaluations && !runtime.pop(receipt),
        "idle monitoring consumes no inference and emits no repeated resolver request");
    auto delta = balance_delta(1, 1, 100 * scale);
    check(runtime.observe(delta, 150).error == Error::okay, "runtime feed enters canonical state");
    check(runtime.poll(150, 4) == Error::okay && runtime.pop(receipt) &&
        receipt.candidate.action == EconomicAction::reduce && receipt.account == 1 && receipt.policy == 40,
        "affected program runs under account mandate");
    check(runtime.observe(delta, 151).replay, "runtime replay returns original commit");
    check(runtime.poll(151, 4) == Error::okay && !runtime.pop(receipt), "replay does not reschedule inference");
    delta = balance_delta(2, 2, 200 * scale);
    check(runtime.observe(delta, 152).error == Error::okay, "second observation");
    check(runtime.poll(152, 4) == Error::okay && !runtime.pop(receipt), "cooldown coalesces changed facts");
    check(runtime.poll(200, 4) == Error::okay && runtime.pop(receipt) &&
        receipt.candidate.action == EconomicAction::hold, "latest fact evaluated at cooldown");
    check(runtime.poll(300, 4) == Error::okay && runtime.pop(receipt), "candidate TTL is a timer interrupt");
    check(receipt.candidate.action == EconomicAction::hold, "unexpired state refreshes candidate on TTL");
    check(runtime.poll(600, 4) == Error::okay && runtime.pop(receipt) &&
        receipt.candidate.error == Error::stale, "state expiry escalates without price events");
    check(runtime.poll(601, 4) == Error::okay && !runtime.pop(receipt), "expired state not repeated every poll");
    check(runtime.pause(1, 9, 602) == Error::unauthorized, "runtime pause verifies policy version");
    check(runtime.pause(1, 3, 602) == Error::okay, "runtime pause current policy");
    check(runtime.poll(651, 4) == Error::okay && runtime.pop(receipt) &&
        receipt.candidate.error == Error::unauthorized, "paused runtime emits no financial candidate");
    check(runtime.poll(650, 4) == Error::sequence, "monotonic runtime clock");
    auto bad = registration;
    bad.dependencies[0].key.account = 2;
    check(runtime.install(bad, 700) == Error::domain, "registered programs cannot observe other account");
    bad = registration;
    bad.dependencies[0].key.network = 1;
    check(runtime.install(bad, 700) == Error::domain, "registered program network bound");
    check(runtime.install(registration, 700) == Error::sequence, "program version cannot roll back");
    registration.program.version = 3;
    check(runtime.install(registration, 700) == Error::okay, "scoped forward program replacement");
    check(runtime.checkpoint(2, runtime.state_generation(), false) == Error::unauthorized,
        "runtime cannot compact unverified journal");
    check(runtime.checkpoint(2, runtime.state_generation(), true) == Error::okay,
        "owner-verified checkpoint compaction");
    EconomicRuntime<8, 4, 2> pressure;
    registration.program = safety_program();
    registration.mandate = {1, 421614, 40, 3, 1000, 0, 0, true};
    for (std::uint64_t id = 1; id <= 4; ++id) {
        registration.program.id = id;
        check(pressure.install(registration, 100) == Error::okay, "independent program installed");
    }
    check(pressure.observe(balance_delta(1, 1, 100 * scale), 100).error == Error::okay,
        "backpressure fixture feed");
    check(pressure.poll(100, 4) == Error::backpressure && pressure.queued_approx() == 2,
        "full decision queue retains pending candidate");
    check(pressure.observe(balance_delta(2, 2, 200 * scale), 101).error == Error::backpressure &&
        pressure.state_generation() == 1, "blocked queue cannot silently replace input state");
    check(pressure.pause(3, 3, 101) == Error::okay, "pause takes effect despite pending decision");
    check(pressure.pop(receipt), "consumer drains queue");
    check(pressure.pop(receipt), "consumer drains second item");
    check(pressure.poll(101, 4) == Error::backpressure, "remaining candidates preserve bounded pressure");
    bool paused_seen = false;
    while (pressure.pop(receipt)) {
        if (receipt.candidate.program == 3) paused_seen |= receipt.candidate.error == Error::unauthorized;
    }
    check(paused_seen, "paused pending candidate cannot leak actionable amount");
    check(pressure.counters().execution_authority == 0, "hardware runtime never grants signing authority");
}

ExecutionGraph graph_example() {
    ExecutionGraph graph{};
    graph.plan = 1;
    graph.account = 7;
    graph.network = 421614;
    graph.policy_version = graph.expected_policy_version = 3;
    graph.state_generation = 4;
    graph.now_ns = 200;
    graph.observed_ns = 100;
    graph.max_age_ns = 500;
    graph.expires_ns = 600;
    graph.assets[0] = {10, 100 * scale, 0, 0, scale, scale, 0, 0, 5 * scale};
    graph.assets[1] = {20, 0, 0, 0, 2 * scale, 2 * scale, 500000, 800000, 0};
    graph.asset_count = 2;
    graph.allowed_protocols[0] = 8;
    graph.protocol_count = 1;
    graph.maximum_cost_value = 5 * scale;
    graph.maximum_turnover_value = 100 * scale;
    graph.maximum_debt_value = 20 * scale;
    graph.minimum_health_factor = 1500000;
    graph.step_count = 2;
    graph.steps[0] = {200, ExecutionPrimitive::supply, 7, 421614, 8, 9, 2,
        20, 20, 0, 500, 20 * scale, 20 * scale, 20 * scale, 0, 0, scale, 0, 0, true, true};
    graph.steps[1] = {100, ExecutionPrimitive::swap, 7, 421614, 8, 9, 0,
        10, 20, 0, 500, 40 * scale, 20 * scale, 21 * scale, 0, 0, scale, 0, 0, true, true};
    graph.targets[0] = {10, 59 * scale, 60 * scale, 0, 0, 0};
    graph.targets[1] = {20, 0, 0, 20 * scale, 20 * scale, 0};
    graph.target_count = 2;
    return graph;
}

void execution_graph() {
    auto graph = graph_example();
    auto result = compile_execution_graph(graph);
    check(result && result.value.order[0] == 1 && result.value.order[1] == 0,
        "compiler orders swap before dependent supply");
    check(result.value.assets_after[0].available == 60 * scale &&
        result.value.assets_after[1].available == 0 && result.value.assets_after[1].reserved == 20 * scale,
        "projection uses conservative quote floor");
    check(result.value.execution_authority == 0 && result.value.valid_until_ns == 500,
        "transaction graph grants no authority and clips expiry");
    graph.steps[1].dependencies = 1;
    check(compile_execution_graph(graph).error == Error::infeasible, "cyclic graph rejected");
    graph = graph_example();
    graph.steps[0].dependencies = 1ULL << 4;
    check(compile_execution_graph(graph).error == Error::invalid, "dependency cannot point outside graph");
    graph = graph_example();
    graph.steps[0].id = graph.steps[1].id;
    check(compile_execution_graph(graph).error == Error::conflict, "duplicate execution identity");
    graph = graph_example();
    graph.steps[0].dependencies = 0;
    graph.steps[0].id = 1;
    check(compile_execution_graph(graph).error == Error::illiquid, "unfunded intermediate supply rejected");
    graph = graph_example();
    graph.steps[1].maximum_fee = scale;
    graph.steps[1].maximum_cost_value = scale;
    graph.steps[1].fee_asset = 10;
    check(compile_execution_graph(graph).value.assets_after[0].available == 59 * scale,
        "fee debit included before swap");
    graph.steps[1].maximum_cost_value = 0;
    check(compile_execution_graph(graph).error == Error::cost, "declared fee value cannot understate token fee");
    graph = graph_example();
    graph.steps[1].debit = 99 * scale;
    check(compile_execution_graph(graph).error == Error::illiquid, "cash floor protected at every step");
    graph = graph_example();
    graph.steps[1].quote_verified = false;
    check(compile_execution_graph(graph).error == Error::invalid, "swap needs adapter quote assertion");
    graph = graph_example();
    graph.steps[1].destination_allowed = false;
    check(compile_execution_graph(graph).error == Error::unauthorized, "unknown destination rejected");
    graph = graph_example();
    graph.steps[1].protocol = 123;
    check(compile_execution_graph(graph).error == Error::unauthorized, "protocol permission checked");
    graph = graph_example();
    graph.steps[1].account = 123;
    check(compile_execution_graph(graph).error == Error::unauthorized, "account mismatch rejected");
    graph = graph_example();
    graph.steps[1].expires_ns = 200;
    check(compile_execution_graph(graph).error == Error::expired, "expired step cannot compile");
    graph = graph_example();
    graph.targets[0].maximum_available = 59 * scale;
    check(compile_execution_graph(graph).error == Error::infeasible, "terminal target binds compiled flow");
    graph = graph_example();
    graph.targets[1].asset = 10;
    check(compile_execution_graph(graph).error == Error::conflict, "targets must cover distinct assets");
    graph = graph_example();
    graph.step_count = 3;
    graph.steps[2] = {300, ExecutionPrimitive::borrow, 7, 421614, 8, 9, 1,
        10, 10, 0, 500, 5 * scale, 5 * scale, 5 * scale, 0, 0, scale, 0, 0, true, true};
    graph.targets[0].minimum_available = graph.targets[0].maximum_available = 65 * scale;
    graph.targets[0].maximum_liability = 5 * scale;
    result = compile_execution_graph(graph);
    check(result && result.value.liability_value == 5 * scale && result.value.health_factor == 6400000,
        "borrow is liability plus proceeds, not yield");
    graph.assets[1].collateral_factor = 100000;
    check(compile_execution_graph(graph).error == Error::collateral,
        "borrowing checks borrow capacity separately from liquidation health");
    graph.assets[1].collateral_factor = 500000;
    graph.step_count = 4;
    graph.steps[3] = {400, ExecutionPrimitive::repay, 7, 421614, 8, 9, 4,
        10, 10, 0, 500, 5 * scale, 0, 0, 0, 0, scale, 0, 0, true, true};
    graph.targets[0] = {10, 60 * scale, 60 * scale, 0, 0, 0};
    result = compile_execution_graph(graph);
    check(result && result.value.debt_free && result.value.assets_after[0].available == 60 * scale,
        "repayment removes debt and cash exactly once in projection");
    graph.steps[3].debit = 6 * scale;
    check(compile_execution_graph(graph).error == Error::invalid, "cannot repay more than declared debt");
    std::array<ExecutionGraph, 2> candidates{{graph_example(), graph_example()}};
    candidates[0].plan = 10;
    candidates[1].plan = 11;
    candidates[0].steps[1].fee_asset = 10;
    candidates[0].steps[1].maximum_fee = scale;
    candidates[0].steps[1].maximum_cost_value = scale;
    const auto compared = compare_execution_graphs(candidates);
    check(compared && compared.value.best.plan == 11 && compared.value.feasible == 2,
        "equal economic intent chooses lower cost graph");
    candidates[1].targets[0].minimum_available = 0;
    check(compare_execution_graphs(candidates).error == Error::domain,
        "cannot make worse terminal target look like cheaper route");
    candidates[0].asset_count = 33;
    check(compare_execution_graphs(candidates).error == Error::capacity,
        "comparison validates counts before accessing fixed arrays");
    graph = graph_example();
    ExecutionProjection output{};
    check(machine_economics_evaluate(20, &graph, sizeof(graph), &output, sizeof(output)) == 0 &&
        output.count == 2, "execution compiler exported through checked ABI");
}

} // namespace

int main() {
    arithmetic();
    oracle();
    lending();
    derivatives();
    amm();
    allocation();
    routing();
    statistics();
    transition();
    economic_state();
    economic_strategy();
    economic_runtime();
    execution_graph();
    abi();
    std::cout << "{\"checks\":" << checks << ",\"passed\":true}" << '\n';
}
