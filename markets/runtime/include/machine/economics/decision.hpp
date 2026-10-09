#pragma once

#include "machine/economics/derivatives.hpp"
#include "machine/economics/lending.hpp"
#include "machine/economics/oracle.hpp"

namespace machine::economics {

enum class EconomicAction : std::uint32_t {
    none,
    hold,
    reduce,
    hedge,
    repay,
    borrow,
    allocate,
    swap,
    cancel,
    escalate,
    abort,
};

enum class Reason : std::uint32_t {
    none,
    healthy,
    margin_buffer,
    debt_buffer,
    cheapest_feasible_recovery,
    no_allowed_recovery,
    stale_state,
    missing_evidence,
    unsupported_product,
    insufficient_cash,
    cost_limit,
    search_limit,
    oracle_disagreement,
};

struct ExecutionVenue {
    std::uint64_t id{};
    std::int64_t executable_price{};
    std::int64_t available_quantity{};
    std::int64_t quantity_step{};
    std::int64_t fee_rate{};
    std::int64_t fixed_cost{};
    std::int64_t min_notional{};
    bool allowed{};
    Envelope envelope{};
};

struct RecoveryPolicy {
    std::int64_t minimum_equity{};
    std::int64_t target_margin_headroom{};
    std::int64_t maximum_leverage{};
    std::int64_t maximum_close_quantity{};
    std::int64_t maximum_total_cost{};
    std::int64_t max_slippage_bps{};
    std::int64_t maximum_close_fraction{};
    std::uint32_t maximum_lots_examined{};
    std::uint64_t deadline_ns{};
};

struct RecoveryChoice {
    EconomicAction action{EconomicAction::escalate};
    Reason reason{Reason::no_allowed_recovery};
    Error error{Error::unknown};
    std::uint64_t venue_id{};
    std::int64_t close_quantity{};
    std::int64_t execution_price{};
    std::int64_t total_cost{};
    std::int64_t equity_after{};
    std::int64_t headroom_after{};
    std::int64_t leverage_after{};
    std::uint64_t valid_until_ns{};
    std::uint32_t examined{};
    std::uint32_t feasible{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline bool meets_recovery(
    const PositionRisk& risk,
    const RecoveryPolicy& policy
) noexcept {
    if (risk.equity < policy.minimum_equity || risk.margin_headroom < policy.target_margin_headroom ||
        risk.liquidatable) {
        return false;
    }
    if (risk.notional && (risk.equity <= 0 || risk.leverage > policy.maximum_leverage)) {
        return false;
    }
    return true;
}

// No learned signal or phrase is treated as an order. This bounded search
// simulates actual close candidates, checks the post-state, and picks the
// cheapest feasible reduction with deterministic tie breaking. The retained
// margin on closure matches this declared isolated simulation; venue-specific
// collateral release remains the external adapter's responsibility.
[[nodiscard]] inline Result<RecoveryChoice> decide_derivative_recovery(
    const LinearPosition& position,
    const RecoveryPolicy& policy,
    std::span<const ExecutionVenue> venues,
    const Envelope& state
) noexcept {
    const auto state_error = validate(state);
    if (state_error != Error::okay) {
        return failure<RecoveryChoice>(state_error);
    }
    const auto before = linear_risk(position);
    if (!before) {
        return failure<RecoveryChoice>(before.error);
    }
    if (policy.minimum_equity < 0 || policy.target_margin_headroom < 0 || policy.maximum_leverage <= 0 ||
        policy.maximum_close_quantity < 0 || policy.maximum_total_cost < 0 || policy.max_slippage_bps < 0 ||
        policy.max_slippage_bps > bps_scale || policy.maximum_close_fraction <= 0 ||
        policy.maximum_close_fraction > scale || !policy.maximum_lots_examined ||
        policy.maximum_lots_examined > 4096 || policy.deadline_ns <= state.now_ns ||
        venues.size() > 16) {
        return failure<RecoveryChoice>(Error::invalid);
    }
    const auto state_until = expiry(state);
    if (!state_until) {
        return failure<RecoveryChoice>(state_until.error);
    }
    RecoveryChoice result{};
    result.valid_until_ns = std::min(state_until.value, policy.deadline_ns);
    if (meets_recovery(before.value, policy)) {
        result.action = EconomicAction::hold;
        result.reason = Reason::healthy;
        result.error = Error::okay;
        result.equity_after = before.value.equity;
        result.headroom_after = before.value.margin_headroom;
        result.leverage_after = before.value.leverage;
        return success(result);
    }
    const auto fraction_limit = multiply(before.value.absolute_quantity, policy.maximum_close_fraction, Rounding::floor);
    if (!fraction_limit) {
        return failure<RecoveryChoice>(fraction_limit.error);
    }
    bool found = false;
    for (std::size_t index = 0; index < venues.size(); ++index) {
        const auto& venue = venues[index];
        if (!venue.id || venue.executable_price <= 0 || venue.available_quantity < 0 ||
            venue.quantity_step <= 0 || venue.fee_rate < 0 || venue.fee_rate > scale ||
            venue.fixed_cost < 0 || venue.min_notional < 0) {
            return failure<RecoveryChoice>(Error::invalid);
        }
        for (std::size_t j = 0; j < index; ++j) {
            if (venues[j].id == venue.id) {
                return failure<RecoveryChoice>(Error::conflict);
            }
        }
        const auto venue_error = validate(venue.envelope);
        if (!venue.allowed || venue_error != Error::okay || venue.envelope.now_ns != state.now_ns ||
            venue.envelope.policy_version != state.policy_version) {
            continue;
        }
        const auto adverse = position.signed_quantity > 0
            ? std::max<std::int64_t>(0, position.mark_price - venue.executable_price)
            : std::max<std::int64_t>(0, venue.executable_price - position.mark_price);
        const auto slippage = divide_wide(static_cast<Wide>(adverse) * bps_scale,
            position.mark_price, Rounding::ceil);
        if (!slippage) {
            return failure<RecoveryChoice>(slippage.error);
        }
        if (slippage.value > policy.max_slippage_bps) {
            continue;
        }
        const auto maximum = std::min({before.value.absolute_quantity, policy.maximum_close_quantity,
            fraction_limit.value, venue.available_quantity});
        const auto lots = maximum / venue.quantity_step;
        if (lots > policy.maximum_lots_examined - result.examined) {
            // Do not return a cheaper-looking plan from a partial search.
            return failure<RecoveryChoice>(Error::incomplete);
        }
        for (std::int64_t lot = 1; lot <= lots; ++lot) {
            ++result.examined;
            const auto close = lot * venue.quantity_step;
            const auto quote_notional = multiply(close, venue.executable_price, Rounding::ceil);
            if (!quote_notional) {
                return failure<RecoveryChoice>(quote_notional.error);
            }
            if (quote_notional.value < venue.min_notional) {
                continue;
            }
            const auto reduction = simulate_reduction(position, close, venue.executable_price, venue.fee_rate);
            if (!reduction) {
                if (reduction.error == Error::overflow || reduction.error == Error::invalid) {
                    return failure<RecoveryChoice>(reduction.error);
                }
                continue;
            }
            auto risk = reduction.value.risk_after;
            if (venue.fixed_cost > risk.equity) {
                continue;
            }
            risk.equity -= venue.fixed_cost;
            risk.margin_headroom -= venue.fixed_cost;
            if (risk.notional) {
                if (risk.equity <= 0) {
                    continue;
                }
                const auto leverage = ratio(risk.notional, risk.equity, Rounding::ceil);
                if (!leverage) {
                    return failure<RecoveryChoice>(leverage.error);
                }
                risk.leverage = leverage.value;
            }
            const auto loss = multiply(close, adverse, Rounding::ceil);
            if (!loss) {
                return failure<RecoveryChoice>(loss.error);
            }
            const auto total = narrow(static_cast<Wide>(loss.value) + reduction.value.execution_fee + venue.fixed_cost);
            if (!total) {
                return failure<RecoveryChoice>(total.error);
            }
            if (total.value > policy.maximum_total_cost || !meets_recovery(risk, policy)) {
                continue;
            }
            ++result.feasible;
            const bool better = !found || total.value < result.total_cost ||
                (total.value == result.total_cost && (close < result.close_quantity ||
                (close == result.close_quantity && venue.id < result.venue_id)));
            if (!better) {
                continue;
            }
            const auto until = expiry(venue.envelope);
            if (!until) {
                return failure<RecoveryChoice>(until.error);
            }
            result.action = EconomicAction::reduce;
            result.reason = Reason::cheapest_feasible_recovery;
            result.error = Error::okay;
            result.venue_id = venue.id;
            result.close_quantity = close;
            result.execution_price = venue.executable_price;
            result.total_cost = total.value;
            result.equity_after = risk.equity;
            result.headroom_after = risk.margin_headroom;
            result.leverage_after = risk.leverage;
            result.valid_until_ns = std::min({state_until.value, policy.deadline_ns, until.value});
            found = true;
        }
    }
    if (!found) {
        result.action = EconomicAction::escalate;
        result.reason = Reason::no_allowed_recovery;
        result.error = Error::infeasible;
    }
    return success(result);
}

struct RepayPolicy {
    std::int64_t target_health_factor{};
    std::int64_t max_payment_value{};
    std::int64_t available_cash_value{};
    std::int64_t cash_reserve_value{};
    std::int64_t maximum_cost{};
    std::int64_t execution_cost{};
};

struct RepayChoice {
    EconomicAction action{EconomicAction::escalate};
    Reason reason{Reason::no_allowed_recovery};
    std::int64_t payment_value{};
    std::int64_t debt_value_after{};
    std::int64_t cash_value_after{};
    std::int64_t health_factor_after{};
    bool debt_free_after{};
    std::uint64_t valid_until_ns{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline Result<RepayChoice> decide_repayment(
    const Health& health,
    const RepayPolicy& policy,
    const Envelope& state
) noexcept {
    const auto until = expiry(state);
    if (!until) {
        return failure<RepayChoice>(until.error);
    }
    if (health.debt_value < 0 || health.liquidation_capacity < 0 ||
        health.debt_free != (health.debt_value == 0) || policy.target_health_factor <= scale ||
        policy.max_payment_value < 0 || policy.available_cash_value < 0 || policy.cash_reserve_value < 0 ||
        policy.cash_reserve_value > policy.available_cash_value || policy.maximum_cost < 0 ||
        policy.execution_cost < 0) {
        return failure<RepayChoice>(Error::invalid);
    }
    RepayChoice result{};
    result.valid_until_ns = until.value;
    result.debt_value_after = health.debt_value;
    result.cash_value_after = policy.available_cash_value;
    result.debt_free_after = health.debt_free;
    const auto current_factor = health.debt_free ? success<std::int64_t>(0)
        : ratio(health.liquidation_capacity, health.debt_value, Rounding::floor);
    if (!current_factor) {
        return failure<RepayChoice>(current_factor.error);
    }
    result.health_factor_after = current_factor.value;
    if (health.debt_free || current_factor.value >= policy.target_health_factor) {
        result.action = EconomicAction::hold;
        result.reason = Reason::healthy;
        return success(result);
    }
    if (policy.execution_cost > policy.maximum_cost) {
        result.reason = Reason::cost_limit;
        return success(result);
    }
    const auto target_debt = divide_wide(static_cast<Wide>(health.liquidation_capacity) * scale,
        policy.target_health_factor, Rounding::floor);
    if (!target_debt) {
        return failure<RepayChoice>(target_debt.error);
    }
    const auto required = health.debt_value - target_debt.value;
    const auto needed = add(required, policy.execution_cost);
    if (!needed) {
        return failure<RepayChoice>(needed.error);
    }
    if (required > policy.max_payment_value || needed.value > policy.available_cash_value - policy.cash_reserve_value) {
        result.reason = Reason::insufficient_cash;
        return success(result);
    }
    result.payment_value = required;
    result.debt_value_after = target_debt.value;
    result.cash_value_after = policy.available_cash_value - needed.value;
    result.debt_free_after = target_debt.value == 0;
    if (!result.debt_free_after) {
        const auto factor = ratio(health.liquidation_capacity, target_debt.value, Rounding::floor);
        if (!factor) {
            return failure<RepayChoice>(factor.error);
        }
        result.health_factor_after = factor.value;
        if (factor.value < policy.target_health_factor) {
            return failure<RepayChoice>(Error::infeasible);
        }
    } else {
        result.health_factor_after = 0;
    }
    result.action = EconomicAction::repay;
    result.reason = Reason::debt_buffer;
    return success(result);
}

struct RebalanceTerms {
    std::int64_t current_expected_income{};
    std::int64_t proposed_expected_income{};
    std::int64_t trading_cost{};
    std::int64_t exit_cost{};
    std::int64_t settlement_cost{};
    std::int64_t model_uncertainty_buffer{};
    std::int64_t minimum_improvement{};
    std::uint64_t last_rebalance_ns{};
    std::uint64_t cooldown_ns{};
};

struct RebalanceChoice {
    EconomicAction action{EconomicAction::hold};
    Reason reason{Reason::healthy};
    std::int64_t gross_improvement{};
    std::int64_t total_cost{};
    std::int64_t conservative_improvement{};
    std::uint64_t valid_until_ns{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline Result<RebalanceChoice> decide_rebalance(
    const RebalanceTerms& terms,
    const Envelope& state
) noexcept {
    const auto until = expiry(state);
    if (!until) {
        return failure<RebalanceChoice>(until.error);
    }
    if (terms.trading_cost < 0 || terms.exit_cost < 0 || terms.settlement_cost < 0 ||
        terms.model_uncertainty_buffer < 0 || terms.minimum_improvement < 0 ||
        terms.last_rebalance_ns > state.now_ns) {
        return failure<RebalanceChoice>(Error::invalid);
    }
    const auto gross = subtract(terms.proposed_expected_income, terms.current_expected_income);
    const auto cost = narrow(static_cast<Wide>(terms.trading_cost) + terms.exit_cost + terms.settlement_cost);
    if (!gross || !cost) {
        return failure<RebalanceChoice>(Error::overflow);
    }
    const auto improvement = narrow(static_cast<Wide>(gross.value) - cost.value - terms.model_uncertainty_buffer);
    if (!improvement) {
        return failure<RebalanceChoice>(improvement.error);
    }
    RebalanceChoice result{};
    result.gross_improvement = gross.value;
    result.total_cost = cost.value;
    result.conservative_improvement = improvement.value;
    result.valid_until_ns = until.value;
    if (state.now_ns - terms.last_rebalance_ns < terms.cooldown_ns) {
        return success(result);
    }
    if (improvement.value > terms.minimum_improvement) {
        result.action = EconomicAction::allocate;
        result.reason = Reason::cheapest_feasible_recovery;
    }
    return success(result);
}

} // namespace machine::economics
