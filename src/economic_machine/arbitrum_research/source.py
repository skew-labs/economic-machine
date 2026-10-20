"""Bounded, unfiltered Arbitrum One public Portal source.

The provider's finalized label is provenance, not independently checked L1
finality. A response proves only that the selected rows were returned. It does
not prove that the provider has no missing transactions, logs, or call traces.
The durable caller owns retries, shared budgets, storage and cross-page linkage.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

DATASET = "arbitrum-one"
CHAIN_ID = 42161
BASE_URL = "https://portal.sqd.dev/datasets/arbitrum-one"
NITRO_FIRST_EXECUTION_BLOCK = 22_207_818
MAX_BLOCKS = 10_000
MAX_RESPONSE = 32 * 1024 * 1024
MAX_HEAD_RESPONSE = 64 * 1024
RETRY_STATUSES = frozenset((408, 429, 500, 502, 503, 504, 529))
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")

# No address, protocol, transaction-type, topic or success-status filter.
FIELDS = {
    "block": dict.fromkeys(("number", "hash", "parentHash", "timestamp",
        "stateRoot", "transactionsRoot", "receiptsRoot", "gasUsed", "gasLimit",
        "baseFeePerGas", "miner", "extraData"), True),
    "transaction": dict.fromkeys(("transactionIndex", "hash", "from", "to",
        "value", "input", "nonce", "status", "type", "gas", "gasUsed",
        "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "effectiveGasPrice",
        "cumulativeGasUsed", "contractAddress", "chainId"), True),
    "log": dict.fromkeys(("address", "topics", "data", "logIndex",
        "transactionIndex", "transactionHash"), True),
    "trace": dict.fromkeys(("type", "transactionIndex", "traceAddress",
        "subtraces", "error", "revertReason", "callFrom", "callTo", "callValue",
        "callCallType", "callInput", "callGas", "callResultGasUsed",
        "callResultOutput", "createFrom", "createValue", "createInit",
        "createGas", "createResultAddress", "createResultCode",
        "createResultGasUsed", "suicideAddress", "suicideRefundAddress",
        "suicideBalance", "rewardAuthor", "rewardValue", "rewardType"), True),
}


class SourceError(RuntimeError):
    pass


class SourceIntegrityError(SourceError):
    pass


class SourceLimit(SourceError):
    """Caller must shrink its range or explicitly admit a larger byte bound."""


class SourceHTTPError(SourceError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"SOURCE_HTTP_{status}")


class SourceBackoff(SourceError):
    def __init__(self, retry_after_seconds: float, status: int | None = None):
        self.retry_after_seconds = retry_after_seconds
        self.status = status
        super().__init__(f"SOURCE_BACKOFF_{status or 'NETWORK'}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceHTTPError(code)


def retry_after(value: str | None, *, now: float | None = None) -> float:
    """Respect a provider's entire Retry-After interval, including HTTP dates."""
    now = time.time() if now is None else now
    try:
        delay = float(value)
        if not math.isfinite(delay):
            raise ValueError("nonfinite")
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError, AttributeError):
            delay = 60.0
    return max(1.0, delay)


def query(start: int, end: int) -> dict:
    if type(start) is not int or type(end) is not int or not 0 <= start <= end < 2**53:
        raise ValueError("SOURCE_BLOCK_RANGE")
    if end - start + 1 > MAX_BLOCKS:
        raise SourceLimit("SOURCE_BLOCK_WINDOW")
    return {"type": "evm", "fromBlock": start, "toBlock": end,
            "includeAllBlocks": True, "fields": copy.deepcopy(FIELDS),
            "transactions": [{}], "logs": [{}], "traces": [{}]}


def _json(raw: bytes):
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise SourceIntegrityError("SOURCE_DUPLICATE_JSON_KEY")
            out[k] = v
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise SourceIntegrityError("SOURCE_JSON") from exc


def parse_page(raw: bytes, start: int, end: int, *, truncated: bool = False) -> dict:
    """Validate a contiguous prefix; never turn absence into an empty block.

    On a byte cap only complete NDJSON records survive. Truncated JSON arrays
    and a first record too large to fit are not committed. A clean, shorter
    provider batch advances only to the last actual returned header plus one.
    """
    query(start, end)  # Also validates callers of this pure parser.
    if not isinstance(raw, bytes):
        raise TypeError("SOURCE_RAW_BYTES")
    if not raw.strip():
        raise SourceBackoff(60.0, 204)
    if raw.lstrip().startswith(b"["):
        if truncated:
            raise SourceLimit("SOURCE_ARRAY_RESPONSE_TOO_LARGE")
        blocks = _json(raw)
        if not isinstance(blocks, list):
            raise SourceIntegrityError("SOURCE_BLOCK_ARRAY")
    else:
        if truncated:
            pos = raw.rfind(b"\n")
            if pos < 0:
                raise SourceLimit("SOURCE_SINGLE_BLOCK_TOO_LARGE")
            raw = raw[:pos + 1]
        blocks = [_json(line) for line in raw.splitlines() if line.strip()]
    if not blocks:
        raise SourceBackoff(60.0, 204)
    if len(blocks) > end - start + 1:
        raise SourceIntegrityError("SOURCE_EXTRA_BLOCKS")
    previous = None
    missing_receipt_status = missing_trace_transactions = 0
    trace_rows = tx_rows = log_rows = classic_blocks = 0
    for offset, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("header"), dict):
            raise SourceIntegrityError("SOURCE_BLOCK_SHAPE")
        h = block["header"]
        if type(h.get("number")) is not int or h["number"] != start + offset:
            raise SourceIntegrityError("SOURCE_BLOCK_GAP_OR_ORDER")
        if any(not isinstance(h.get(k), str) or not _HASH.fullmatch(h[k])
               for k in ("hash", "parentHash")):
            raise SourceIntegrityError("SOURCE_BLOCK_HASH")
        if previous is not None and h["parentHash"].lower() != previous.lower():
            raise SourceIntegrityError("SOURCE_PARENT_LINK_MISMATCH")
        previous = h["hash"]
        classic_blocks += int(h["number"] < NITRO_FIRST_EXECUTION_BLOCK)
        for name in ("transactions", "logs", "traces"):
            # Portal omits empty tables. Preserve raw bytes separately.
            items = block.setdefault(name, [])
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise SourceIntegrityError("SOURCE_TABLE_SHAPE")
        transactions = block["transactions"]
        for index, tx in enumerate(transactions):
            if type(tx.get("transactionIndex")) is not int or tx["transactionIndex"] != index:
                raise SourceIntegrityError("SOURCE_TRANSACTION_INDEX_GAP")
            if not isinstance(tx.get("hash"), str) or not _HASH.fullmatch(tx["hash"]):
                raise SourceIntegrityError("SOURCE_TRANSACTION_HASH")
            status = tx.get("status")
            if status is None:
                missing_receipt_status += 1
            elif type(status) is not int or status not in (0, 1):
                raise SourceIntegrityError("SOURCE_TRANSACTION_STATUS")
        seen_logs = set()
        for log_offset, log in enumerate(block["logs"]):
            idx, ti = log.get("logIndex"), log.get("transactionIndex")
            if type(idx) is not int or idx != log_offset or idx in seen_logs:
                raise SourceIntegrityError("SOURCE_LOG_INDEX")
            if type(ti) is not int or not 0 <= ti < len(transactions):
                raise SourceIntegrityError("SOURCE_LOG_TRANSACTION")
            if log.get("transactionHash", "").lower() != transactions[ti]["hash"].lower():
                raise SourceIntegrityError("SOURCE_LOG_TRANSACTION_HASH")
            seen_logs.add(idx)
        trace_keys, traced_transactions = set(), set()
        for trace in block["traces"]:
            ti, address = trace.get("transactionIndex"), trace.get("traceAddress")
            if type(ti) is not int or not 0 <= ti < len(transactions):
                # Block rewards can be outside a transaction and must remain
                # observable, without being attached to another tx by guessing.
                if trace.get("type") != "reward" or ti is not None:
                    raise SourceIntegrityError("SOURCE_TRACE_TRANSACTION")
            else:
                traced_transactions.add(ti)
            if not isinstance(address, list) or any(type(x) is not int or x < 0 for x in address):
                raise SourceIntegrityError("SOURCE_TRACE_ADDRESS")
            key = (ti, tuple(address))
            if key in trace_keys:
                raise SourceIntegrityError("SOURCE_DUPLICATE_TRACE")
            trace_keys.add(key)
        missing_trace_transactions += len(transactions) - len(traced_transactions)
        tx_rows += len(transactions)
        log_rows += len(block["logs"])
        trace_rows += len(block["traces"])
    last = blocks[-1]["header"]["number"]
    return {"raw": raw, "blocks": blocks, "first_block": start, "last_block": last,
            "next_block": last + 1, "requested_start": start, "requested_end": end,
            "range_complete": last == end, "response_truncated": truncated,
            "sha256": hashlib.sha256(raw).hexdigest(), "dataset": DATASET,
            "chain_id": CHAIN_ID, "provenance": "SQD_PROVIDER_FINALIZED_STREAM",
            "independent_canonical_verification": False,
            "coverage": {"contiguous_returned_headers": True,
                "all_protocols_requested": True, "table_completeness_verified": False,
                "transactions": tx_rows, "logs": log_rows, "traces": trace_rows,
                "transactions_missing_receipt_status": missing_receipt_status,
                "transactions_without_trace_rows": missing_trace_transactions,
                "classic_blocks": classic_blocks,
                "classic_trace_completeness": "UNVERIFIED",
                "state_diffs": "NOT_AVAILABLE_IN_DATASET"}}


class PublicSource:
    """Public no-key adapter. It never charges, signs, sleeps or auto-retries.

    ``fetch(start, end)`` returns parse_page's dictionary plus safe response
    metadata. Caller archives ``raw`` before advancing ``next_block``. The
    caller must compare first.parentHash with its previously committed block.
    Persist SourceBackoff.retry_after_seconds across process restarts.
    """
    def __init__(self, *, max_response: int = MAX_RESPONSE, timeout: float = 45,
                 opener=None,compressed=None):
        if type(max_response) is not int or not 1024 <= max_response <= 128 * 1024 * 1024:
            raise ValueError("SOURCE_RESPONSE_BOUND")
        if not 1 <= timeout <= 120:
            raise ValueError("SOURCE_TIMEOUT")
        self.max_response, self.timeout = max_response, timeout
        self.compressed=os.environ.get('EM_HISTORY_GZIP')=='1' if compressed is None else compressed
        if type(self.compressed) is not bool:raise ValueError('SOURCE_COMPRESSION_FLAG')
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, path: str, payload: dict | None, limit: int):
        req = urllib.request.Request(BASE_URL + path,
            data=None if payload is None else json.dumps(payload, separators=(",", ":")).encode(),
            headers={"User-Agent": "economic-machine-research/1.0",
                     "Accept": "application/x-ndjson, application/json",
                     "Accept-Encoding": "gzip" if self.compressed else "identity", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                if response.status == 204:
                    raise SourceBackoff(60.0, 204)
                if response.status != 200:
                    raise SourceHTTPError(response.status)
                if not self.compressed and response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise SourceIntegrityError("SOURCE_UNEXPECTED_CONTENT_ENCODING")
                from .transport import read_body
                raw,truncated,transport=read_body(response,limit)
                return raw,truncated, {
                    **transport,
                    "http_status": response.status,
                    "content_type": response.headers.get("Content-Type", ""),
                    "data_source": response.headers.get("x-sqd-data-source", "unknown"),
                }
        except urllib.error.HTTPError as exc:
            delay = retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
            if exc.code in RETRY_STATUSES:
                raise SourceBackoff(delay, exc.code) from None
            raise SourceHTTPError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            raise SourceBackoff(60.0) from exc

    def metadata(self) -> dict:
        raw, truncated, _ = self._request("/metadata", None, MAX_HEAD_RESPONSE)
        if truncated:
            raise SourceLimit("SOURCE_METADATA_BOUND")
        meta = _json(raw)
        if not isinstance(meta, dict) or meta.get("dataset") != DATASET or type(meta.get("start_block")) is not int:
            raise SourceIntegrityError("SOURCE_METADATA_SCHEMA")
        if meta["start_block"] != 0:
            raise SourceIntegrityError("SOURCE_GENESIS_NOT_AVAILABLE")
        return meta

    def head(self) -> int:
        raw, truncated, _ = self._request("/finalized-head", None, MAX_HEAD_RESPONSE)
        if truncated:
            raise SourceLimit("SOURCE_HEAD_BOUND")
        value = _json(raw)
        if (not isinstance(value, dict) or type(value.get("number")) is not int
                or not 0 <= value["number"] < 2**53
                or not isinstance(value.get("hash"), str) or not _HASH.fullmatch(value["hash"])):
            raise SourceIntegrityError("SOURCE_HEAD_SCHEMA")
        return value["number"]

    def fetch(self, start: int, end: int) -> dict:
        body = query(start, end)
        raw, truncated, metadata = self._request("/finalized-stream", body, self.max_response)
        page = parse_page(raw, start, end, truncated=truncated)
        page["response"] = metadata
        page["query_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        return page
