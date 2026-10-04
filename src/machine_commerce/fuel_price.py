"""Independent price floor and sequencer gate, using pinned Arbitrum feeds.

This protects the reviewed quote; the signed CoW order enforces its ETH minimum.
It is a bounded sanity check, not a prediction or guarantee of market fairness.
"""
from eth_abi import decode

from economic_machine.values import MachineError

from .datapass import call_data
from .fuel_rpc import source_label

ETH_USD = "0x639fe6ab55c921f74e7fac1ee960c0b6293ba612"
USDC_USD = "0x50834f3163758fcc1df9973b6e91f0f0f0434ad3"
SEQUENCER = "0xfdb631f5ee196f0ed6faa767959853a9f217697d"
ROUND_TYPES = ["uint80", "int256", "uint256", "uint256", "uint80"]


def price_floor(reader, urls, clock, amount, minimum):
    observations = []
    for index, url in enumerate(urls):
        block = reader(url, "eth_getBlockByNumber", ["latest", False])
        # A later RPC can return a block mined after the first request started.
        # Sample the clock after each read instead of comparing to a frozen time.
        now = int(clock())
        if not 0 <= now - int(block["timestamp"], 16) <= 120:
            raise MachineError("FRESH_CHAIN_STATE_REQUIRED")

        def call(target, signature, types, url=url, height=block["number"]):
            value = reader(url, "eth_call", [{"to": target, "data": call_data(signature, [], [])}, height])
            return decode(types, bytes.fromhex(value[2:]))

        _, down, since, _, _ = call(SEQUENCER, "latestRoundData()", ROUND_TYPES)
        if down != 0 or since == 0 or now - since <= 3600:
            raise MachineError("SEQUENCER_UNAVAILABLE_OR_RECOVERING")
        values = []
        for feed, description, max_age in [(ETH_USD, "ETH / USD", 3600), (USDC_USD, "USDC / USD", 86400)]:
            if call(feed, "description()", ["string"])[0] != description or call(feed, "decimals()", ["uint8"])[0] != 8:
                raise MachineError("PRICE_FEED_IDENTITY_MISMATCH")
            round_id, answer, started, updated, answered = call(feed, "latestRoundData()", ROUND_TYPES)
            if answer <= 0 or not 0 < started <= updated <= now or now - updated > max_age or answered < round_id:
                raise MachineError("PRICE_FEED_STALE_OR_INVALID")
            values.append(answer)
        fair = amount * 10**12 * values[1] // values[0]
        # Includes every route fee and slippage: no more than 5% below the anchor.
        if minimum * 10000 < fair * 9500:
            raise MachineError("QUOTE_BELOW_INDEPENDENT_PRICE_FLOOR")
        observations.append({"rpc": source_label(index), "block": block["number"], "eth_usd_8": str(values[0]),
                             "usdc_usd_8": str(values[1]), "fair_eth_wei": str(fair),
                             "block_timestamp": int(block["timestamp"], 16), "checked_at": int(clock())})
    finished = int(clock())
    if any(not 0 <= finished - x["block_timestamp"] <= 120 for x in observations):
        raise MachineError("FRESH_CHAIN_STATE_REQUIRED")
    high, low = max(int(x["fair_eth_wei"]) for x in observations), min(int(x["fair_eth_wei"]) for x in observations)
    if high - low > low // 200:
        raise MachineError("PRICE_RPC_DISAGREEMENT")
    return {"source": "CHAINLINK_ARBITRUM", "max_shortfall_bps": 500, "observations": observations}
