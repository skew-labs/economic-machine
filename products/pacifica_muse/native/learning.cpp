// Reuse Economic Machine's typed HOLD-only DAG engine, not v6 execution/replay.
#include "../../../markets/solana/v6/evolution/native.cpp"
#include "../../../markets/solana/v6/research/fused_dag.hpp"
extern "C" void* ml_create(const Node*n,U count,const U*outputs){return fused_create(n,count,outputs,1,UINT64_MAX);}
extern "C" int ml_step(void*p,const I*features,U now,Output*out){return fused_step(p,features,now,now,out);}
extern "C" void ml_destroy(void*p){fused_destroy(p);}
