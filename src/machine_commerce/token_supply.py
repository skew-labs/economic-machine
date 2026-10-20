"""Read-only SKEW issuance at a common block on two independent RPC hosts."""
import asyncio
import hashlib
import re
import time
from decimal import Decimal

import httpx
from eth_utils import keccak

from .fuel_rpc import configured_rpcs

READS = {"eth_chainId", "eth_getBlockByNumber", "eth_call", "eth_getCode"}


def uint(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
        raise ValueError("Invalid RPC integer")
    return int(value, 16)


async def read_rpc(url, method, params):
    if method not in READS:
        raise ValueError("Read-only method required")
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        async with client.stream("POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 64000:
                    raise ValueError("RPC response too large")
            import json
            data = json.loads(body)
    if data.get("id") != 1 or "error" in data or "result" not in data:
        raise ValueError("RPC read failed")
    return data["result"]


class TokenSupply:
    def __init__(self, rpc=read_rpc, clock=time.time, endpoints=None):
        self.rpc, self.clock = rpc, clock
        self.endpoints = endpoints
        self.lock = asyncio.Lock()
        self.cached = None
        self.key = None
        self.expires = 0

    async def snapshot(self, publication):
        unavailable = {"status": "UNAVAILABLE", "total_supply": None, "maximum_supply": None,
                       "issued_percent": None, "observed_at": None}
        if publication is None:
            return unavailable
        token = publication["contracts"]["SkewSolutionToken"]
        async with self.lock:
            now = self.clock()
            if self.cached is not None and self.key == token and now < self.expires:
                return self.cached
            try:
                endpoints = self.endpoints or configured_rpcs()
                async def tip(url):
                    chain, block = await asyncio.gather(self.rpc(url, "eth_chainId", []),
                        self.rpc(url, "eth_getBlockByNumber", ["latest", False]))
                    if uint(chain) != 42161:
                        raise ValueError("Wrong chain")
                    return uint(block["number"])
                tips = await asyncio.gather(*(tip(url) for url in endpoints))
                height = min(tips)
                async def observe(url):
                    block = await self.rpc(url, "eth_getBlockByNumber", [hex(height), False])
                    block_hash = block["hash"]
                    timestamp = uint(block["timestamp"])
                    if (uint(block["number"]) != height or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash)
                            or not now - 180 <= timestamp <= now + 30):
                        raise ValueError("Stale or invalid block")
                    async def call(signature):
                        selector = "0x" + keccak(text=signature)[:4].hex()
                        return uint(await self.rpc(url, "eth_call", [{"to": token, "data": selector}, hex(height)]))
                    supply, cap, decimals, code = await asyncio.gather(call("totalSupply()"), call("cap()"), call("decimals()"),
                        self.rpc(url, "eth_getCode", [token, hex(height)]))
                    if (decimals != 18 or cap != 160000 * 10**18 or supply > cap
                            or hashlib.sha256(bytes.fromhex(code.removeprefix("0x"))).hexdigest()
                            != publication["runtime_sha256"]["SkewSolutionToken"]):
                        raise ValueError("Contract mismatch")
                    after = await self.rpc(url, "eth_getBlockByNumber", [hex(height), False])
                    if after["hash"] != block_hash:
                        raise ValueError("Block changed")
                    return supply, cap, block_hash, timestamp
                rows = await asyncio.gather(*(observe(url) for url in endpoints))
                if len(rows) != 2 or rows[0] != rows[1]:
                    raise ValueError("RPC disagreement")
                supply, cap, block_hash, timestamp = rows[0]
                result = {"status": "AVAILABLE", "total_supply": str(Decimal(supply) / Decimal(10**18)),
                          "maximum_supply": "160000", "issued_percent": str(Decimal(supply) * 100 / Decimal(cap)),
                          "observed_at": int(now), "block_number": height, "block_hash": block_hash,
                          "block_timestamp": timestamp, "confirmation": "LATEST_COMMON_BLOCK_NOT_FINALIZED",
                          "method": "Two independent RPC hosts; matching block, code and ERC-20 state"}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                result = unavailable
            self.cached, self.key, self.expires = result, token, self.clock() + 60
            return result
