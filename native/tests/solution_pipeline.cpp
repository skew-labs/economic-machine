#define SOLUTION_PIPELINE_TEST
#include "../src/solution_pipeline.cpp"
#include <cassert>

int main() {
    pipeline::Ring<std::uint64_t,8> ring;
    for (std::uint64_t i = 0; i < 8; ++i) assert(ring.push(i));
    assert(!ring.push(9));
    for (std::uint64_t i = 0, out = 0; i < 8; ++i) { assert(ring.pop(out)); assert(out == i); }
    std::uint64_t out{}; assert(!ring.pop(out));
    std::atomic<bool> failed{false};
    std::thread producer([&] { for (std::uint64_t i = 0; i < 200000; ++i) while (!ring.push(i)) std::this_thread::yield(); });
    std::thread consumer([&] { for (std::uint64_t i = 0, item = 0; i < 200000; ++i) {
        while (!ring.pop(item)) { std::this_thread::yield(); }
        if (item != i) failed = true;
    } });
    producer.join(); consumer.join(); assert(!failed);
    assert(!pipeline::pin(CPU_SETSIZE));
    std::cout << "SPSC_BACKPRESSURE_200000_ORDERED_NO_LOSS_PASS\n";
}
