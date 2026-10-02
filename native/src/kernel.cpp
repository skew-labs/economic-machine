#include "machine/kernel.hpp"

#include <algorithm>

namespace machine {

Output evaluate(const Input& i) noexcept {
    Output result{abi_version, static_cast<std::uint32_t>(Code::invalid), i.sequence, 0, 0, 0, 0};
    const auto fail = [&](Code code) {
        result.code = static_cast<std::uint32_t>(code);
        return result;
    };
    if (i.version != abi_version || (i.side != 1 && i.side != 2) || i.quantity <= 0 ||
        i.price <= 0 || i.reference_price <= 0 || i.quantity_step <= 0 || i.price_tick <= 0 ||
        i.min_notional < 0 || i.max_order <= 0 || i.available_quote < 0 || i.available_base < 0 ||
        i.exposure_after < 0 || i.max_exposure < 0 || i.turnover_remaining < 0 ||
        i.max_slippage_bps > 10000 || i.fee_reserve_bps > 10000 || !i.now_ns || !i.sequence ||
        !i.max_age_ns || i.policy_active > 1 || i.oracle_valid > 1) return fail(Code::invalid);
    if (!i.policy_active || !i.oracle_valid) return fail(Code::disabled);
    if (i.now_ns >= i.deadline_ns) return fail(Code::expired);
    if (!i.observed_ns || i.observed_ns > i.now_ns || i.now_ns - i.observed_ns > i.max_age_ns)
        return fail(Code::stale);
    if (i.sequence != i.expected_sequence) return fail(Code::sequence);
    if (i.quantity % i.quantity_step || i.price % i.price_tick) return fail(Code::increment);
    const auto product = static_cast<__int128>(i.quantity) * i.price;
    // Round conservatively to quote atoms; never truncate a required debit.
    const auto notional = (product + amount_scale - 1) / amount_scale;
    if (notional > std::numeric_limits<std::int64_t>::max()) return fail(Code::overflow);
    result.notional = static_cast<std::int64_t>(notional);
    if (notional < i.min_notional || notional > i.max_order) return fail(Code::notional);
    const auto required = (notional * (10000 + i.fee_reserve_bps) + 9999) / 10000;
    if (required > std::numeric_limits<std::int64_t>::max()) return fail(Code::overflow);
    result.required_quote = static_cast<std::int64_t>(required);
    if ((i.side == static_cast<std::uint32_t>(Side::buy) && required > i.available_quote) ||
        (i.side == static_cast<std::uint32_t>(Side::sell) && i.quantity > i.available_base))
        return fail(Code::balance);
    if (i.exposure_after > i.max_exposure) return fail(Code::exposure);
    const auto adverse = i.side == 1 ? static_cast<__int128>(i.price) - i.reference_price :
                                      static_cast<__int128>(i.reference_price) - i.price;
    if (adverse > 0 && adverse * 10000 > static_cast<__int128>(i.reference_price) * i.max_slippage_bps)
        return fail(Code::slippage);
    if (notional > i.turnover_remaining) return fail(Code::turnover);
    const auto max_time = std::numeric_limits<std::uint64_t>::max();
    const auto oracle_deadline = i.observed_ns > max_time - i.max_age_ns ? max_time : i.observed_ns + i.max_age_ns;
    result.valid_until_ns = std::min(i.deadline_ns, oracle_deadline);
    result.code = static_cast<std::uint32_t>(Code::admit);
    return result;
}

} // namespace machine

extern "C" {
std::uint32_t machine_abi_version() noexcept { return machine::abi_version; }
std::size_t machine_input_size() noexcept { return sizeof(machine::Input); }
std::size_t machine_output_size() noexcept { return sizeof(machine::Output); }
int machine_evaluate(const machine::Input* input, machine::Output* output) noexcept {
    if (!input || !output) return -1;
    *output = machine::evaluate(*input);
    return 0;
}
}
