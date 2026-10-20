#include "machine/economics/pump_signal.hpp"
#include <new>
using namespace machine::economics::pump;
extern "C" {
uint64_t machine_pump_abi() {return 1;}
void* machine_pump_create(const Policy* p,uint64_t slot) noexcept {
    if(!p || !valid(*p))return nullptr;
    return new(std::nothrow) Engine(*p,slot);
}
void machine_pump_destroy(void* p) noexcept {delete static_cast<Engine*>(p);}
uint64_t machine_pump_event(void* p,const Event* e) noexcept {
    return p&&e ? static_cast<Engine*>(p)->ingest(*e) : bad_input;
}
uint64_t machine_pump_funding(void* p,uint64_t wallet,uint64_t funder,uint64_t slot) noexcept {
    return p && static_cast<Engine*>(p)->funding(wallet,funder,slot);
}
void machine_pump_invalidate(void* p) noexcept {if(p)static_cast<Engine*>(p)->invalidate();}
uint64_t machine_pump_decide(void* p,uint64_t slot,uint64_t ms,Result* r) noexcept {
    if(!p || !r)return bad_input;
    *r=static_cast<Engine*>(p)->decide(slot,ms);return r->reason;
}
uint64_t machine_pump_quotes(const Event* e,uint64_t budget,uint64_t* tokens,uint64_t* proceeds) noexcept {
    return e && tokens && proceeds && quotes(*e,budget,*tokens,*proceeds);
}
uint64_t machine_pump_bytes() {return sizeof(Engine);}
}
