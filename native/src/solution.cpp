#include <array>
#include <cstddef>
#include <cstdint>

namespace {
constexpr std::uint32_t max_nodes = 64, max_edges = 2016;
struct Edge { std::uint32_t u, v, weight; };
struct Graph { Edge edges[max_edges]; std::uint32_t nodes, count; };
struct Answer { std::uint64_t bits, score, edge_visits, candidates; std::uint32_t valid, complete; };

// Single-producer/single-consumer within one worker; not a cross-thread queue.
// Fixed capacity avoids allocations in candidate evaluation and result staging.
template<class T, std::size_t Size> struct FixedQueue {
    std::array<T,Size> data{}; std::size_t head{}, length{};
    bool push(const T& item) noexcept { if(length == Size)return false;data[(head+length)%Size]=item;++length;return true; }
    bool pop(T& item) noexcept { if(!length)return false;item=data[head];head=(head+1)%Size;--length;return true; }
};
bool valid(const Graph& g) noexcept {
    if(g.nodes < 2 || g.nodes > max_nodes || g.count < 1 || g.count > max_edges)return false;
    bool seen[max_nodes][max_nodes]{};
    for(std::uint32_t i=0;i<g.count;++i){
        auto e=g.edges[i]; if(e.u>=g.nodes || e.v>=g.nodes || e.u>=e.v || !e.weight || e.weight>1024 || seen[e.u][e.v])return false;
        seen[e.u][e.v]=true;
    }return true;
}
std::uint64_t canonical(std::uint64_t bits,std::uint32_t nodes) noexcept {
    const auto mask=nodes==64?UINT64_MAX:((std::uint64_t{1}<<nodes)-1);
    bits &= mask; return bits&1 ? bits^mask : bits;
}
std::uint64_t score(const Graph& g,std::uint64_t bits) noexcept {
    std::uint64_t total=0;
    for(std::uint32_t i=0;i<g.count;++i){const auto e=g.edges[i];if(((bits>>e.u)^(bits>>e.v))&1)total+=e.weight;}
    return total;
}
std::uint64_t random(std::uint64_t& state) noexcept {
    // SplitMix64: reproducible search randomness, never used for on-chain problem generation.
    auto z=(state+=0x9e3779b97f4a7c15ULL);z=(z^(z>>30))*0xbf58476d1ce4e5b9ULL;
    z=(z^(z>>27))*0x94d049bb133111ebULL;return z^(z>>31);
}
}
extern "C" {
unsigned solution_abi() noexcept{return 1;}
std::size_t solution_graph_size() noexcept{return sizeof(Graph);}
std::size_t solution_answer_size() noexcept{return sizeof(Answer);}
int solution_score(const Graph* g,std::uint64_t bits,Answer* out) noexcept {
    if(!g||!out) { return 1; }
    *out={};if(!valid(*g))return 1;
    if(canonical(bits,g->nodes)!=bits)return 1;
    out->bits=bits;out->score=score(*g,bits);out->valid=out->complete=1;
    out->edge_visits=g->count;out->candidates=1;return 0;
}
int solution_score_batch(const Graph* g,const std::uint64_t* bits,std::size_t count,Answer* out) noexcept {
    if(!g||!bits||!out||!count||count>64||!valid(*g)) { return 1; }
    for(std::size_t i=0;i<count;++i){
        out[i]={};
        if(canonical(bits[i],g->nodes)!=bits[i])continue;
        out[i].bits=bits[i];out[i].score=score(*g,bits[i]);out[i].edge_visits=g->count;
        out[i].candidates=1;out[i].valid=out[i].complete=1;
    }return 0;
}
int solution_search(const Graph* g,std::uint64_t seed,std::uint64_t budget,unsigned algorithm,Answer* out) noexcept {
    if(!g||!out) { return 1; }
    *out={};if(!valid(*g)||budget<g->count||budget>10000000||algorithm>3)return 1;
    FixedQueue<Answer,8> queue;
    auto accept=[&](std::uint64_t bits,std::uint64_t value){
        ++out->candidates;
        if(!out->valid||value>out->score||(value==out->score&&bits<out->bits)){
            Answer candidate{};candidate.bits=bits;candidate.score=value;candidate.valid=1;
            if(!queue.push(candidate))return false;
            Answer staged{};while(queue.pop(staged)){out->bits=staged.bits;out->score=staged.score;out->valid=1;}
        }return true;
    };
    if(algorithm==2){
        // Exact reference for small graphs only, to validate search quality and symmetry handling.
        if(g->nodes>20)return 1;
        const auto possibilities=std::uint64_t{1}<<(g->nodes-1);
        for(std::uint64_t i=0;i<possibilities;++i){
            if(out->edge_visits+g->count>budget)return 0;
            const auto bits=i<<1;out->edge_visits+=g->count;if(!accept(bits,score(*g,bits)))return 2;
        }out->complete=1;return 0;
    }
    if(algorithm==0){
        while(out->edge_visits+g->count<=budget){
            auto bits=canonical(random(seed),g->nodes);out->edge_visits+=g->count;
            if(!accept(bits,score(*g,bits)))return 2;
        }return 0;
    }
    std::uint32_t weights[max_nodes][max_nodes]{};
    for(std::uint32_t i=0;i<g->count;++i){auto e=g->edges[i];weights[e.u][e.v]=weights[e.v][e.u]=e.weight;}
    std::uint64_t bits=canonical(random(seed),g->nodes), current=score(*g,bits), flips=0;
    out->edge_visits=g->count;if(!accept(bits,current))return 2;
    while(out->edge_visits+g->nodes-1<=budget){
        const auto node=static_cast<std::uint32_t>(random(seed)%g->nodes);
        std::int64_t gain=0;
        for(std::uint32_t v=0;v<g->nodes;++v)if(v!=node){
            ++out->edge_visits;const auto w=weights[node][v];
            gain+=(((bits>>node)^(bits>>v))&1)?-static_cast<std::int64_t>(w):static_cast<std::int64_t>(w);
        }
        // Integer escape probability. No floating-point or hardware-dependent exp() in the hot path.
        const std::uint64_t temperature=32+((4096-(flips%4096))*8);
        if(gain>=0||(algorithm==1&&random(seed)%(temperature+static_cast<std::uint64_t>(-gain))<temperature/8)){
            bits=canonical(bits^(std::uint64_t{1}<<node),g->nodes);
            current=static_cast<std::uint64_t>(static_cast<std::int64_t>(current)+gain);
            if(!accept(bits,current))return 2;
        }
        ++flips;
    }return 0; // A heuristic never asserts global optimality.
}
}
