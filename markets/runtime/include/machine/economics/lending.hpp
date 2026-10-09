#pragma once

#include "machine/economics/value.hpp"

namespace machine::economics {

struct LendingMarket {
    std::int64_t cash{};
    std::int64_t borrowed{};
    std::int64_t reserves{};
    std::int64_t base_apr{};
    std::int64_t slope_apr{};
    std::int64_t jump_apr{};
    std::int64_t kink{};
    std::int64_t reserve_factor{};
};

struct LendingRates {
    std::int64_t utilization{};
    std::int64_t borrow_apr{};
    std::int64_t supply_apr{};
    std::int64_t available_cash{};
};

[[nodiscard]] inline Result<LendingRates> lending_rates(const LendingMarket& market) noexcept {
    if (market.cash < 0 || market.borrowed < 0 || market.reserves < 0 || market.base_apr < 0 ||
        market.slope_apr < 0 || market.jump_apr < 0 || market.kink <= 0 || market.kink > scale ||
        market.reserve_factor < 0 || market.reserve_factor > scale) {
        return failure<LendingRates>(Error::invalid);
    }
    const auto gross = add(market.cash, market.borrowed);
    if (!gross) {
        return failure<LendingRates>(gross.error);
    }
    if (market.reserves > gross.value) {
        return failure<LendingRates>(Error::collateral);
    }
    const auto assets = gross.value - market.reserves;
    if (!assets && market.borrowed) {
        return failure<LendingRates>(Error::collateral);
    }
    const auto utilization = !market.borrowed ? success<std::int64_t>(0) : ratio(
        market.borrowed, assets, Rounding::ceil
    );
    if (!utilization) {
        return failure<LendingRates>(utilization.error);
    }
    if (utilization.value > scale) {
        return failure<LendingRates>(Error::collateral);
    }
    const auto normal = multiply(std::min(utilization.value, market.kink), market.slope_apr, Rounding::ceil);
    const auto jump = multiply(std::max<std::int64_t>(0, utilization.value - market.kink), market.jump_apr, Rounding::ceil);
    if (!normal || !jump) {
        return failure<LendingRates>(Error::overflow);
    }
    const auto borrow = narrow(static_cast<Wide>(market.base_apr) + normal.value + jump.value);
    if (!borrow) {
        return failure<LendingRates>(borrow.error);
    }
    const auto gross_supplier = multiply(borrow.value, utilization.value, Rounding::floor);
    if (!gross_supplier) {
        return failure<LendingRates>(gross_supplier.error);
    }
    const auto supply = multiply(gross_supplier.value, scale - market.reserve_factor, Rounding::floor);
    if (!supply) {
        return failure<LendingRates>(supply.error);
    }
    return success(LendingRates{utilization.value, borrow.value, supply.value, market.cash});
}

// APR * duration: simple interest only. This does not promise APY, incentive
// persistence or compounding. Expenses round upward and receipts downward.
[[nodiscard]] inline Result<std::int64_t> simple_interest(
    std::int64_t principal,
    std::int64_t annual_rate,
    std::int64_t duration_seconds,
    bool expense
) noexcept {
    constexpr std::int64_t year = 365 * 86400;
    if (principal < 0 || annual_rate < 0 || duration_seconds < 0 || duration_seconds > 10 * year) {
        return failure<std::int64_t>(Error::invalid);
    }
    // Three full-width inputs cannot share an unchecked 128-bit product.
    // Bound the scaled principal-rate product before applying time.
    const auto annual = multiply(principal, annual_rate, expense ? Rounding::ceil : Rounding::floor);
    if (!annual) {
        return annual;
    }
    return divide_wide(static_cast<Wide>(annual.value) * duration_seconds, year,
        expense ? Rounding::ceil : Rounding::floor);
}

struct YieldTerms {
    std::int64_t principal{};
    std::int64_t base_apr{};
    std::int64_t incentive_apr{};
    std::int64_t incentive_retention{};
    std::int64_t exit_haircut{};
    std::int64_t entry_fee{};
    std::int64_t exit_fee{};
    std::int64_t horizon_seconds{};
    std::int64_t lock_seconds{};
    std::int64_t exit_available{};
};

struct YieldEstimate {
    std::int64_t base_income{};
    std::int64_t incentive_income{};
    std::int64_t exit_loss{};
    std::int64_t fees{};
    std::int64_t net_income{};
    std::int64_t terminal_value{};
};

[[nodiscard]] inline Result<YieldEstimate> forward_yield(const YieldTerms& terms) noexcept {
    if (terms.principal <= 0 || terms.base_apr < 0 || terms.incentive_apr < 0 ||
        terms.incentive_retention < 0 || terms.incentive_retention > scale ||
        terms.exit_haircut < 0 || terms.exit_haircut > scale || terms.entry_fee < 0 ||
        terms.exit_fee < 0 || terms.horizon_seconds <= 0 || terms.lock_seconds < 0 ||
        terms.exit_available < 0) {
        return failure<YieldEstimate>(Error::invalid);
    }
    if (terms.horizon_seconds < terms.lock_seconds || terms.exit_available < terms.principal) {
        return failure<YieldEstimate>(Error::illiquid);
    }
    const auto retained = multiply(terms.incentive_apr, terms.incentive_retention, Rounding::floor);
    if (!retained) {
        return failure<YieldEstimate>(retained.error);
    }
    const auto base = simple_interest(terms.principal, terms.base_apr, terms.horizon_seconds, false);
    const auto incentive = simple_interest(terms.principal, retained.value, terms.horizon_seconds, false);
    const auto loss = multiply(terms.principal, terms.exit_haircut, Rounding::ceil);
    const auto fees = add(terms.entry_fee, terms.exit_fee);
    if (!base || !incentive || !loss || !fees) {
        return failure<YieldEstimate>(Error::overflow);
    }
    const auto net = narrow(static_cast<Wide>(base.value) + incentive.value - loss.value - fees.value);
    if (!net) {
        return failure<YieldEstimate>(net.error);
    }
    const auto terminal = add(terms.principal, net.value);
    if (!terminal) {
        return failure<YieldEstimate>(terminal.error);
    }
    if (terminal.value < 0) {
        return failure<YieldEstimate>(Error::collateral);
    }
    return success(YieldEstimate{base.value, incentive.value, loss.value, fees.value, net.value, terminal.value});
}

struct CollateralAsset {
    std::uint64_t asset{};
    std::int64_t quantity{};
    std::int64_t conservative_price{};
    std::int64_t collateral_factor{};
    std::int64_t liquidation_factor{};
};

struct DebtAsset {
    std::uint64_t asset{};
    std::int64_t principal{};
    std::int64_t accrued_interest{};
    std::int64_t conservative_price{};
};

struct Health {
    std::int64_t collateral_value{};
    std::int64_t borrow_capacity{};
    std::int64_t liquidation_capacity{};
    std::int64_t debt_value{};
    std::int64_t headroom{};
    std::int64_t health_factor{};
    bool debt_free{};
    bool liquidatable{};
};

[[nodiscard]] inline Result<Health> lending_health(
    std::span<const CollateralAsset> collateral,
    std::span<const DebtAsset> debts
) noexcept {
    if (collateral.size() > 32 || debts.size() > 32) {
        return failure<Health>(Error::capacity);
    }
    Health result{};
    for (std::size_t i = 0; i < collateral.size(); ++i) {
        const auto& asset = collateral[i];
        if (!asset.asset || asset.quantity < 0 || asset.conservative_price <= 0 ||
            asset.collateral_factor < 0 || asset.collateral_factor > scale ||
            asset.liquidation_factor < asset.collateral_factor || asset.liquidation_factor > scale) {
            return failure<Health>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (collateral[j].asset == asset.asset) {
                return failure<Health>(Error::conflict);
            }
        }
        const auto value = multiply(asset.quantity, asset.conservative_price, Rounding::floor);
        if (!value) {
            return failure<Health>(value.error);
        }
        const auto capacity = multiply(value.value, asset.collateral_factor, Rounding::floor);
        const auto liquidation = multiply(value.value, asset.liquidation_factor, Rounding::floor);
        if (!capacity || !liquidation) {
            return failure<Health>(Error::overflow);
        }
        const auto total = add(result.collateral_value, value.value);
        const auto borrow_total = add(result.borrow_capacity, capacity.value);
        const auto liquidation_total = add(result.liquidation_capacity, liquidation.value);
        if (!total || !borrow_total || !liquidation_total) {
            return failure<Health>(Error::overflow);
        }
        result.collateral_value = total.value;
        result.borrow_capacity = borrow_total.value;
        result.liquidation_capacity = liquidation_total.value;
    }
    for (std::size_t i = 0; i < debts.size(); ++i) {
        const auto& debt = debts[i];
        if (!debt.asset || debt.principal < 0 || debt.accrued_interest < 0 || debt.conservative_price <= 0) {
            return failure<Health>(Error::invalid);
        }
        for (std::size_t j = 0; j < i; ++j) {
            if (debts[j].asset == debt.asset) {
                return failure<Health>(Error::conflict);
            }
        }
        const auto amount = add(debt.principal, debt.accrued_interest);
        if (!amount) {
            return failure<Health>(amount.error);
        }
        const auto value = multiply(amount.value, debt.conservative_price, Rounding::ceil);
        if (!value) {
            return failure<Health>(value.error);
        }
        const auto total = add(result.debt_value, value.value);
        if (!total) {
            return failure<Health>(total.error);
        }
        result.debt_value = total.value;
    }
    const auto headroom = subtract(result.borrow_capacity, result.debt_value);
    if (!headroom) {
        return failure<Health>(headroom.error);
    }
    result.headroom = headroom.value;
    result.debt_free = result.debt_value == 0;
    // Infinity is represented by debt_free, not a silently overflowing ratio.
    if (!result.debt_free) {
        const auto factor = ratio(result.liquidation_capacity, result.debt_value, Rounding::floor);
        if (!factor) {
            return failure<Health>(factor.error);
        }
        result.health_factor = factor.value;
    }
    result.liquidatable = !result.debt_free && result.liquidation_capacity <= result.debt_value;
    return success(result);
}

struct Repayment {
    std::int64_t interest_paid{};
    std::int64_t principal_paid{};
    std::int64_t interest_remaining{};
    std::int64_t principal_remaining{};
    std::int64_t unused_cash{};
};

[[nodiscard]] inline Result<Repayment> repay(
    std::int64_t principal,
    std::int64_t interest,
    std::int64_t payment
) noexcept {
    if (principal < 0 || interest < 0 || payment < 0) {
        return failure<Repayment>(Error::invalid);
    }
    const auto total = add(principal, interest);
    if (!total) {
        return failure<Repayment>(total.error);
    }
    const auto paid = std::min(payment, total.value);
    const auto interest_paid = std::min(paid, interest);
    const auto principal_paid = paid - interest_paid;
    return success(Repayment{interest_paid, principal_paid, interest - interest_paid,
        principal - principal_paid, payment - paid});
}

} // namespace machine::economics
