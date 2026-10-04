#include "../src/mining.cpp"
#include <cassert>
#include <iostream>

int main() {
    Job j{}; j.count = 3; j.asset_in = 1; j.asset_out = 3; j.amount = 1000000;
    j.minimum_net = 1; j.max_cost = 1000; j.max_impact = 500; j.max_hops = 4;
    j.edges[0] = {1,1,3,1000000000,1000000000,3000,100};
    j.edges[1] = {2,1,2,1000000000,1200000000,1000,150};
    j.edges[2] = {3,2,3,1000000000,1000000000,1000,150};
    Answer answer{};
    assert(machine_mining_search(&j,70000,&answer) == 0);
    assert(answer.complete && answer.length == 2 && answer.path[0] == 1 && answer.path[1] == 2);
    assert(machine_mining_search(&j,1,&answer) == 0 && !answer.complete);
    std::int64_t path[4] = {0,0,0,0};
    assert(machine_mining_score(&j,path,5,&answer) != 0);
    assert(machine_mining_score(&j,path,-1,&answer) != 0);
    assert(machine_mining_score(nullptr,path,1,&answer) != 0);
    j.count = 17; assert(machine_mining_search(&j,70000,&answer) != 0);
    j.count = 3; j.max_hops = 5; assert(machine_mining_search(&j,70000,&answer) != 0);
    j.max_hops = 4; j.edges[0].reserve_in = INT64_MAX;
    assert(machine_mining_score(&j,path,1,&answer) != 0);
    // Dense cyclic graph: exercise the entire bounded depth and repeated-pool pruning under sanitizers.
    j.count = 16; j.amount = 1000000;
    for (std::int64_t i = 0; i < 16; ++i)
        j.edges[i] = {i+1, i%2 ? 2 : 1, i%2 ? 1 : 2,1000000000,1000000000,0,0};
    j.asset_out = 3;
    assert(machine_mining_search(&j,70000,&answer) == 0 && answer.complete && !answer.length);
    assert(answer.visited > 1000 && answer.visited <= 70000);
    std::cout << "MINING_NATIVE_BOUNDARIES_AND_DENSE_GRAPH_PASS expansions=" << answer.visited << '\n';
}
