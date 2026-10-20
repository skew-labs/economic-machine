#include "machine/economics/value.hpp"
#include <cstdint>
// A stateless signal gate. Feed identity/sequence/deadline checks are performed
// before entering this function. Economic Machine's checked fixed-point math
// keeps overflow out of the model-trigger decision. No orders are generated.
extern "C" int em_resident_gate(std::int64_t price,std::int64_t reference,
                                std::int64_t threshold_bps) {
    using namespace machine::economics;
    if(price<=0 || reference<=0 || threshold_bps<1 || threshold_bps>10000) return -1;
    auto delta=subtract(price,reference);
    if(!delta) return -1;
    auto magnitude=absolute(delta.value);
    if(!magnitude) return -1;
    // Cross multiplication avoids flooring away sub-basis-point crossings.
    return Wide(magnitude.value)*bps_scale >= Wide(reference)*threshold_bps ? 1 : 0;
}
