#include "machine/program.hpp"

namespace machine {

static bool valid_unit(std::uint32_t unit) noexcept {
    return unit > 0 && unit <= static_cast<unsigned>(Unit::count);
}
static Unit multiply_unit(Unit a, Unit b) noexcept {
    if (a == Unit::scalar || a == Unit::rate) return b;
    if (b == Unit::scalar || b == Unit::rate) return a;
    if ((a == Unit::quantity && b == Unit::price) ||
        (a == Unit::price && b == Unit::quantity)) return Unit::money;
    return Unit::none;
}
static Unit divide_unit(Unit a, Unit b) noexcept {
    if (b == Unit::scalar || b == Unit::rate) return a;
    if (a == b) return Unit::scalar;
    if (a == Unit::money && b == Unit::price) return Unit::quantity;
    if (a == Unit::money && b == Unit::quantity) return Unit::price;
    return Unit::none;
}

Validation validate_program(const Program& p) noexcept {
    if (p.version != program_version || !p.count || p.count > instruction_count)
        return {ProgramCode::malformed, 0};
    std::array<Unit, register_count> units{};
    bool terminal = false;
    for (std::uint32_t pc = 0; pc < p.count; ++pc) {
        const auto& i = p.instructions[pc];
        const auto op = static_cast<Op>(i.opcode);
        if (terminal || i.destination >= register_count || i.a >= register_count ||
            i.b >= register_count || i.c >= register_count || !valid_unit(i.unit))
            return {ProgramCode::malformed, pc};
        const auto requested = static_cast<Unit>(i.unit);
        Unit output = Unit::none;
        const auto need = [&](unsigned reg) { return units[reg] != Unit::none; };
        switch (op) {
        case Op::constant:
            output = requested;
            if (output == Unit::boolean && i.immediate != 0 && i.immediate != 1)
                return {ProgramCode::malformed, pc};
            break;
        case Op::load:
            if (i.immediate < 0 || i.immediate >= static_cast<std::int64_t>(field_count) ||
                !valid_unit(p.field_units[i.immediate])) return {ProgramCode::malformed, pc};
            output = static_cast<Unit>(p.field_units[i.immediate]);
            break;
        case Op::add: case Op::subtract: case Op::minimum: case Op::maximum:
        case Op::less: case Op::less_equal: case Op::equal:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != units[i.b]) return {ProgramCode::unit_mismatch, pc};
            output = op == Op::less || op == Op::less_equal || op == Op::equal ? Unit::boolean : units[i.a];
            break;
        case Op::multiply_scaled: case Op::divide_scaled:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] == Unit::boolean || units[i.b] == Unit::boolean ||
                units[i.a] == Unit::count || units[i.b] == Unit::count ||
                units[i.a] == Unit::duration || units[i.b] == Unit::duration)
                return {ProgramCode::unit_mismatch, pc};
            output = op == Op::multiply_scaled ? multiply_unit(units[i.a], units[i.b]) : divide_unit(units[i.a], units[i.b]);
            break;
        case Op::logical_and: case Op::logical_or:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::boolean || units[i.b] != Unit::boolean)
                return {ProgramCode::unit_mismatch, pc};
            output = Unit::boolean;
            break;
        case Op::logical_not: case Op::assert_true:
            if (!need(i.a)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::boolean) return {ProgramCode::unit_mismatch, pc};
            output = Unit::boolean;
            break;
        case Op::absolute: case Op::negate:
            if (!need(i.a)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] == Unit::boolean) return {ProgramCode::unit_mismatch, pc};
            output = units[i.a];
            break;
        case Op::floor_step: case Op::ceil_step:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != units[i.b] || units[i.a] == Unit::boolean)
                return {ProgramCode::unit_mismatch, pc};
            output = units[i.a];
            break;
        case Op::clamp:
            if (!need(i.a) || !need(i.b) || !need(i.c)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != units[i.b] || units[i.a] != units[i.c] || units[i.a] == Unit::boolean)
                return {ProgramCode::unit_mismatch, pc};
            output = units[i.a];
            break;
        case Op::notional_buy: case Op::notional_sell:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::quantity || units[i.b] != Unit::price)
                return {ProgramCode::unit_mismatch, pc};
            output = Unit::money;
            break;
        case Op::fee_reserve:
            if (!need(i.a) || !need(i.b)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::money || units[i.b] != Unit::rate)
                return {ProgramCode::unit_mismatch, pc};
            output = Unit::money;
            break;
        case Op::select:
            if (!need(i.a) || !need(i.b) || !need(i.c)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::boolean || units[i.b] != units[i.c])
                return {ProgramCode::unit_mismatch, pc};
            output = units[i.b];
            break;
        case Op::candidate:
            if (!need(i.a)) return {ProgramCode::uninitialized, pc};
            if (units[i.a] != Unit::scalar && units[i.a] != Unit::rate)
                return {ProgramCode::unit_mismatch, pc};
            if (i.immediate < 1 || i.immediate > static_cast<int>(Candidate::escalate))
                return {ProgramCode::malformed, pc};
            terminal = true; output = units[i.a];
            break;
        case Op::abstain:
            terminal = true; output = requested;
            break;
        default: return {ProgramCode::malformed, pc};
        }
        if (output == Unit::none || output != requested) return {ProgramCode::unit_mismatch, pc};
        units[i.destination] = output;
    }
    return {terminal ? ProgramCode::okay : ProgramCode::no_candidate, p.count - 1};
}

ProgramOutput run_program(const Program& p, const Frame& f) noexcept {
    ProgramOutput output{program_version, 0, 0, 0, f.sequence, 0, 0, 0};
    const auto finish = [&](ProgramCode code, std::uint32_t pc = 0) {
        output.code = static_cast<unsigned>(code); output.failed_instruction = pc;
        output.candidate = 0; output.score = 0;
        return output;
    };
    const auto checked = validate_program(p);
    if (checked.code != ProgramCode::okay) return finish(checked.code, checked.instruction);
    if (f.version != program_version || f.valid != 1 || !f.sequence || f.sequence != f.expected_sequence)
        return finish(ProgramCode::state_invalid);
    if (f.now_ns >= f.deadline_ns) return finish(ProgramCode::expired);
    if (f.observed_ns > f.now_ns) return finish(ProgramCode::future_state);
    if (!f.max_age_ns || f.now_ns - f.observed_ns > f.max_age_ns) return finish(ProgramCode::stale);
    output.valid_until_ns = std::min(f.deadline_ns,
        f.observed_ns > UINT64_MAX - f.max_age_ns ? UINT64_MAX : f.observed_ns + f.max_age_ns);
    std::array<std::int64_t, register_count> r{};
    for (std::uint32_t pc = 0; pc < p.count; ++pc) {
        const auto& i = p.instructions[pc];
        const auto op = static_cast<Op>(i.opcode);
        __int128 value = 0;
        switch (op) {
        case Op::constant: value = i.immediate; break;
        case Op::load:
            value = f.values[i.immediate];
            if (static_cast<Unit>(i.unit) == Unit::boolean && value != 0 && value != 1)
                return finish(ProgramCode::malformed, pc);
            break;
        case Op::add: value = static_cast<__int128>(r[i.a]) + r[i.b]; break;
        case Op::subtract: value = static_cast<__int128>(r[i.a]) - r[i.b]; break;
        case Op::minimum: value = std::min(r[i.a], r[i.b]); break;
        case Op::maximum: value = std::max(r[i.a], r[i.b]); break;
        case Op::multiply_scaled:
            value = static_cast<__int128>(r[i.a]) * r[i.b] / amount_scale;
            break;
        case Op::divide_scaled:
            if (!r[i.b]) return finish(ProgramCode::divide_zero, pc);
            value = static_cast<__int128>(r[i.a]) * amount_scale / r[i.b];
            break;
        case Op::less: value = r[i.a] < r[i.b]; break;
        case Op::less_equal: value = r[i.a] <= r[i.b]; break;
        case Op::equal: value = r[i.a] == r[i.b]; break;
        case Op::logical_and: value = r[i.a] && r[i.b]; break;
        case Op::logical_or: value = r[i.a] || r[i.b]; break;
        case Op::logical_not: value = !r[i.a]; break;
        case Op::absolute:
            value = r[i.a] < 0 ? -static_cast<__int128>(r[i.a]) : r[i.a];
            break;
        case Op::negate: value = -static_cast<__int128>(r[i.a]); break;
        case Op::clamp:
            if (r[i.b] > r[i.c]) return finish(ProgramCode::malformed, pc);
            value = std::max(r[i.b], std::min(r[i.a], r[i.c]));
            break;
        case Op::floor_step: case Op::ceil_step: {
            if (r[i.a] < 0 || r[i.b] <= 0) return finish(ProgramCode::malformed, pc);
            auto lots = static_cast<__int128>(r[i.a]) / r[i.b];
            if (op == Op::ceil_step && r[i.a] % r[i.b]) ++lots;
            value = lots * r[i.b];
            break;
        }
        case Op::notional_buy: case Op::notional_sell: {
            if (r[i.a] < 0 || r[i.b] <= 0) return finish(ProgramCode::malformed, pc);
            auto product = static_cast<__int128>(r[i.a]) * r[i.b];
            value = (product + (op == Op::notional_buy ? amount_scale - 1 : 0)) / amount_scale;
            break;
        }
        case Op::fee_reserve:
            if (r[i.a] < 0 || r[i.b] < 0 || r[i.b] > amount_scale)
                return finish(ProgramCode::malformed, pc);
            value = (static_cast<__int128>(r[i.a]) * r[i.b] + amount_scale - 1) / amount_scale;
            break;
        case Op::select: value = r[i.a] ? r[i.b] : r[i.c]; break;
        case Op::assert_true:
            if (!r[i.a]) return finish(ProgramCode::assertion_failed, pc);
            value = 1; break;
        case Op::candidate:
            output.candidate = static_cast<unsigned>(i.immediate);
            output.score = r[i.a]; output.failed_instruction = pc;
            return output;
        case Op::abstain: return finish(ProgramCode::explicit_abstention, pc);
        default: return finish(ProgramCode::malformed, pc);
        }
        if (value < INT64_MIN || value > INT64_MAX) return finish(ProgramCode::overflow, pc);
        r[i.destination] = static_cast<std::int64_t>(value);
        if (static_cast<Unit>(i.unit) == Unit::boolean && r[i.destination] != 0 && r[i.destination] != 1)
            return finish(ProgramCode::malformed, pc);
    }
    return finish(ProgramCode::no_candidate);
}
} // namespace machine

extern "C" std::size_t machine_program_size() noexcept { return sizeof(machine::Program); }
extern "C" std::size_t machine_frame_size() noexcept { return sizeof(machine::Frame); }
extern "C" std::size_t machine_program_output_size() noexcept { return sizeof(machine::ProgramOutput); }
extern "C" int machine_program_validate(const machine::Program* p, std::uint32_t* code, std::uint32_t* pc) noexcept {
    if (!p || !code || !pc) return -1;
    auto result = machine::validate_program(*p);
    *code = static_cast<unsigned>(result.code); *pc = result.instruction;
    return 0;
}
extern "C" int machine_program_run(const machine::Program* p, const machine::Frame* f, machine::ProgramOutput* o) noexcept {
    if (!p || !f || !o) return -1;
    *o = machine::run_program(*p, *f); return 0;
}
