#pragma once
#include <cstdint>
struct EmProfile {
    std::uint64_t chain, account, version, expires_ns, initial_nonce;
    std::uint64_t max_position, clip, spread, inventory_weight, ttl_seconds, session_epoch;
};
struct EmFrame {
    std::uint64_t chain, now_ns, observed_ns, sequence, account_nonce, unix_seconds;
    std::uint64_t index, best_bid, best_ask;
    std::int64_t inventory, momentum;
    std::uint64_t resting_bid, resting_ask, resting_bid_lots, resting_ask_lots, resting_expiry, session_epoch;
};
struct EmCommand {
    std::uint8_t calldata[68];
    std::uint32_t size;
    std::uint64_t nonce, generation, policy_version, execution_authority;
};
// Fixed-capacity wire packer. The caller owns the output buffer; no allocation.
constexpr std::uint32_t em_batch_capacity = 32;
constexpr std::uint32_t em_batch_bytes = 1220;
extern "C" {
std::uint32_t em_abi_version();
void* em_create(const EmProfile*,std::uint64_t now_ns);
int em_step(void*,const EmFrame*,EmCommand*);
// 0: quote emitted, 7: confirmed quote unchanged and still fresh; no nonce consumed.
// Pack 1..32 unsigned commands into replaceBatch(bytes), preserving input FIFO order.
int em_pack_batch(const EmCommand*,std::uint32_t count,std::uint8_t* out,std::uint32_t capacity);
// Before signing only: outward price bounds for a simultaneous quote cohort.
int em_bound_quote(void*,std::uint64_t bid_ceiling,std::uint64_t ask_floor,EmCommand*);
// Caller must first reconcile receipt + canonical block + account state.
// status 1: nonce consumed; 0: transaction definitely reverted/not broadcast.
// Unknown, pending, dropped, or reorged outcomes must NOT be acknowledged.
int em_ack(void*,std::uint64_t account_nonce,int status);
void em_destroy(void*);
}
