#pragma once
#include <cstdint>
#include <type_traits>

namespace research {
using U=std::uint64_t;using I=std::int64_t;
struct Digest { unsigned char bytes[32]; };
// Cold, immutable snapshot. One owner thread owns each installed engine.
struct Profile {
    U abi,revision; Digest user,hash,parent;
    I position,clip,min_spread,max_spread,max_skew,loss;
    I weights[5]; U seat,style,reserved;
};
struct Quote { I side,price,lots,reduce; };
struct Decision { I raw[4]; Quote quotes[2]; U count,reduce; };
// Fixed 128-byte frame. Flat order/trade arrays are packed once at ingestion.
struct Frame {
    U id,slot,unix_ns,oracle_slot,publish_ns;
    I mark,funding,imbalance,momentum,depth;
    U order_offset,order_count,verified,provenance,external,reserved;
};
struct Order { I side,seat,price,lots; };
struct Trade { U slot; I side,lots,turnover; };
struct Fence {
    U cutoff_id,finished_ns,consumed_id,purge_frames;
    I quote_cost,exit_cost,maker_fee_bps; U training;
};
struct Metrics {
    I pnl,drawdown,inventory_sum,max_position,adverse,fills,turnover,updates;
    I exit_fee,maker_fees,network_cost,score_micro,frames,loss_stop;
};
struct Trace { U slot; I position,equity; };
struct Comparison { Metrics parent[2],candidate[2]; U eligible; };
struct Outcome {
    Digest user,profile,parent,candidate,window;
    U revision,first_id,last_id,first_ns,last_ns,cutoff_id,finished_ns;
    Comparison comparison;
};
static_assert(sizeof(Profile)==224 && sizeof(Frame)==128 && sizeof(Order)==32);
static_assert(std::is_trivially_copyable_v<Profile> && sizeof(Digest)==32);
}
extern "C" {
void* rp_engine_create(const void* nodes,std::uint64_t count,const std::uint64_t* outputs,
    const research::Profile*,std::uint64_t now,std::uint64_t expires);
int rp_engine_step(void*,const std::int64_t* features,std::int64_t mark,std::int64_t bid,std::int64_t ask,
    std::uint64_t now,std::uint64_t observed,research::Decision*);
void rp_engine_destroy(void*);
int rp_replay(void*,const research::Frame*,std::uint64_t,const research::Order*,std::uint64_t,
    const research::Trade*,std::uint64_t,const research::Fence*,std::uint64_t delay,std::uint64_t queue,
    research::Metrics*,research::Trace*);
int rp_compare(void* const* engines,const research::Frame*,std::uint64_t,const research::Order*,std::uint64_t,
    const research::Trade*,std::uint64_t,const research::Fence*,research::Comparison*);
void* rp_registry_open(const char*);
void rp_registry_close(void*);
int rp_profile_append(void*,const research::Profile*,std::uint64_t expected_revision);
int rp_profile_get(void*,const research::Digest*,std::uint64_t revision,research::Profile*);
int rp_outcome_append(void*,const research::Outcome*);
int rp_outcome_get(void*,const research::Digest*,std::uint64_t revision,std::uint64_t last_id,research::Outcome*);
int rp_cursor(void*,const research::Digest*,std::uint64_t revision,std::uint64_t*);
}
