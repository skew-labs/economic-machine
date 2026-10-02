#include "machine/kernel.hpp"

#include <cstdlib>
#include <iostream>
#include <thread>

using machine::Code;
static int checks = 0;
static void check(bool okay, const char* name) {
    ++checks;
    if (!okay) { std::cerr << "FAILED: " << name << '\n'; std::exit(1); }
}

machine::Input baseline() {
    return {1, 1, 7, 7, 1000, 950, 100, 1100, 1'000'000, 10'000'000, 10'000'000,
            1000, 1000, 1'000'000, 20'000'000, 30'000'000, 2'000'000,
            10'000'000, 20'000'000, 30'000'000, 15, 20, 1, 1};
}

int main() {
    auto input = baseline();
    const auto output = machine::evaluate(input);
    check(output.code == 0 && output.notional == 10'000'000, "valid order");
    check(output.required_quote == 10'020'000, "fee debit rounded conservatively");
    check(output.execution_authority == 0 && output.valid_until_ns == 1050, "candidate has no authority");
    const auto reject = [](auto mutate, Code expected, const char* name) {
        auto value = baseline(); mutate(value);
        check(machine::evaluate(value).code == static_cast<unsigned>(expected), name);
    };
    reject([](auto& i) { i.version = 2; }, Code::invalid, "ABI mismatch");
    reject([](auto& i) { i.side = 0; }, Code::invalid, "bad side");
    reject([](auto& i) { i.policy_active = 0; }, Code::disabled, "paused policy");
    reject([](auto& i) { i.oracle_valid = 0; }, Code::disabled, "unverified oracle");
    reject([](auto& i) { i.deadline_ns = i.now_ns; }, Code::expired, "TTL boundary");
    reject([](auto& i) { i.observed_ns = 1001; }, Code::stale, "future oracle");
    reject([](auto& i) { i.observed_ns = 899; }, Code::stale, "stale oracle");
    reject([](auto& i) { i.sequence++; }, Code::sequence, "state identity");
    reject([](auto& i) { i.quantity++; }, Code::increment, "lot step");
    reject([](auto& i) { i.price++; }, Code::increment, "price tick");
    reject([](auto& i) { i.min_notional = 11'000'000; }, Code::notional, "minimum notional");
    reject([](auto& i) { i.max_order = 9'000'000; }, Code::notional, "maximum order");
    reject([](auto& i) { i.available_quote = 10'019'999; }, Code::balance, "fee reserve");
    reject([](auto& i) { i.side = 2; i.available_base = 999'999; }, Code::balance, "sell availability");
    reject([](auto& i) { i.exposure_after = 20'000'001; }, Code::exposure, "exposure cap");
    reject([](auto& i) { i.price = 10'016'000; }, Code::slippage, "buy slippage");
    reject([](auto& i) { i.side = 2; i.price = 9'984'000; }, Code::slippage, "sell slippage");
    reject([](auto& i) { i.turnover_remaining = 9'999'999; }, Code::turnover, "shared turnover");
    reject([](auto& i) { i.quantity = INT64_MAX; i.price = INT64_MAX; i.quantity_step = i.price_tick = 1; }, Code::overflow, "multiply overflow");
    reject([](auto& i) { i.max_slippage_bps = 10001; }, Code::invalid, "slippage range");
    reject([](auto& i) { i.fee_reserve_bps = 10001; }, Code::invalid, "fee range");
    input = baseline(); input.price = 9'000'000;
    check(machine::evaluate(input).code == 0, "favorable execution accepted");
    input = baseline(); input.observed_ns = 900;
    check(machine::evaluate(input).code == 0, "oracle age exact boundary");
    input = baseline(); input.side = 2; input.price = 11'000'000;
    check(machine::evaluate(input).code == 0, "favorable sale accepted");
    machine::Output result{};
    check(machine_evaluate(nullptr, &result) == -1, "null input");
    check(machine_evaluate(&input, nullptr) == -1, "null output");
    check(machine_input_size() == sizeof(input) && machine_output_size() == sizeof(result), "C layout");
    check(machine_evaluate(&input, &result) == 0 && result.execution_authority == 0, "C candidate");

    machine::SpscRing<std::uint64_t, 8> small;
    std::uint64_t value = 0;
    check(!small.pop(value), "empty queue");
    for (unsigned i = 0; i < 8; ++i) check(small.push(i), "queue fill");
    check(!small.push(9), "full queue backpressure");
    for (unsigned i = 0; i < 8; ++i) { check(small.pop(value), "queue drain"); check(value == i, "queue ordering"); }
    for (unsigned i = 0; i < 1000; ++i) { check(small.push(i) && small.pop(value) && value == i, "queue wrap"); }
    machine::SpscRing<std::uint64_t, 1024> threaded;
    constexpr std::uint64_t events = 200000;
    std::atomic<bool> corrupt{false};
    std::thread consumer([&] {
        for (std::uint64_t expected = 1; expected <= events; ++expected) {
            std::uint64_t received;
            while (!threaded.pop(received)) std::this_thread::yield();
            if (received != expected) corrupt.store(true);
        }
    });
    for (std::uint64_t i = 1; i <= events; ++i) while (!threaded.push(i)) std::this_thread::yield();
    consumer.join();
    check(!corrupt.load() && threaded.size_approx() == 0, "cross-thread ordering");

    machine::StateBook<4> state;
    machine::StateDelta delta{1, 1, 100, 1000, 1100, 3, 4, 1};
    check(state.apply(delta) == Code::admit && state.get(1), "initial state");
    check(state.apply(delta) == Code::sequence, "duplicate state");
    delta.sequence = 3;
    check(state.apply(delta) == Code::sequence && !state.get(1), "gap invalidates state");
    delta.sequence = 2;
    check(state.apply(delta) == Code::unknown, "gap requires full snapshot");
    delta.sequence = 3;
    check(state.snapshot(delta) == Code::admit && state.get(1), "full snapshot resynchronizes");
    delta.sequence = 4; delta.observed_ns = 99;
    check(state.apply(delta) == Code::sequence, "timestamp cannot regress");
    delta.observed_ns = 101; delta.policy_epoch = 0;
    check(state.apply(delta) == Code::sequence, "policy epoch cannot regress");
    delta.policy_epoch = 1;
    check(state.apply(delta) == Code::admit, "next sequence");
    delta.bid = 1200;
    check(state.apply(delta) == Code::invalid, "crossed book rejected");
    for (std::uint64_t id = 2; id <= 4; ++id) { delta = {id, 1, 100, 1000, 1100, 3, 4, 1}; check(state.apply(delta) == Code::admit, "state capacity fill"); }
    delta.instrument = 5;
    check(state.apply(delta) == Code::capacity && !state.get(5), "state full no eviction");

    machine::CapitalBook<2> book(100);
    check(book.reserve(1, 7, 60) == Code::admit && book.held() == 60, "reserve");
    check(book.reserve(1, 7, 60) == Code::admit && book.held() == 60, "reserve replay");
    check(book.reserve(1, 8, 60) == Code::conflict, "idempotency binding");
    check(book.reserve(2, 8, 50) == Code::turnover, "shared budget");
    check(book.ambiguous(1) == Code::admit && book.held() == 60, "unknown retains funds");
    check(book.settle(1, 61) == Code::turnover && book.held() == 60, "overfill retains hold");
    check(book.settle(1, 40) == Code::admit && book.held() == 0 && book.spent() == 40, "actual settle");
    check(book.settle(1, 40) == Code::admit && book.spent() == 40, "terminal replay");
    check(book.settle(1, 41) == Code::conflict, "terminal mismatch");
    check(book.reserve(2, 8, 60) == Code::admit, "remaining budget");
    check(book.reserve(3, 9, 1) == Code::capacity, "no silent receipt eviction");
    check(book.settle(2, 0) == Code::admit && book.held() == 0, "confirmed rejection release");
    check(book.ambiguous(1) == Code::conflict, "terminal cannot reopen");
    std::cout << "{\"passed_assertions\":" << checks << ",\"threaded_events\":" << events
              << ",\"financial_transmissions\":0,\"authority\":\"NONE\"}\n";
}
