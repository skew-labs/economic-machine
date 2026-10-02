#include "machine/program.hpp"

#include <cstdlib>
#include <iostream>

using namespace machine;
static unsigned checks{};
static void check(bool okay, const char* message) {
    ++checks;
    if (!okay) { std::cerr << message << '\n'; std::exit(1); }
}
static Instruction ins(Op op, unsigned dst, Unit unit, std::int64_t value = 0,
                       unsigned a = 0, unsigned b = 0, unsigned c = 0) {
    return {static_cast<unsigned>(op), dst, a, b, c, static_cast<unsigned>(unit), value};
}
static Program baseline() {
    Program p{}; p.version = 1; p.count = 8;
    p.field_units[0] = static_cast<unsigned>(Unit::rate);
    p.instructions[0] = ins(Op::load, 0, Unit::rate, 0);
    p.instructions[1] = ins(Op::constant, 1, Unit::rate, 800000);
    p.instructions[2] = ins(Op::less, 2, Unit::boolean, 0, 0, 1);
    p.instructions[3] = ins(Op::assert_true, 3, Unit::boolean, 0, 2);
    p.instructions[4] = ins(Op::constant, 4, Unit::scalar, 900000);
    p.instructions[5] = ins(Op::constant, 5, Unit::scalar, 500000);
    p.instructions[6] = ins(Op::select, 6, Unit::scalar, 0, 2, 4, 5);
    p.instructions[7] = ins(Op::candidate, 7, Unit::scalar, static_cast<int>(Candidate::reduce), 6);
    return p;
}
static Frame frame() {
    Frame f{}; f.version = 1; f.valid = 1; f.sequence = f.expected_sequence = 7;
    f.observed_ns = 900; f.now_ns = 950; f.max_age_ns = 100; f.deadline_ns = 1100;
    f.values[0] = 700000;
    return f;
}
int main() {
    auto p = baseline(); auto f = frame();
    check(validate_program(p).code == ProgramCode::okay, "typed program");
    auto result = run_program(p, f);
    check(result.code == 0 && result.candidate == 2 && result.score == 900000, "numeric decision");
    check(result.execution_authority == 0 && result.valid_until_ns == 1000, "no authority and bounded freshness");
    f.values[0] = 800000;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::assertion_failed), "assertion exact boundary");
    f = frame(); f.valid = 0;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::state_invalid), "invalid state");
    f = frame(); f.expected_sequence = 8;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::state_invalid), "state identity");
    f = frame(); f.now_ns = 1100;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::expired), "expiry exact boundary");
    f = frame(); f.now_ns = 1001;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::stale), "stale state");
    f = frame(); f.now_ns = 899;
    check(run_program(p, f).code == static_cast<unsigned>(ProgramCode::future_state), "future state");
    p = baseline(); p.instructions[0].immediate = 32;
    check(validate_program(p).code == ProgramCode::malformed, "load outside state");
    p = baseline(); p.instructions[0].destination = 32;
    check(validate_program(p).code == ProgramCode::malformed, "register bound");
    p = baseline(); p.instructions[2].a = 31;
    check(validate_program(p).code == ProgramCode::uninitialized, "uninitialized register");
    p = baseline(); p.instructions[1].unit = static_cast<unsigned>(Unit::money);
    check(validate_program(p).code == ProgramCode::unit_mismatch, "cannot compare money with rate");
    p = baseline(); p.count = 7;
    check(validate_program(p).code == ProgramCode::no_candidate, "must terminate");
    p = baseline(); p.count = 9;
    check(validate_program(p).code == ProgramCode::malformed, "nothing after terminal");
    p = baseline(); p.instructions[7] = ins(Op::abstain, 7, Unit::scalar);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::explicit_abstention), "explicit abstention");
    p = {}; p.version = 1; p.count = 4;
    p.instructions[0] = ins(Op::constant, 0, Unit::quantity, 2000000);
    p.instructions[1] = ins(Op::constant, 1, Unit::price, 10000000);
    p.instructions[2] = ins(Op::multiply_scaled, 2, Unit::money, 0, 0, 1);
    p.instructions[3] = ins(Op::abstain, 3, Unit::scalar);
    check(validate_program(p).code == ProgramCode::okay, "quantity times price is money");
    p.instructions[2] = ins(Op::add, 2, Unit::money, 0, 0, 1);
    check(validate_program(p).code == ProgramCode::unit_mismatch, "quantity plus price forbidden");
    p.instructions[0] = ins(Op::constant, 0, Unit::scalar, INT64_MAX);
    p.instructions[1] = ins(Op::constant, 1, Unit::scalar, 1);
    p.instructions[2] = ins(Op::add, 2, Unit::scalar, 0, 0, 1);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "checked add overflow");
    p.instructions[1].immediate = 0;
    p.instructions[2] = ins(Op::divide_scaled, 2, Unit::scalar, 0, 0, 1);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::divide_zero), "zero divisor");
    p.instructions[1].immediate = 2000000;
    p.instructions[2] = ins(Op::multiply_scaled, 2, Unit::scalar, 0, 0, 1);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "scaled multiply overflow");
    p.instructions[0].immediate = INT64_MIN;
    p.instructions[1].immediate = -1;
    p.instructions[2] = ins(Op::divide_scaled, 2, Unit::scalar, 0, 0, 1);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "minimum integer division overflow");
    p = baseline(); f = frame(); f.observed_ns = UINT64_MAX - 50; f.now_ns = UINT64_MAX - 25;
    f.deadline_ns = UINT64_MAX;
    check(run_program(p, f).valid_until_ns == UINT64_MAX, "freshness addition saturates");
    check(machine_program_run(nullptr, &f, &result) == -1, "null C program");
    check(machine_program_size() == sizeof(Program) && machine_frame_size() == sizeof(Frame), "C ABI sizes");
    std::uint32_t code{}, pc{};
    check(machine_program_validate(&p, &code, &pc) == 0 && code == 0, "C validation");

    const auto arithmetic = [](Op op, Unit left, Unit right, Unit out,
                                std::int64_t a, std::int64_t b, std::int64_t expected) {
        Program math{}; math.version = 1; math.count = 6;
        math.instructions[0] = ins(Op::constant, 0, left, a);
        math.instructions[1] = ins(Op::constant, 1, right, b);
        math.instructions[2] = ins(op, 2, out, 0, 0, 1);
        math.instructions[3] = ins(Op::constant, 3, out, expected);
        math.instructions[4] = ins(Op::equal, 4, Unit::boolean, 0, 2, 3);
        math.instructions[5] = ins(Op::assert_true, 5, Unit::boolean, 0, 4);
        // Append a terminal only after the expected value assertion.
        math.instructions[6] = ins(Op::abstain, 6, Unit::scalar); math.count = 7;
        return run_program(math, frame()).code == static_cast<unsigned>(ProgramCode::explicit_abstention);
    };
    check(arithmetic(Op::notional_buy, Unit::quantity, Unit::price, Unit::money, 1, 1, 1), "buy cost rounds up");
    check(arithmetic(Op::notional_sell, Unit::quantity, Unit::price, Unit::money, 1, 1, 0), "sale receipt rounds down");
    check(arithmetic(Op::notional_buy, Unit::quantity, Unit::price, Unit::money, 1000000, 10000000, 10000000), "exact notional");
    check(arithmetic(Op::fee_reserve, Unit::money, Unit::rate, Unit::money, 1, 2000, 1), "fractional fee reserves a micro-unit");
    check(arithmetic(Op::fee_reserve, Unit::money, Unit::rate, Unit::money, 10000000, 2000, 20000), "fee uses rates rather than bps");
    check(arithmetic(Op::floor_step, Unit::quantity, Unit::quantity, Unit::quantity, 12345, 1000, 12000), "lot floor");
    check(arithmetic(Op::ceil_step, Unit::quantity, Unit::quantity, Unit::quantity, 12345, 1000, 13000), "lot ceiling");
    check(arithmetic(Op::ceil_step, Unit::quantity, Unit::quantity, Unit::quantity, 12000, 1000, 12000), "exact lot unchanged");
    check(arithmetic(Op::absolute, Unit::money, Unit::money, Unit::money, -123, 0, 123), "absolute liability");
    check(arithmetic(Op::negate, Unit::money, Unit::money, Unit::money, 123, 0, -123), "signed debit");
    p = {}; p.version = 1; p.count = 3;
    p.instructions[0] = ins(Op::constant, 0, Unit::money, INT64_MIN);
    p.instructions[1] = ins(Op::absolute, 1, Unit::money, 0, 0);
    p.instructions[2] = ins(Op::abstain, 2, Unit::scalar);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "absolute min integer");
    p.instructions[1] = ins(Op::negate, 1, Unit::money, 0, 0);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "negate min integer");
    p = {}; p.version = 1; p.count = 4;
    p.instructions[0] = ins(Op::constant, 0, Unit::quantity, INT64_MAX);
    p.instructions[1] = ins(Op::constant, 1, Unit::quantity, 2);
    p.instructions[2] = ins(Op::ceil_step, 2, Unit::quantity, 0, 0, 1);
    p.instructions[3] = ins(Op::abstain, 3, Unit::scalar);
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::overflow), "rounded lot overflow");
    p.instructions[1].immediate = 0;
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::malformed), "zero lot step");
    p.instructions[0].immediate = -1; p.instructions[1].immediate = 2;
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::malformed), "negative quantity rounding");
    p = {}; p.version = 1; p.count = 5;
    p.instructions[0] = ins(Op::constant, 0, Unit::rate, 1000000);
    p.instructions[1] = ins(Op::constant, 1, Unit::rate, 100000);
    p.instructions[2] = ins(Op::constant, 2, Unit::rate, 900000);
    p.instructions[3] = ins(Op::clamp, 3, Unit::rate, 0, 0, 1, 2);
    p.instructions[4] = ins(Op::candidate, 4, Unit::rate, 1, 3);
    check(run_program(p, frame()).score == 900000, "clamped score");
    p.instructions[1].immediate = 950000;
    check(run_program(p, frame()).code == static_cast<unsigned>(ProgramCode::malformed), "inverted clamp envelope");

    DepthBook<4> book;
    std::array<Level, 4> bids{{{9900000, 2000000}, {9800000, 3000000}, {}, {}}};
    std::array<Level, 4> asks{{{10000000, 1000000}, {10100000, 3000000}, {}, {}}};
    check(book.quote(Side::buy, 1000000, 100, 100).code == Code::unknown, "no quote without snapshot");
    check(book.snapshot(bids, asks, 1, 100) == Code::admit, "valid depth snapshot");
    auto quote = book.quote(Side::buy, 2000000, 150, 100);
    check(quote.code == Code::admit && quote.notional == 20100000 && quote.average_price == 10050000,
          "buy walks multiple levels");
    check(quote.worst_price == 10100000 && quote.sequence == 1, "quote state identity");
    quote = book.quote(Side::sell, 3000000, 150, 100);
    check(quote.notional == 29600000 && quote.worst_price == 9800000, "sell depth");
    check(book.quote(Side::buy, 5000000, 150, 100).code == Code::capacity, "never invent tail liquidity");
    check(book.quote(Side::buy, 1, 201, 100).code == Code::stale, "stale depth");
    check(book.quote(Side::buy, 1, 99, 100).code == Code::stale, "future depth");
    check(book.update(false, 10000000, 0, 2, 101) == Code::admit, "delete level");
    check(book.quote(Side::buy, 1000000, 150, 100).notional == 10100000, "deleted best ask");
    check(book.update(false, 10050000, 1000000, 3, 102) == Code::admit, "insert level");
    check(book.quote(Side::buy, 1000000, 150, 100).notional == 10050000, "sorted inserted ask");
    check(book.update(false, 10050000, 2000000, 3, 102) == Code::sequence && book.valid(), "duplicate does not corrupt");
    check(book.update(false, 10050000, 2000000, 5, 104) == Code::sequence && !book.valid(), "gap invalidates");
    check(book.update(false, 10050000, 2000000, 4, 103) == Code::unknown, "gap requires full snapshot");
    check(book.snapshot(bids, asks, 6, 105) == Code::admit, "snapshot recovery");
    check(book.update(true, 10000000, 1000000, 7, 106) == Code::invalid && !book.valid(), "crossed book invalidates");
    bids[1].price = bids[0].price;
    check(book.snapshot(bids, asks, 8, 107) == Code::invalid, "duplicate snapshot price");
    bids[1].price = 9800000;
    check(book.snapshot(bids, asks, 8, 107) == Code::admit, "recovery after malformed input");
    asks[0] = {INT64_MAX, INT64_MAX}; asks[1] = {};
    check(book.snapshot(bids, asks, 9, 108) == Code::admit, "large representable level");
    check(book.quote(Side::buy, INT64_MAX, 109, 100).code == Code::overflow, "quote accumulator bound");
    std::cout << "{\"checks\":" << checks << ",\"program\":\"PASS\",\"depth\":\"PASS\",\"transactions\":0}\n";
}
