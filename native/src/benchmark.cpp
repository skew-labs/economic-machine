#include "machine/kernel.hpp"

#include <algorithm>
#include <chrono>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
#if defined(__linux__)
#include <sched.h>
#endif

using Clock = std::chrono::steady_clock;
static std::uint64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
}
static bool pin(int core) {
#if defined(__linux__)
    if (core < 0 || core >= CPU_SETSIZE) return false;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    return sched_setaffinity(0, sizeof(set), &set) == 0;
#else
    (void)core;
    return false;
#endif
}

static void distribution(std::vector<std::uint64_t>& samples) {
    std::sort(samples.begin(), samples.end());
    auto q = [&](std::size_t n) { return samples[(samples.size() - 1) * n / 100]; };
    std::cout << "{\"samples\":" << samples.size() << ",\"p50_ns\":" << q(50)
              << ",\"p95_ns\":" << q(95) << ",\"p99_ns\":" << q(99)
              << ",\"max_ns\":" << samples.back() << '}';
}

int main(int argc, char** argv) {
    const int producer_core = argc > 1 ? std::stoi(argv[1]) : -1;
    const int consumer_core = argc > 2 ? std::stoi(argv[2]) : -1;
    const bool producer_pinned = pin(producer_core);
    constexpr std::size_t count = 100000;
    machine::Input input{1, 1, 7, 7, 1000, 950, 100, 1100, 1'000'000, 10'000'000, 10'000'000,
        1000, 1000, 1'000'000, 20'000'000, 30'000'000, 2'000'000,
        10'000'000, 20'000'000, 30'000'000, 15, 20, 1, 1};
    std::uint64_t checksum = 0;
    for (std::size_t i = 0; i < 10000; ++i) {
        input.sequence = input.expected_sequence = i + 1;
        checksum += machine::evaluate(input).notional;
    }
    std::vector<std::uint64_t> evaluations;
    evaluations.reserve(count);
    const auto start = now_ns();
    for (std::size_t i = 0; i < count; ++i) {
        input.sequence = input.expected_sequence = i + 1;
        const auto before = now_ns();
        const auto output = machine::evaluate(input);
        const auto after = now_ns();
        checksum += output.notional + output.sequence;
        evaluations.push_back(after - before);
    }
    const auto elapsed = now_ns() - start;
    struct Message { std::uint64_t sequence; std::uint64_t sent; };
    machine::SpscRing<Message, 4096> queue;
    std::vector<std::uint64_t> transit(count);
    bool consumer_pinned = false;
    bool ordered = true;
    std::thread consumer([&] {
        consumer_pinned = pin(consumer_core);
        for (std::size_t i = 0; i < count; ++i) {
            Message event;
            while (!queue.pop(event)) std::this_thread::yield();
            if (event.sequence != i + 1) ordered = false;
            transit[i] = now_ns() - event.sent;
        }
    });
    for (std::size_t i = 0; i < count; ++i) {
        Message event{i + 1, now_ns()};
        while (!queue.push(event)) std::this_thread::yield();
    }
    consumer.join();
    std::cout << "{\"schema\":\"machine-native-benchmark-1\",\"compiler\":\"" << __VERSION__
              << "\",\"warmup\":10000,\"clock\":\"steady_clock\",\"producer_core\":" << producer_core
              << ",\"consumer_core\":" << consumer_core << ",\"producer_pinned\":" << (producer_pinned ? "true" : "false")
              << ",\"consumer_pinned\":" << (consumer_pinned ? "true" : "false") << ",\"kernel\":";
    distribution(evaluations);
    std::cout << ",\"batch_elapsed_ns\":" << elapsed << ",\"spsc_transit\":";
    distribution(transit);
    std::cout << ",\"ordered\":" << (ordered ? "true" : "false") << ",\"checksum\":" << checksum
              << ",\"financial_transmissions\":0,\"llm_calls\":0,\"scope\":\"IN_MEMORY_KERNEL_NOT_VENUE_OR_CHAIN_LATENCY\"}\n";
    return ordered ? 0 : 1;
}
