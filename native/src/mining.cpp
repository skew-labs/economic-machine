#include "machine/economics/amm.hpp"

// ABI 1: caller-owned fixed buffers. No network, wallet, allocator or language model.
// Asset quantities use the same normalized integer units as Economic Machine.
namespace {
using namespace machine::economics;
struct Edge { std::int64_t pool, in, out, reserve_in, reserve_out, fee, cost; };
struct Job {
    Edge edges[16];
    std::int64_t count, asset_in, asset_out, amount, minimum_net, max_cost, max_impact, max_hops;
};
struct Answer {
    std::int64_t path[4], length, net, output, cost, visited, feasible, complete;
};
bool valid(const Job& j) noexcept {
    if (j.count < 1 || j.count > 16 || j.asset_in <= 0 || j.asset_out <= 0 || j.asset_in == j.asset_out ||
        j.amount <= 0 || j.minimum_net < 0 || j.max_cost < 0 || j.max_impact < 0 || j.max_impact > 10000 ||
        j.max_hops < 1 || j.max_hops > 4) return false;
    for (std::int64_t i = 0; i < j.count; ++i) {
        const auto& e = j.edges[i];
        if (e.pool <= 0 || e.in <= 0 || e.out <= 0 || e.in == e.out || e.reserve_in <= 0 ||
            e.reserve_out <= 0 || e.fee < 0 || e.fee >= scale || e.cost < 0) return false;
    }
    return true;
}
Result<RouteQuote> quote(const Job& j, const std::int64_t* path, std::int64_t n) noexcept {
    SwapRoute r{}; r.count = static_cast<std::uint32_t>(n); r.amount_in = j.amount;
    r.minimum_output = j.minimum_net; r.max_cost = j.max_cost; r.max_impact_bps = j.max_impact;
    if (n < 1 || n > 4 || n > j.max_hops) return failure<RouteQuote>(Error::invalid);
    std::int64_t asset = j.asset_in;
    for (std::int64_t i = 0; i < n; ++i) {
        if (path[i] < 0 || path[i] >= j.count) return failure<RouteQuote>(Error::invalid);
        const auto& e = j.edges[path[i]];
        if (e.in != asset) return failure<RouteQuote>(Error::invalid);
        // Frozen model replay uses a fixed logical clock. This is NOT a live freshness attestation.
        Envelope envelope{};
        envelope.sequence = envelope.expected_sequence = 1;
        envelope.observed_ns = envelope.now_ns = 1;
        envelope.max_age_ns = 1; envelope.deadline_ns = 2;
        envelope.policy_version = envelope.expected_policy_version = 1;
        r.pools[i] = {static_cast<std::uint64_t>(e.pool), static_cast<std::uint64_t>(e.in),
            static_cast<std::uint64_t>(e.out), e.reserve_in, e.reserve_out, e.fee, e.cost, envelope};
        asset = e.out;
    }
    if (asset != j.asset_out) return failure<RouteQuote>(Error::invalid);
    auto q = simulate_route(r);
    if (q && (q.value.amount_out <= q.value.fixed_cost ||
        q.value.amount_out - q.value.fixed_cost < j.minimum_net)) return failure<RouteQuote>(Error::cost);
    return q;
}
struct Search {
    const Job& j; Answer& best; std::int64_t budget; std::int64_t path[4]{};
    void walk(std::int64_t asset, std::int64_t depth) noexcept {
        if (depth < 0 || depth >= 4 || depth >= j.max_hops) return;
        for (std::int64_t i = 0; i < j.count; ++i) {
            if (j.edges[i].in != asset) continue;
            bool repeated = false;
            for (std::int64_t k = 0; k < depth; ++k)
                if (j.edges[path[k]].pool == j.edges[i].pool) repeated = true;
            if (repeated) continue;
            if (best.visited == budget) { best.complete = 0; return; }
            ++best.visited; path[depth] = i;
            if (j.edges[i].out == j.asset_out) {
                const auto q = quote(j, path, depth + 1);
                if (q) {
                    ++best.feasible;
                    const auto net = q.value.amount_out - q.value.fixed_cost;
                    if (!best.length || net > best.net) {
                        best.length = depth + 1; best.net = net;
                        best.output = q.value.amount_out; best.cost = q.value.fixed_cost;
                        for (std::int64_t k = 0; k <= depth; ++k) best.path[k] = path[k];
                    }
                }
            }
            // A path may reach its target, leave it, then return via distinct pools.
            // Enumerate those too; forbid repeated pools, not intermediate assets.
            walk(j.edges[i].out, depth + 1);
            if (!best.complete) return;
        }
    }
};
}
extern "C" {
unsigned machine_mining_abi() noexcept { return 1; }
std::size_t machine_mining_job_size() noexcept { return sizeof(Job); }
std::size_t machine_mining_answer_size() noexcept { return sizeof(Answer); }
int machine_mining_score(const Job* j, const std::int64_t* path, std::int64_t n, Answer* out) noexcept {
    if (!j || !path || !out) return 1;
    *out = {}; if (!valid(*j)) return 1;
    const auto q = quote(*j, path, n); if (!q) return static_cast<int>(q.error);
    out->length = n; out->net = q.value.amount_out - q.value.fixed_cost;
    out->output = q.value.amount_out; out->cost = q.value.fixed_cost;
    for (std::int64_t i = 0; i < n; ++i) out->path[i] = path[i];
    out->complete = 1; out->feasible = 1; return 0;
}
int machine_mining_search(const Job* j, std::int64_t budget, Answer* out) noexcept {
    if (!j || !out) return 1;
    *out = {}; if (!valid(*j) || budget < 1 || budget > 70000) return 1;
    out->complete = 1; Search search{*j, *out, budget}; search.walk(j->asset_in, 0);
    return 0;
}
}
