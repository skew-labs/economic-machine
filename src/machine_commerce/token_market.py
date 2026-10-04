"""Read-only, address-pinned SKEW market observations. Never a trading quote."""

import asyncio
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from .token_supply import TokenSupply

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
DEX = "https://api.dexscreener.com/latest/dex/pairs/arbitrum/"
GECKO = "https://api.geckoterminal.com/api/v2/networks/arbitrum/pools/"
MANIFEST = Path(__file__).resolve().parents[2] / "contracts/deployments/arbitrum-one.json"


def deployment():
    """Packaged, independently reconciled deployment; not a live supply feed."""
    try:
        data = json.loads(MANIFEST.read_text())
        if (data["chain_id"] != 42161 or data["status"] != "MAINNET_DEPLOYED_FINALIZED"
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", data["transaction_hash"])
                or not all(ADDRESS.fullmatch(data["contracts"][name])
                           and int(data["contracts"][name], 16) != 0
                           for name in ["SkewLaunchBundle", "SkewDataPass", "SkewArtifactMining", "SkewSolutionToken"])):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > Decimal("1e30") or (positive and n == 0):
            return None
        return str(n)
    except (InvalidOperation, ValueError):
        return None


def identity():
    published = deployment()
    token = os.environ.get("MACHINE_SKEW_TOKEN_ADDRESS", published["contracts"]["SkewSolutionToken"] if published else "")
    pool = os.environ.get("MACHINE_SKEW_POOL_ADDRESS", "")
    valid = lambda x: bool(ADDRESS.fullmatch(x)) and int(x, 16) != 0
    return (token.lower() if valid(token) else None, pool.lower() if valid(pool) else None)


def empty(status, token=None, pool=None):
    published = deployment()
    if not token or not published or token.lower() != published["contracts"]["SkewSolutionToken"].lower():
        published = None
    return {"symbol": "SKEW", "name": "Skew Solution", "chain_id": 42161,
            "deployment": published,
            "status": status, "token_address": token, "pool_address": pool,
            "price_usd": None, "volume_24h_usd": None, "market_cap_usd": None,
            "liquidity_usd": None, "observed_at": None, "candles": [],
            "history_status": "UNAVAILABLE", "source": None,
            "methodology": "One configured DEX pool. Market cap is provider-reported; FDV is not substituted.",
            "protocol": {"maximum_supply": "160000", "reward_per_accepted_job": "1",
                         "premine": False, "scope": "CONTRACT_DESIGN_NOT_CIRCULATING_SUPPLY"}}


def normalize_pair(payload, token, pool, now):
    pairs = payload.get("pairs")
    if not isinstance(pairs, list):
        raise ValueError("Missing pairs")
    pair = next((p for p in pairs if isinstance(p, dict)
                 and p.get("chainId") == "arbitrum"
                 and str(p.get("pairAddress", "")).lower() == pool
                 and str((p.get("baseToken") or {}).get("address", "")).lower() == token), None)
    if pair is None:
        raise ValueError("Configured SKEW must be the base token of this exact pool")
    result = empty("AVAILABLE", token, pool)
    price = number(pair.get("priceUsd"), positive=True)
    if price is None:
        raise ValueError("No observed price")
    result.update(price_usd=price,
                  volume_24h_usd=number((pair.get("volume") or {}).get("h24")),
                  market_cap_usd=number(pair.get("marketCap"), positive=True),
                  liquidity_usd=number((pair.get("liquidity") or {}).get("usd")),
                  observed_at=int(now), source="DEX Screener",
                  source_url="https://dexscreener.com/arbitrum/" + pool)
    return result


def normalize_history(payload, now):
    rows = payload["data"]["attributes"]["ohlcv_list"]
    if not isinstance(rows, list) or len(rows) > 169:
        raise ValueError("Invalid history")
    points = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("Invalid candle")
        timestamp = row[0]
        if type(timestamp) is not int or timestamp <= 0 or timestamp > now:
            raise ValueError("Invalid timestamp")
        close = number(row[4], positive=True)
        if close is None or timestamp in points:
            raise ValueError("Invalid close")
        if timestamp >= now - 7 * 86400:
            points[timestamp] = {"timestamp": timestamp, "price_usd": close,
                                 "volume_usd": number(row[5])}
    return [points[key] for key in sorted(points)]


class TokenMarket:
    """Single-flight, 60-second cache; errors never masquerade as a zero price."""

    def __init__(self, fetch=None, clock=time.time, supply=None):
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.lock = asyncio.Lock()
        self.cached = None
        self.cache_key = None
        self.expires = 0
        self.supply = supply or TokenSupply(clock=clock)

    @staticmethod
    async def _fetch(url):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256000:
                        raise ValueError("Oversize provider response")
                import json
                return json.loads(body)

    async def snapshot(self):
        result = dict(await self.market_snapshot())
        result["supply"] = await self.supply.snapshot(result.get("deployment"))
        return result

    async def market_snapshot(self):
        token, pool = identity()
        if not token:
            return empty("AWAITING_TOKEN_ADDRESS")
        if not pool:
            return empty("AWAITING_MARKET", token)
        key = token, pool
        async with self.lock:
            now = self.clock()
            if self.cached is not None and self.cache_key == key and now < self.expires:
                return self.cached
            try:
                result = normalize_pair(await self.fetch(DEX + pool), token, pool, now)
            except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                result = empty("SOURCE_UNAVAILABLE", token, pool)
            if result["status"] == "AVAILABLE":
                try:
                    url = GECKO + pool + "/ohlcv/hour?aggregate=1&limit=168&currency=usd&token=base&include_empty_intervals=false"
                    result["candles"] = normalize_history(await self.fetch(url), now)
                    result["history_status"] = "AVAILABLE" if result["candles"] else "NO_TRADES"
                    result["history_source"] = "GeckoTerminal"
                except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                    pass  # A chart outage does not invent or invalidate a separate spot observation.
            self.cached, self.cache_key, self.expires = result, key, self.clock() + 60
            return result
