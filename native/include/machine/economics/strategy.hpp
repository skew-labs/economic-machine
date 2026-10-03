#pragma once

#include "machine/economics/state.hpp"
#include "machine/economics/decision.hpp"

namespace machine::economics {

enum class Comparator : std::uint32_t { less, less_equal, equal, greater_equal, greater, not_equal };

enum class StrategyOpcode : std::uint32_t {
    observe = 1,
    constant,
    add,
    subtract,
    multiply,
    divide,
    minimum,
    maximum,
    absolute,
    negate,
    clamp,
    compare,
    logical_and,
    logical_or,
    logical_not,
    select,
    assert_true,
    assert_nonnegative,
    assert_bounded,
    branch,
    hold,
    reduce,
    hedge,
    repay,
    borrow,
    allocate,
    swap,
    cancel,
    settle,
    escalate,
    abort,
    price,
    quote,
    score,
    fee,
    floor_step,
    ceil_step,
    notional_buy,
    notional_sell,
};

struct RegisterType {
    FactUnit unit{};
    std::uint64_t asset{};
    std::uint64_t quote_asset{};
};

[[nodiscard]] inline bool same_type(const RegisterType& a, const RegisterType& b) noexcept {
    return a.unit == b.unit && a.asset == b.asset && a.quote_asset == b.quote_asset;
}

[[nodiscard]] inline bool valid_type(const RegisterType& type) noexcept {
    const auto unit = static_cast<std::uint32_t>(type.unit);
    return unit && unit <= static_cast<std::uint32_t>(FactUnit::signed_quantity) &&
        (asset_unit(type.unit) ? type.asset != 0 : type.asset == 0) &&
        (type.unit == FactUnit::price ? (type.quote_asset && type.asset != type.quote_asset) : type.quote_asset == 0);
}

struct StrategyInstruction {
    StrategyOpcode opcode{};
    std::uint32_t destination{};
    std::uint32_t a{};
    std::uint32_t b{};
    std::uint32_t c{};
    std::int64_t immediate{};
    RegisterType type{};
    std::uint32_t true_target{};
    std::uint32_t false_target{};
};

struct StrategyProgram {
    std::uint64_t id{};
    std::uint64_t version{};
    std::uint64_t policy_version{};
    std::array<RegisterType, 32> dependency_types{};
    std::uint32_t dependency_count{};
    std::array<StrategyInstruction, 128> instructions{};
    std::uint32_t count{};
    std::uint64_t ttl_ns{};
};

struct StrategyValidation {
    Error error{Error::unknown};
    std::uint32_t instruction{};
    std::uint32_t reachable{};
};

struct StrategyFrame {
    DependencyFrame dependencies{};
    std::uint64_t program_version{};
    std::uint64_t policy_version{};
    std::uint64_t now_ns{};
    bool policy_active{};
};

struct StrategyResult {
    Error error{Error::unknown};
    EconomicAction action{EconomicAction::none};
    std::uint32_t instruction{};
    std::uint32_t executed{};
    std::int64_t amount{};
    std::int64_t score{};
    std::uint64_t program{};
    std::uint64_t program_version{};
    std::uint64_t state_generation{};
    std::uint64_t valid_until_ns{};
    std::uint64_t diagnostic_fingerprint{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline bool terminal_opcode(StrategyOpcode opcode) noexcept {
    switch (opcode) {
    case StrategyOpcode::hold:
    case StrategyOpcode::reduce:
    case StrategyOpcode::hedge:
    case StrategyOpcode::repay:
    case StrategyOpcode::borrow:
    case StrategyOpcode::allocate:
    case StrategyOpcode::swap:
    case StrategyOpcode::cancel:
    case StrategyOpcode::settle:
    case StrategyOpcode::escalate:
    case StrategyOpcode::abort:
        return true;
    default:
        return false;
    }
}

[[nodiscard]] inline EconomicAction terminal_action(StrategyOpcode opcode) noexcept {
    switch (opcode) {
    case StrategyOpcode::hold: return EconomicAction::hold;
    case StrategyOpcode::reduce: return EconomicAction::reduce;
    case StrategyOpcode::hedge: return EconomicAction::hedge;
    case StrategyOpcode::repay: return EconomicAction::repay;
    case StrategyOpcode::borrow: return EconomicAction::borrow;
    case StrategyOpcode::allocate: return EconomicAction::allocate;
    case StrategyOpcode::swap: return EconomicAction::swap;
    case StrategyOpcode::cancel: return EconomicAction::cancel;
    case StrategyOpcode::escalate: return EconomicAction::escalate;
    case StrategyOpcode::abort: return EconomicAction::abort;
    // SETTLE is a request for the settlement verifier, not a receipt that
    // proves an external transaction succeeded. It intentionally escalates.
    case StrategyOpcode::settle: return EconomicAction::escalate;
    default: return EconomicAction::none;
    }
}

[[nodiscard]] inline Result<RegisterType> product_type(
    const RegisterType& a,
    const RegisterType& b,
    bool division
) noexcept {
    if (division && same_type(a, b) && a.unit != FactUnit::boolean) {
        return success(RegisterType{FactUnit::rate, 0});
    }
    if (b.unit == FactUnit::rate && a.unit != FactUnit::boolean && a.unit != FactUnit::count &&
        a.unit != FactUnit::duration_ns) {
        return success(a);
    }
    if (!division && a.unit == FactUnit::rate && b.unit != FactUnit::boolean &&
        b.unit != FactUnit::count && b.unit != FactUnit::duration_ns) {
        return success(b);
    }
    if (!division && a.asset == b.asset && a.asset &&
        ((a.unit == FactUnit::quantity && b.unit == FactUnit::price) ||
         (a.unit == FactUnit::price && b.unit == FactUnit::quantity))) {
        return success(RegisterType{FactUnit::money, a.unit == FactUnit::price ? a.quote_asset : b.quote_asset});
    }
    return failure<RegisterType>(Error::domain);
}

// Validate every reachable path independently, including definite assignment
// at joins. A register assigned only on one branch is unavailable at the join.
// Branches go forward, so no loop bound can be evaded by a malformed program.
[[nodiscard]] inline StrategyValidation validate_strategy(const StrategyProgram& program) noexcept {
    if (!program.id || !program.version || !program.policy_version || !program.ttl_ns ||
        !program.count || program.count > program.instructions.size() ||
        program.dependency_count > program.dependency_types.size()) {
        return {Error::invalid, 0, 0};
    }
    for (std::size_t i = 0; i < program.dependency_count; ++i) {
        if (!valid_type(program.dependency_types[i])) {
            return {Error::domain, 0, 0};
        }
    }
    struct TypeFrame {
        std::array<RegisterType, 64> types{};
        std::uint64_t initialized{};
        bool reached{};
    };
    std::array<TypeFrame, 128> frames{};
    frames[0].reached = true;
    StrategyValidation result{Error::okay, 0, 0};
    const auto propagate = [&](std::uint32_t target, const TypeFrame& from) -> Error {
        if (target >= program.count) {
            return Error::invalid;
        }
        auto& destination = frames[target];
        if (!destination.reached) {
            destination = from;
            destination.reached = true;
            return Error::okay;
        }
        const auto common = destination.initialized & from.initialized;
        for (std::size_t i = 0; i < destination.types.size(); ++i) {
            if ((common & (1ULL << i)) && !same_type(destination.types[i], from.types[i])) {
                return Error::domain;
            }
        }
        destination.initialized = common;
        return Error::okay;
    };
    for (std::uint32_t pc = 0; pc < program.count; ++pc) {
        const auto& instruction = program.instructions[pc];
        if (!frames[pc].reached) {
            // No unreachable instruction payload can hide an unsupported
            // operation in a versioned economic program.
            return {Error::invalid, pc, result.reachable};
        }
        ++result.reachable;
        auto frame = frames[pc];
        if (instruction.destination >= 64 || instruction.a >= 64 || instruction.b >= 64 ||
            instruction.c >= 64 || !valid_type(instruction.type)) {
            return {Error::invalid, pc, result.reachable};
        }
        const auto initialized = [&](std::uint32_t reg) {
            return (frame.initialized & (1ULL << reg)) != 0;
        };
        const auto need_one = initialized(instruction.a);
        const auto need_two = need_one && initialized(instruction.b);
        const auto need_three = need_two && initialized(instruction.c);
        RegisterType output{};
        switch (instruction.opcode) {
        case StrategyOpcode::observe:
            if (instruction.immediate < 0 || instruction.immediate >= program.dependency_count) {
                return {Error::missing, pc, result.reachable};
            }
            output = program.dependency_types[instruction.immediate];
            break;
        case StrategyOpcode::constant:
            output = instruction.type;
            if ((output.unit == FactUnit::boolean && instruction.immediate != 0 && instruction.immediate != 1) ||
                (instruction.immediate < 0 && output.unit != FactUnit::signed_money &&
                 output.unit != FactUnit::signed_quantity && output.unit != FactUnit::rate)) {
                return {Error::invalid, pc, result.reachable};
            }
            break;
        case StrategyOpcode::add:
        case StrategyOpcode::subtract:
        case StrategyOpcode::minimum:
        case StrategyOpcode::maximum:
        case StrategyOpcode::floor_step:
        case StrategyOpcode::ceil_step:
            if (!need_two || !same_type(frame.types[instruction.a], frame.types[instruction.b]) ||
                frame.types[instruction.a].unit == FactUnit::boolean) {
                return {Error::domain, pc, result.reachable};
            }
            output = frame.types[instruction.a];
            break;
        case StrategyOpcode::multiply:
        case StrategyOpcode::divide:
        case StrategyOpcode::notional_buy:
        case StrategyOpcode::notional_sell: {
            if (!need_two) {
                return {Error::missing, pc, result.reachable};
            }
            const auto type = product_type(frame.types[instruction.a], frame.types[instruction.b],
                instruction.opcode == StrategyOpcode::divide);
            if (!type) {
                return {type.error, pc, result.reachable};
            }
            output = type.value;
            if ((instruction.opcode == StrategyOpcode::notional_buy || instruction.opcode == StrategyOpcode::notional_sell) &&
                output.unit != FactUnit::money) {
                return {Error::domain, pc, result.reachable};
            }
            break;
        }
        case StrategyOpcode::absolute:
        case StrategyOpcode::negate:
        case StrategyOpcode::price:
        case StrategyOpcode::quote:
        case StrategyOpcode::score:
            if (!need_one || frame.types[instruction.a].unit == FactUnit::boolean) {
                return {Error::domain, pc, result.reachable};
            }
            output = frame.types[instruction.a];
            if ((instruction.opcode == StrategyOpcode::price || instruction.opcode == StrategyOpcode::quote) &&
                output.unit != FactUnit::price) {
                return {Error::domain, pc, result.reachable};
            }
            if (instruction.opcode == StrategyOpcode::score && output.unit != FactUnit::rate) {
                return {Error::domain, pc, result.reachable};
            }
            break;
        case StrategyOpcode::fee:
            if (!need_two || frame.types[instruction.a].unit != FactUnit::money ||
                frame.types[instruction.b].unit != FactUnit::rate) {
                return {Error::domain, pc, result.reachable};
            }
            output = frame.types[instruction.a];
            break;
        case StrategyOpcode::clamp:
        case StrategyOpcode::assert_bounded:
            if (!need_three || !same_type(frame.types[instruction.a], frame.types[instruction.b]) ||
                !same_type(frame.types[instruction.a], frame.types[instruction.c])) {
                return {Error::domain, pc, result.reachable};
            }
            output = instruction.opcode == StrategyOpcode::clamp ? frame.types[instruction.a]
                : RegisterType{FactUnit::boolean, 0};
            break;
        case StrategyOpcode::compare:
            if (!need_two || !same_type(frame.types[instruction.a], frame.types[instruction.b]) ||
                instruction.immediate < 0 || instruction.immediate > static_cast<std::int64_t>(Comparator::not_equal)) {
                return {Error::domain, pc, result.reachable};
            }
            output = {FactUnit::boolean, 0};
            break;
        case StrategyOpcode::logical_and:
        case StrategyOpcode::logical_or:
            if (!need_two || frame.types[instruction.a].unit != FactUnit::boolean ||
                frame.types[instruction.b].unit != FactUnit::boolean) {
                return {Error::domain, pc, result.reachable};
            }
            output = {FactUnit::boolean, 0};
            break;
        case StrategyOpcode::logical_not:
        case StrategyOpcode::assert_true:
        case StrategyOpcode::branch:
            if (!need_one || frame.types[instruction.a].unit != FactUnit::boolean) {
                return {Error::domain, pc, result.reachable};
            }
            output = {FactUnit::boolean, 0};
            break;
        case StrategyOpcode::assert_nonnegative:
            if (!need_one || frame.types[instruction.a].unit == FactUnit::boolean) {
                return {Error::domain, pc, result.reachable};
            }
            output = {FactUnit::boolean, 0};
            break;
        case StrategyOpcode::select:
            if (!need_three || frame.types[instruction.a].unit != FactUnit::boolean ||
                !same_type(frame.types[instruction.b], frame.types[instruction.c])) {
                return {Error::domain, pc, result.reachable};
            }
            output = frame.types[instruction.b];
            break;
        default:
            if (!terminal_opcode(instruction.opcode)) {
                return {Error::unsupported, pc, result.reachable};
            }
            if (instruction.opcode != StrategyOpcode::hold && instruction.opcode != StrategyOpcode::abort &&
                instruction.opcode != StrategyOpcode::escalate && instruction.opcode != StrategyOpcode::settle) {
                if (!need_one || (frame.types[instruction.a].unit != FactUnit::quantity &&
                    frame.types[instruction.a].unit != FactUnit::money)) {
                    return {Error::domain, pc, result.reachable};
                }
            }
            continue;
        }
        if (!same_type(output, instruction.type)) {
            return {Error::domain, pc, result.reachable};
        }
        frame.types[instruction.destination] = output;
        frame.initialized |= 1ULL << instruction.destination;
        if (instruction.opcode == StrategyOpcode::branch) {
            if (instruction.true_target <= pc || instruction.false_target <= pc ||
                instruction.true_target == instruction.false_target) {
                return {Error::invalid, pc, result.reachable};
            }
            const auto first = propagate(instruction.true_target, frame);
            const auto second = propagate(instruction.false_target, frame);
            if (first != Error::okay || second != Error::okay) {
                return {first != Error::okay ? first : second, pc, result.reachable};
            }
        } else {
            const auto error = propagate(pc + 1, frame);
            if (error != Error::okay) {
                return {error, pc, result.reachable};
            }
        }
    }
    return result;
}

[[nodiscard]] inline bool compare_values(std::int64_t a, std::int64_t b, Comparator comparator) noexcept {
    switch (comparator) {
    case Comparator::less: return a < b;
    case Comparator::less_equal: return a <= b;
    case Comparator::equal: return a == b;
    case Comparator::greater_equal: return a >= b;
    case Comparator::greater: return a > b;
    case Comparator::not_equal: return a != b;
    }
    return false;
}

[[nodiscard]] inline StrategyResult run_strategy(
    const StrategyProgram& program,
    const StrategyFrame& frame
) noexcept {
    StrategyResult result{};
    result.program = program.id;
    result.program_version = program.version;
    result.state_generation = frame.dependencies.state_generation;
    const auto validation = validate_strategy(program);
    if (validation.error != Error::okay) {
        result.error = validation.error;
        result.instruction = validation.instruction;
        return result;
    }
    if (!frame.policy_active || frame.policy_version != program.policy_version ||
        frame.program_version != program.version || frame.dependencies.count != program.dependency_count ||
        !frame.dependencies.state_generation) {
        result.error = Error::unauthorized;
        return result;
    }
    if (frame.now_ns >= frame.dependencies.valid_until_ns) {
        result.error = Error::expired;
        return result;
    }
    const auto ttl = add_time(frame.now_ns, program.ttl_ns);
    if (!ttl) {
        result.error = ttl.error;
        return result;
    }
    result.valid_until_ns = std::min(ttl.value, frame.dependencies.valid_until_ns);
    for (std::size_t i = 0; i < program.dependency_count; ++i) {
        const auto& fact = frame.dependencies.facts[i];
        if (validate_fact(fact) != Error::okay || fact_freshness(fact, frame.now_ns) != Error::okay ||
            !same_type(program.dependency_types[i], {fact.unit, fact.asset, fact.quote_asset})) {
            result.error = Error::domain;
            return result;
        }
    }
    std::array<std::int64_t, 64> registers{};
    std::uint32_t pc = 0;
    Fingerprint trace;
    trace.append(program.id);
    trace.append(program.version);
    trace.append(frame.dependencies.state_generation);
    while (pc < program.count) {
        const auto& instruction = program.instructions[pc];
        ++result.executed;
        result.instruction = pc;
        if (terminal_opcode(instruction.opcode)) {
            result.action = terminal_action(instruction.opcode);
            if (result.action != EconomicAction::hold && result.action != EconomicAction::abort &&
                result.action != EconomicAction::escalate) {
                result.amount = registers[instruction.a];
                if (result.amount <= 0) {
                    result.action = EconomicAction::none;
                    result.error = Error::invalid;
                    return result;
                }
            }
            result.error = Error::okay;
            trace.append(static_cast<std::uint32_t>(result.action));
            trace.append(static_cast<std::uint64_t>(result.amount));
            result.diagnostic_fingerprint = trace.value();
            return result;
        }
        const auto a = registers[instruction.a];
        const auto b = registers[instruction.b];
        const auto c = registers[instruction.c];
        Result<std::int64_t> value{Error::okay, 0};
        switch (instruction.opcode) {
        case StrategyOpcode::observe:
            value.value = frame.dependencies.facts[instruction.immediate].value;
            break;
        case StrategyOpcode::constant:
            value.value = instruction.immediate;
            break;
        case StrategyOpcode::add: value = add(a, b); break;
        case StrategyOpcode::subtract: value = subtract(a, b); break;
        case StrategyOpcode::multiply: value = multiply(a, b); break;
        case StrategyOpcode::divide: value = ratio(a, b); break;
        case StrategyOpcode::minimum: value.value = std::min(a, b); break;
        case StrategyOpcode::maximum: value.value = std::max(a, b); break;
        case StrategyOpcode::absolute: value = absolute(a); break;
        case StrategyOpcode::negate: value = narrow(-static_cast<Wide>(a)); break;
        case StrategyOpcode::compare:
            value.value = compare_values(a, b, static_cast<Comparator>(instruction.immediate));
            break;
        case StrategyOpcode::logical_and: value.value = a && b; break;
        case StrategyOpcode::logical_or: value.value = a || b; break;
        case StrategyOpcode::logical_not: value.value = !a; break;
        case StrategyOpcode::select: value.value = a ? b : c; break;
        case StrategyOpcode::clamp:
            if (b > c) {
                value.error = Error::invalid;
            } else {
                value.value = std::max(b, std::min(a, c));
            }
            break;
        case StrategyOpcode::assert_true:
            value.value = a;
            if (!a) value.error = Error::infeasible;
            break;
        case StrategyOpcode::assert_nonnegative:
            value.value = a >= 0;
            if (a < 0) value.error = Error::infeasible;
            break;
        case StrategyOpcode::assert_bounded:
            value.value = b <= a && a <= c && b <= c;
            if (!value.value) value.error = Error::infeasible;
            break;
        case StrategyOpcode::fee:
            if (a < 0 || b < 0 || b > scale) {
                value.error = Error::invalid;
            } else {
                value = multiply(a, b, Rounding::ceil);
            }
            break;
        case StrategyOpcode::floor_step: value = step_round(a, b, Rounding::floor); break;
        case StrategyOpcode::ceil_step: value = step_round(a, b, Rounding::ceil); break;
        case StrategyOpcode::notional_buy:
        case StrategyOpcode::notional_sell:
            if (a < 0 || b < 0) {
                value.error = Error::invalid;
            } else {
                value = multiply(a, b, instruction.opcode == StrategyOpcode::notional_buy
                    ? Rounding::ceil : Rounding::floor);
            }
            break;
        case StrategyOpcode::price:
        case StrategyOpcode::quote:
            value.value = a;
            if (a <= 0) value.error = Error::invalid;
            break;
        case StrategyOpcode::score:
            value.value = a;
            if (a < 0 || a > scale) value.error = Error::invalid;
            result.score = a;
            break;
        case StrategyOpcode::branch:
            registers[instruction.destination] = a;
            trace.append(pc);
            trace.append(static_cast<std::uint32_t>(instruction.opcode));
            trace.append(static_cast<std::uint64_t>(a));
            pc = a ? instruction.true_target : instruction.false_target;
            continue;
        default:
            value.error = Error::unsupported;
            break;
        }
        if (!value) {
            result.error = value.error;
            return result;
        }
        // Arithmetic cannot turn an unsigned amount or boolean register into
        // a value outside its declared type, even when the wide math succeeds.
        if ((instruction.type.unit == FactUnit::boolean && value.value != 0 && value.value != 1) ||
            (value.value < 0 && instruction.type.unit != FactUnit::signed_money &&
             instruction.type.unit != FactUnit::signed_quantity && instruction.type.unit != FactUnit::rate)) {
            result.error = Error::domain;
            return result;
        }
        registers[instruction.destination] = value.value;
        trace.append(pc);
        trace.append(static_cast<std::uint32_t>(instruction.opcode));
        trace.append(static_cast<std::uint64_t>(value.value));
        ++pc;
    }
    result.error = Error::incomplete;
    return result;
}

} // namespace machine::economics
