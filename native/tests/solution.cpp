#include "../src/solution.cpp"
#include <cassert>
#include <iostream>

int main(){
    Graph g{};g.nodes=3;g.count=3;g.edges[0]={0,1,1};g.edges[1]={0,2,1};g.edges[2]={1,2,1};
    Answer out{};assert(solution_search(&g,1,1000,2,&out)==0&&out.score==2&&out.complete);
    assert(out.bits==2);assert(solution_score(&g,1,&out)!=0);
    std::uint64_t candidates[4]={0,2,4,1};Answer batch[4]{};
    assert(solution_score_batch(&g,candidates,4,batch)==0&&batch[1].score==2&&batch[2].score==2&&!batch[3].valid);
    assert(solution_score_batch(&g,candidates,65,batch)!=0);
    assert(solution_score(&g,UINT64_MAX,&out)!=0);
    assert(solution_search(&g,1,1,1,&out)!=0);
    g.nodes=65;assert(solution_search(&g,1,1000,1,&out)!=0);
    g.nodes=64;g.count=0;
    for(std::uint32_t u=0;u<64;++u)for(std::uint32_t v=u+1;v<64;++v)g.edges[g.count++]={u,v,1};
    assert(solution_search(&g,42,100000,1,&out)==0&&out.valid&&!out.complete);
    assert(out.edge_visits<=100000&&out.score<=1024);
    auto bits=out.bits,value=out.score;assert(solution_score(&g,bits,&out)==0&&out.score==value);
    g.edges[0].u=64;assert(solution_search(&g,1,10000,1,&out)!=0);
    FixedQueue<int,2> q;int x{};assert(q.push(1)&&q.push(2)&&!q.push(3));assert(q.pop(x)&&x==1);assert(q.pop(x)&&x==2);assert(!q.pop(x));
    std::cout<<"SOLUTION_BOUNDARIES_EXACT_TRIANGLE_DENSE64_QUEUE_PASS\n";
}
