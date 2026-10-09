#include "engine.hpp"
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <vector>
static std::size_t allocations{};
void* operator new(std::size_t n){++allocations;if(auto*p=std::malloc(n))return p;throw std::bad_alloc();}
void operator delete(void*p)noexcept{std::free(p);}void operator delete(void*p,std::size_t)noexcept{std::free(p);}
using U=std::uint64_t;
static U get(const std::uint8_t*b,unsigned start,unsigned width){U r{};for(unsigned i=0;i<width;++i)if(b[31-(start+i)/8]&(1U<<((start+i)%8)))r|=U{1}<<i;return r;}
int main(){
    EmProfile p{421614,7,1,100000000000ULL,0,8,2,3,1,30,1};void* e=em_create(&p,1);assert(e);
    EmFrame f{421614,2,2,1,0,10000,1000,999,1001,8,0,0,0,0,0,0,1};EmCommand out{};
    assert(em_step(e,&f,&out)==0 && out.size==68 && !out.execution_authority);
    assert(get(out.calldata+36,200,32)==1);
    assert(get(out.calldata+4,0,32)==7 && get(out.calldata+36,0,64)==1);
    assert(get(out.calldata+36,136,32)==0 && get(out.calldata+36,168,32)==2);
    assert(em_step(e,&f,&out)==1);assert(em_ack(e,9,1)==2);assert(em_ack(e,1,1)==0);
    f.account_nonce=1;f.sequence=2;f.now_ns=f.observed_ns=3;f.inventory=-8;
    assert(em_step(e,&f,&out)==0);assert(get(out.calldata+36,168,32)==0);assert(em_ack(e,1,0)==0);
    assert(em_step(e,&f,&out)==2);f.sequence++;f.chain=1;assert(em_step(e,&f,&out)==2);f.chain=421614;
    f.now_ns=1000000005;assert(em_step(e,&f,&out)==3);em_destroy(e);
    e=em_create(&p,1);assert(e);
    f={421614,2,2,1,0,10000,1000,999,1001,0,0,0,0,0,0,0,1};assert(em_step(e,&f,&out)==0);
    assert(em_ack(e,1,1)==0);f.account_nonce=1;
    f.resting_bid=get(out.calldata+36,104,16);f.resting_ask=get(out.calldata+36,120,16);
    f.resting_bid_lots=get(out.calldata+36,136,32);f.resting_ask_lots=get(out.calldata+36,168,32);f.resting_expiry=10030;
    auto quiet_start=std::chrono::steady_clock::now();auto quiet_allocations=allocations;
    for(U i=0;i<1000000;++i){f.sequence++;f.now_ns=f.observed_ns=i+3;assert(em_step(e,&f,&out)==7 && out.size==0);}
    auto quiet_ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-quiet_start).count();
    assert(allocations==quiet_allocations);
    f.unix_seconds=10015;f.sequence++;f.now_ns=f.observed_ns=1000010;assert(em_step(e,&f,&out)==0);
    assert(out.nonce==2);assert(em_ack(e,1,0)==0);
    // A rejected renewal must not extend the cache's actual on-chain deadline.
    f.sequence++;f.now_ns=f.observed_ns=1000011;assert(em_step(e,&f,&out)==0 && out.nonce==2);
    std::uint8_t batch[em_batch_bytes]{};auto before=allocations;
    assert(em_pack_batch(&out,1,batch,sizeof(batch))==132 && allocations==before);
    assert(batch[0]==0x30 && batch[35]==32 && batch[67]==36);
    EmCommand pair[2]={out,out};assert(em_pack_batch(pair,2,batch,sizeof(batch))==-4);
    assert(em_pack_batch(&out,1,batch,131)==-2);out.execution_authority=1;
    assert(em_pack_batch(&out,1,batch,sizeof(batch))==-3);em_destroy(e);
    e=em_create(&p,1);assert(e);f={421614,2,2,1,0,10000,1000,999,1001,0,0,0,0,0,0,0,1};
    assert(em_step(e,&f,&out)==0 && em_ack(e,1,1)==0);f.account_nonce=1;f.sequence=2;f.now_ns=f.observed_ns=3;
    // A balanced fill can leave inventory unchanged but consume both quotes.
    assert(em_step(e,&f,&out)==0 && out.nonce==2);em_destroy(e);
    e=em_create(&p,1);assert(e);std::vector<U> samples;samples.reserve(20000);U nonce=0;
    for(U i=0;i<21000;++i){
        f={421614,i+2,i+2,i+1,nonce,10000+i/1000,1000,999,1001,0,0,0,0,0,0,0,1};
        f.inventory=std::int64_t(i%17)-8;f.momentum=(i&1)?4:-4;
        auto before=allocations;auto start=std::chrono::steady_clock::now();int rc=em_step(e,&f,&out);auto end=std::chrono::steady_clock::now();
        assert(rc==0 && allocations==before);assert(get(out.calldata+36,64,40)==f.unix_seconds+30);
        assert(get(out.calldata+36,136,32)<=2 && get(out.calldata+36,168,32)<=2);
        assert(em_ack(e,++nonce,1)==0);if(i>=1000)samples.push_back(std::chrono::duration_cast<std::chrono::nanoseconds>(end-start).count());
    }
    em_destroy(e);std::sort(samples.begin(),samples.end());
    std::printf("{\"samples\":20000,\"p50_ns\":%llu,\"p99_ns\":%llu,\"max_ns\":%llu,\"quiet_events\":1000000,\"quiet_mean_ns\":%llu,\"hot_allocations\":0,\"checks_passed\":true}\n",(unsigned long long)samples[10000],(unsigned long long)samples[19800],(unsigned long long)samples.back(),(unsigned long long)(quiet_ns/1000000));
}
