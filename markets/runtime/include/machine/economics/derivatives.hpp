#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

struct LinearPosition {
    std::uint64_t instrument{};
    std::int64_t signed_quantity{};
    std::int64_t entry_price{};
    std::int64_t mark_price{};
    std::int64_t collateral{};
    std::int64_t accrued_funding{};
    std::int64_t unpaid_fees{};
    std::int64_t initial_margin_rate{};
    std::int64_t maintenance_margin_rate{};
};

struct PositionRisk {
    std::int64_t absolute_quantity{};
    std::int64_t notional{};
    std::int64_t unrealized_pnl{};
    std::int64_t equity{};
    std::int64_t initial_margin{};
    std::int64_t maintenance_margin{};
    std::int64_t margin_headroom{};
    std::int64_t leverage{};
    bool liquidatable{};
};

[[nodiscard]] inline Result<PositionRisk> linear_risk(const LinearPosition& position) noexcept {
    if (!position.instrument || position.entry_price <= 0 || position.mark_price <= 0 ||
        position.collateral < 0 || position.unpaid_fees < 0 || position.initial_margin_rate <= 0 ||
        position.initial_margin_rate > scale || position.maintenance_margin_rate < 0 ||
        position.maintenance_margin_rate > position.initial_margin_rate) {
        return failure<PositionRisk>(Error::invalid);
    }
    const auto quantity = absolute(position.signed_quantity);
    if (!quantity) {
        return failure<PositionRisk>(quantity.error);
    }
    const auto notional = multiply(quantity.value, position.mark_price, Rounding::ceil);
    const auto pnl = multiply(position.signed_quantity, position.mark_price - position.entry_price, Rounding::floor);
    if (!notional || !pnl) {
        return failure<PositionRisk>(Error::overflow);
    }
    // Positive accrued funding is a liability; negative funding is a receipt.
    const auto equity = narrow(static_cast<Wide>(position.collateral) + pnl.value -
        position.accrued_funding - position.unpaid_fees);
    const auto initial = multiply(notional.value, position.initial_margin_rate, Rounding::ceil);
    const auto maintenance = multiply(notional.value, position.maintenance_margin_rate, Rounding::ceil);
    if (!equity || !initial || !maintenance) {
        return failure<PositionRisk>(Error::overflow);
    }
    const auto headroom = subtract(equity.value, maintenance.value);
    if (!headroom) {
        return failure<PositionRisk>(headroom.error);
    }
    const auto leverage = equity.value > 0 ? ratio(notional.value, equity.value, Rounding::ceil)
        : success<std::int64_t>(0);
    if (!leverage) {
        return failure<PositionRisk>(leverage.error);
    }
    return success(PositionRisk{quantity.value, notional.value, pnl.value, equity.value,
        initial.value, maintenance.value, headroom.value, leverage.value,
        quantity.value != 0 && equity.value <= maintenance.value});
}

struct FundingCashFlow {
    std::int64_t notional{};
    std::int64_t signed_payment{};
};

[[nodiscard]] inline Result<FundingCashFlow> linear_funding(
    std::int64_t signed_quantity,
    std::int64_t settlement_price,
    std::int64_t funding_rate
) noexcept {
    if (settlement_price <= 0 || funding_rate < -scale || funding_rate > scale) {
        return failure<FundingCashFlow>(Error::invalid);
    }
    const auto quantity = absolute(signed_quantity);
    if (!quantity) {
        return failure<FundingCashFlow>(quantity.error);
    }
    const auto notional = multiply(quantity.value, settlement_price, Rounding::ceil);
    if (!notional) {
        return failure<FundingCashFlow>(notional.error);
    }
    const auto signed_rate = signed_quantity < 0 ? -funding_rate : funding_rate;
    // Payable funding rounds up; receivable magnitude rounds down.
    const auto payment = multiply(notional.value, signed_rate, Rounding::ceil);
    if (!payment) {
        return failure<FundingCashFlow>(payment.error);
    }
    return success(FundingCashFlow{notional.value, payment.value});
}

struct MarginTier {
    std::int64_t notional_cap{};
    std::int64_t maintenance_rate{};
    std::int64_t maintenance_deduction{};
    std::int64_t initial_rate{};
};

struct TierMargin {
    std::uint32_t tier{};
    std::int64_t initial{};
    std::int64_t maintenance{};
};

[[nodiscard]] inline Result<TierMargin> tier_margin(
    std::int64_t notional,
    std::span<const MarginTier> tiers
) noexcept {
    if (notional < 0 || tiers.empty() || tiers.size() > 32) {
        return failure<TierMargin>(Error::invalid);
    }
    std::int64_t previous_cap = 0;
    std::int64_t previous_rate = 0;
    std::int64_t previous_maintenance = 0;
    std::int64_t previous_initial_rate = 0;
    for (const auto& tier : tiers) {
        if (tier.notional_cap <= previous_cap || tier.maintenance_rate < previous_rate ||
            tier.maintenance_rate > scale || tier.maintenance_deduction < 0 ||
            tier.initial_rate < tier.maintenance_rate || tier.initial_rate > scale ||
            tier.initial_rate < previous_initial_rate) {
            return failure<TierMargin>(Error::invalid);
        }
        const auto boundary = multiply(previous_cap, tier.maintenance_rate, Rounding::ceil);
        if (!boundary) {
            return failure<TierMargin>(boundary.error);
        }
        if (tier.maintenance_deduction > boundary.value ||
            boundary.value - tier.maintenance_deduction < previous_maintenance) {
            return failure<TierMargin>(Error::invalid);
        }
        const auto cap = multiply(tier.notional_cap, tier.maintenance_rate, Rounding::ceil);
        if (!cap) {
            return failure<TierMargin>(cap.error);
        }
        previous_cap = tier.notional_cap;
        previous_rate = tier.maintenance_rate;
        previous_initial_rate = tier.initial_rate;
        previous_maintenance = cap.value - tier.maintenance_deduction;
    }
    for (std::size_t i = 0; i < tiers.size(); ++i) {
        const auto& tier = tiers[i];
        if (notional > tier.notional_cap) {
            continue;
        }
        const auto initial = multiply(notional, tier.initial_rate, Rounding::ceil);
        const auto maintenance = multiply(notional, tier.maintenance_rate, Rounding::ceil);
        if (!initial || !maintenance) {
            return failure<TierMargin>(Error::overflow);
        }
        if (maintenance.value < tier.maintenance_deduction) {
            return failure<TierMargin>(Error::invalid);
        }
        return success(TierMargin{static_cast<std::uint32_t>(i), initial.value,
            maintenance.value - tier.maintenance_deduction});
    }
    return failure<TierMargin>(Error::capacity);
}

struct Reduction {
    std::int64_t quantity_to_close{};
    std::int64_t signed_quantity_after{};
    std::int64_t realized_pnl{};
    std::int64_t execution_fee{};
    std::int64_t collateral_after{};
    PositionRisk risk_after{};
};

[[nodiscard]] inline Result<Reduction> simulate_reduction(
    const LinearPosition& position,
    std::int64_t close_quantity,
    std::int64_t execution_price,
    std::int64_t fee_rate
) noexcept {
    const auto before = linear_risk(position);
    if (!before) {
        return failure<Reduction>(before.error);
    }
    if (close_quantity <= 0 || close_quantity > before.value.absolute_quantity ||
        execution_price <= 0 || fee_rate < 0 || fee_rate > scale) {
        return failure<Reduction>(Error::invalid);
    }
    const auto closed_signed = position.signed_quantity < 0 ? -close_quantity : close_quantity;
    const auto realized = multiply(closed_signed, execution_price - position.entry_price, Rounding::floor);
    const auto notional = multiply(close_quantity, execution_price, Rounding::ceil);
    if (!realized || !notional) {
        return failure<Reduction>(Error::overflow);
    }
    const auto fee = multiply(notional.value, fee_rate, Rounding::ceil);
    if (!fee) {
        return failure<Reduction>(fee.error);
    }
    const auto collateral = narrow(static_cast<Wide>(position.collateral) + realized.value -
        fee.value - position.unpaid_fees - position.accrued_funding);
    if (!collateral) {
        return failure<Reduction>(collateral.error);
    }
    if (collateral.value < 0) {
        return failure<Reduction>(Error::collateral);
    }
    auto after = position;
    after.signed_quantity -= closed_signed;
    after.collateral = collateral.value;
    after.unpaid_fees = 0;
    after.accrued_funding = 0;
    const auto risk = linear_risk(after);
    if (!risk) {
        return failure<Reduction>(risk.error);
    }
    return success(Reduction{close_quantity, after.signed_quantity, realized.value, fee.value,
        collateral.value, risk.value});
}

struct StressRisk {
    std::int64_t worst_equity{};
    std::int64_t worst_headroom{};
    std::uint32_t worst_scenario{};
    bool any_liquidatable{};
};

[[nodiscard]] inline Result<StressRisk> stress_linear(
    const LinearPosition& position,
    std::span<const std::int64_t> mark_shocks_bps
) noexcept {
    const auto before = linear_risk(position);
    if (!before) {
        return failure<StressRisk>(before.error);
    }
    if (mark_shocks_bps.empty() || mark_shocks_bps.size() > 32) {
        return failure<StressRisk>(Error::invalid);
    }
    StressRisk result{INT64_MAX, INT64_MAX, 0, false};
    for (std::size_t i = 0; i < mark_shocks_bps.size(); ++i) {
        const auto shock = mark_shocks_bps[i];
        if (shock <= -bps_scale || shock > 10 * bps_scale) {
            return failure<StressRisk>(Error::invalid);
        }
        const auto mark = basis_points(position.mark_price, bps_scale + shock,
            position.signed_quantity < 0 ? Rounding::ceil : Rounding::floor);
        if (!mark || mark.value <= 0) {
            return failure<StressRisk>(Error::overflow);
        }
        auto shocked = position;
        shocked.mark_price = mark.value;
        const auto risk = linear_risk(shocked);
        if (!risk) {
            return failure<StressRisk>(risk.error);
        }
        result.worst_equity = std::min(result.worst_equity, risk.value.equity);
        if (risk.value.margin_headroom < result.worst_headroom) {
            result.worst_headroom = risk.value.margin_headroom;
            result.worst_scenario = static_cast<std::uint32_t>(i);
        }
        result.any_liquidatable |= risk.value.liquidatable;
    }
    return success(result);
}

// Estimates one isolated, linear, constant-maintenance-rate threshold. Cross
// margin, inverse contracts, insurance funds and exchange liquidation fees
// are outside this model and cannot reuse this threshold as venue evidence.
[[nodiscard]] inline Result<std::int64_t> isolated_liquidation_price(
    const LinearPosition& position
) noexcept {
    const auto risk = linear_risk(position);
    if (!risk) {
        return failure<std::int64_t>(risk.error);
    }
    if (!position.signed_quantity || position.maintenance_margin_rate == scale) {
        return failure<std::int64_t>(Error::unsupported);
    }
    const auto entry_notional = multiply(risk.value.absolute_quantity, position.entry_price, Rounding::ceil);
    if (!entry_notional) {
        return failure<std::int64_t>(entry_notional.error);
    }
    const auto available = narrow(static_cast<Wide>(position.collateral) - position.accrued_funding - position.unpaid_fees);
    if (!available) {
        return failure<std::int64_t>(available.error);
    }
    const auto numerator = position.signed_quantity > 0
        ? subtract(entry_notional.value, available.value)
        : add(entry_notional.value, available.value);
    if (!numerator) {
        return failure<std::int64_t>(numerator.error);
    }
    if (numerator.value <= 0) {
        return success<std::int64_t>(0);
    }
    const auto denominator = multiply(risk.value.absolute_quantity,
        position.signed_quantity > 0 ? scale - position.maintenance_margin_rate
            : scale + position.maintenance_margin_rate,
        position.signed_quantity > 0 ? Rounding::floor : Rounding::ceil);
    if (!denominator) {
        return failure<std::int64_t>(denominator.error);
    }
    return ratio(numerator.value, denominator.value,
        position.signed_quantity > 0 ? Rounding::ceil : Rounding::floor);
}

} // namespace machine::economics
