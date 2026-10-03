// Fixed-frame Linux worker. No RPC, API credentials, wallet or transaction submission.
// The shared-library solver stays scalar and portable; concurrency is in this boundary.
#include "solution.cpp"
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <type_traits>
#include <pthread.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <unistd.h>

namespace {
namespace pipeline {
constexpr std::uint32_t magic = 0x534f4c32, version = 1;
[[maybe_unused]] bool sandbox() noexcept {
    // Installed after the dynamic loader but before threads: descendants inherit it.
    // The trusted parent supplies only three pipe FDs and no provider/wallet secrets.
    // read/write permit the protocol pipes; no open/socket/connect/exec syscall exists.
#define ALLOW_SYSCALL(number) BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, number, 0, 1), BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)
    sock_filter rules[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(seccomp_data, nr)),
        ALLOW_SYSCALL(SYS_read), ALLOW_SYSCALL(SYS_write), ALLOW_SYSCALL(SYS_close),
        ALLOW_SYSCALL(SYS_fstat), ALLOW_SYSCALL(SYS_newfstatat),
        ALLOW_SYSCALL(SYS_mmap), ALLOW_SYSCALL(SYS_mprotect), ALLOW_SYSCALL(SYS_munmap),
        ALLOW_SYSCALL(SYS_brk), ALLOW_SYSCALL(SYS_madvise),
        ALLOW_SYSCALL(SYS_clone), ALLOW_SYSCALL(SYS_futex), ALLOW_SYSCALL(SYS_set_robust_list),
        ALLOW_SYSCALL(SYS_rt_sigaction), ALLOW_SYSCALL(SYS_rt_sigprocmask), ALLOW_SYSCALL(SYS_rt_sigreturn),
        ALLOW_SYSCALL(SYS_sigaltstack), ALLOW_SYSCALL(SYS_getpid), ALLOW_SYSCALL(SYS_gettid),
        ALLOW_SYSCALL(SYS_tgkill), ALLOW_SYSCALL(SYS_getrandom), ALLOW_SYSCALL(SYS_prlimit64),
        ALLOW_SYSCALL(SYS_sched_yield), ALLOW_SYSCALL(SYS_sched_setaffinity), ALLOW_SYSCALL(SYS_sched_getaffinity),
        ALLOW_SYSCALL(SYS_clock_gettime), ALLOW_SYSCALL(SYS_exit), ALLOW_SYSCALL(SYS_exit_group),
#ifdef SYS_rseq
        ALLOW_SYSCALL(SYS_rseq),
#endif
        // glibc may probe clone3; ENOSYS makes it use the permitted clone implementation.
#ifdef SYS_clone3
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
#endif
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
    };
#undef ALLOW_SYSCALL
    sock_fprog program{static_cast<unsigned short>(sizeof(rules) / sizeof(rules[0])), rules};
    return prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0 && prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) == 0;
}
struct Request {
    std::uint32_t magic, version;
    std::uint64_t id;
    Graph graph;
    std::uint64_t seed, budget;
    std::uint32_t algorithm, reserved;
};
struct Result {
    std::uint32_t magic{pipeline::magic}, version{pipeline::version};
    std::uint64_t id{};
    Answer answer{};
    std::uint64_t elapsed_ns{};
    std::uint32_t status{}, reserved{};
};
struct Work { Request request{}; Result result{}; };
static_assert(sizeof(Request) == 24240 && sizeof(Result) == 72);
static_assert(std::is_trivially_copyable_v<Work>);

// Exactly one producer and one consumer. Capacity eight bounds queued graphs in RAM.
// Release publishes the fully copied payload; acquire observes it before slot reuse.
template<class T, std::size_t N> class Ring {
    std::array<T, N> slots_{};
    alignas(64) std::atomic<std::uint64_t> write_{0};
    alignas(64) std::atomic<std::uint64_t> read_{0};
public:
    bool push(const T& value) noexcept {
        auto w = write_.load(std::memory_order_relaxed);
        if (w - read_.load(std::memory_order_acquire) == N) return false;
        slots_[w % N] = value;
        write_.store(w + 1, std::memory_order_release);
        return true;
    }
    bool pop(T& value) noexcept {
        auto r = read_.load(std::memory_order_relaxed);
        if (r == write_.load(std::memory_order_acquire)) return false;
        value = slots_[r % N];
        read_.store(r + 1, std::memory_order_release);
        return true;
    }
};

bool pin(int cpu) noexcept {
    if (cpu == -1) return true;
    if (cpu < 0 || cpu >= CPU_SETSIZE) return false;
    cpu_set_t set;
    CPU_ZERO(&set); CPU_SET(cpu, &set);
    return pthread_setaffinity_np(pthread_self(), sizeof(set), &set) == 0;
}
int read_frame(Request& input) noexcept {
    std::size_t done = 0;
    auto* bytes = reinterpret_cast<char*>(&input);
    while (done < sizeof(input)) {
        auto n = ::read(STDIN_FILENO, bytes + done, sizeof(input) - done);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 || (n == 0 && done != 0)) return -1;
        if (n == 0) return 0;
        done += static_cast<std::size_t>(n);
    }
    return 1;
}
bool write_frame(const Result& output) noexcept {
    const auto* bytes = reinterpret_cast<const char*>(&output);
    std::size_t done = 0;
    while (done < sizeof(output)) {
        auto n = ::write(STDOUT_FILENO, bytes + done, sizeof(output) - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        done += static_cast<std::size_t>(n);
    }
    return true;
}

[[maybe_unused]] int run(int search_cpu = -1, int verify_cpu = -1) {
    Ring<Request, 8> incoming;
    Ring<Work, 8> candidates;
    std::atomic<bool> input_done{false}, search_done{false}, stop{false}, failed{false};
    std::thread search([&] {
        if (!pin(search_cpu)) { failed = true; stop = true; search_done = true; return; }
        Request request{};
        while (!stop.load(std::memory_order_acquire)) {
            if (!incoming.pop(request)) {
                if (!input_done.load(std::memory_order_acquire)) { std::this_thread::yield(); continue; }
                if (!incoming.pop(request)) break;
            }
            Work work{}; work.request = request; work.result.id = request.id;
            auto start = std::chrono::steady_clock::now();
            work.result.status = static_cast<std::uint32_t>(solution_search(
                &request.graph, request.seed, request.budget, request.algorithm, &work.result.answer));
            work.result.elapsed_ns = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - start).count());
            while (!candidates.push(work) && !stop.load(std::memory_order_acquire)) std::this_thread::yield();
        }
        search_done.store(true, std::memory_order_release);
    });
    std::thread verifier([&] {
        if (!pin(verify_cpu)) { failed = true; stop = true; return; }
        Work work{};
        while (!stop.load(std::memory_order_acquire)) {
            if (!candidates.pop(work)) {
                if (!search_done.load(std::memory_order_acquire)) { std::this_thread::yield(); continue; }
                if (!candidates.pop(work)) break;
            }
            Answer checked{};
            if (!work.result.status && (solution_score(&work.request.graph, work.result.answer.bits, &checked)
                    || !work.result.answer.valid || checked.score != work.result.answer.score
                    || work.result.answer.edge_visits > work.request.budget)) work.result.status = 2;
            if (!write_frame(work.result)) { failed = true; stop = true; break; }
        }
    });
    for (std::uint64_t count = 0; !stop.load(std::memory_order_acquire); ++count) {
        Request request{};
        int state = read_frame(request);
        if (state == 0) break;
        if (state < 0 || count >= 128 || request.magic != magic || request.version != version || request.reserved) {
            failed = true; stop = true; break;
        }
        while (!incoming.push(request) && !stop.load(std::memory_order_acquire)) std::this_thread::yield();
    }
    input_done.store(true, std::memory_order_release);
    search.join(); verifier.join();
    return failed ? 2 : 0;
}
}
}

#ifndef SOLUTION_PIPELINE_TEST
int main(int argc, char** argv) {
    // ABI frames are intentionally local little-endian x86_64, not a network protocol.
    if (__BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__ || !pipeline::sandbox()) return 2;
    if (argc == 2 && std::strcmp(argv[1], "--sandbox-self-test") == 0) {
        int file = ::open("/etc/hosts", O_RDONLY);
        if (file != -1 || errno != EPERM) return 2;
        int connection = ::socket(AF_INET, SOCK_STREAM, 0);
        if (connection != -1 || errno != EPERM) return 2;
        constexpr char message[] = "SANDBOX_FILE_NETWORK_DENY_PASS\n";
        return ::write(STDOUT_FILENO, message, sizeof(message)-1) == static_cast<ssize_t>(sizeof(message)-1) ? 0 : 2;
    }
    int search = -1, verify = -1;
    if (argc != 1 && argc != 5) return 2;
    if (argc == 5) {
        if (std::strcmp(argv[1], "--search-cpu") || std::strcmp(argv[3], "--verify-cpu")) return 2;
        auto parse = [](const char* value, int& output) {
            char* end = nullptr; errno = 0; long cpu = std::strtol(value, &end, 10);
            if (errno || end == value || *end || cpu < 0 || cpu >= CPU_SETSIZE) return false;
            output = static_cast<int>(cpu); return true;
        };
        if (!parse(argv[2], search) || !parse(argv[4], verify)) return 2;
    }
    return pipeline::run(search, verify);
}
#endif
