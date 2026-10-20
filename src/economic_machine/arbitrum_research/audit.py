"""Small official-RPC corroboration samples and explicit recovery candidates.

No wallet methods, credentials, retries, graph writes or consensus assertions.
The caller archives ``raw`` before recording audit results and persists any
reported Retry-After cooldown. Call traces are candidates, never money edges.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.request

from .source import CHAIN_ID, retry_after

RPC_URL = "https://arb1.arbitrum.io/rpc"
MAX_RESPONSE = 2 * 1024 * 1024
ALLOWED_METHODS = frozenset(("eth_chainId", "eth_getBlockByNumber",
                            "eth_getTransactionReceipt", "debug_traceTransaction"))
_HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_RETRY = frozenset((408, 429, 500, 502, 503, 504, 529))


def _integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return int(value, 16)
    return None


def _same_hash(a, b):
    return (isinstance(a, str) and isinstance(b, str)
            and _HASH.fullmatch(a) is not None and _HASH.fullmatch(b) is not None
            and a.lower() == b.lower())


def _parse_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AuditClient:
    def __init__(self, *, opener=None, timeout=20, max_response=MAX_RESPONSE):
        if not 1 <= timeout <= 45:
            raise ValueError("AUDIT_TIMEOUT")
        if type(max_response) is not int or not 1024 <= max_response <= 8 * 1024 * 1024:
            raise ValueError("AUDIT_RESPONSE_BOUND")
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.timeout, self.max_response = timeout, max_response
        self.records = []
        self.cooldown = 0.0

    def _rpc(self, method, params):
        if method not in ALLOWED_METHODS or len(self.records) >= 7 or self.cooldown:
            raise ValueError("AUDIT_REQUEST_NOT_ADMITTED")
        request_id = len(self.records) + 1
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        request = urllib.request.Request(RPC_URL, data=json.dumps(body).encode(), headers={
            "User-Agent": "economic-machine-research/1.0", "Content-Type": "application/json",
            "Accept": "application/json", "Accept-Encoding": "identity"})
        record = {"request": body, "at_ms": time.time_ns() // 1_000_000,
                  "outcome": "UNAVAILABLE", "raw_response_b64": ""}
        raw = b""
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                record["http_status"] = response.status
                if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    record["reason"] = "UNEXPECTED_CONTENT_ENCODING"
                else:
                    raw = response.read(self.max_response + 1)
                    if len(raw) > self.max_response:
                        raw = raw[:self.max_response]
                        record["reason"] = "RESPONSE_BOUND_EXCEEDED"
                        record["raw_response_truncated"] = True
                    elif response.status != 200:
                        record["reason"] = "HTTP_STATUS"
                        if response.status in _RETRY:
                            self.cooldown = retry_after(response.headers.get("Retry-After"))
                    else:
                        try:
                            value = _parse_json(raw)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            record["reason"] = "INVALID_JSON"
                        else:
                            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or type(value.get("id")) is not int or value["id"] != request_id:
                                record["reason"] = "RPC_ENVELOPE_MISMATCH"
                            elif "error" in value:
                                record["reason"] = "RPC_ERROR"
                                record["rpc_error"] = value["error"]
                            elif value.get("result") is None:
                                record["reason"] = "NULL_RESULT"
                            else:
                                record["outcome"] = "CANDIDATE"
                                record["result"] = value["result"]
        except urllib.error.HTTPError as exc:
            record["http_status"] = exc.code
            record["reason"] = "HTTP_STATUS"
            if exc.code in _RETRY:
                self.cooldown = retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            try:
                raw = exc.read(min(self.max_response, 4096) + 1)
                if len(raw) > min(self.max_response, 4096):
                    raw = raw[:-1]
                    record["raw_response_truncated"] = True
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            record["reason"] = "NETWORK_UNAVAILABLE"
            record["error_type"] = type(exc).__name__
            self.cooldown = 60.0
        record["raw_response_b64"] = base64.b64encode(raw).decode()
        record["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
        record["raw_response_bytes"] = len(raw)
        if self.cooldown:
            record["retry_after_seconds"] = self.cooldown
        self.records.append(record)
        return record

    def audit_page(self, page, max_transactions=2, probe_traces=True):
        if type(max_transactions) is not int or not 0 <= max_transactions <= 2:
            raise ValueError("AUDIT_TRANSACTION_BOUND")
        if type(probe_traces) is not bool:
            raise ValueError("AUDIT_TRACE_PROBE_FLAG")
        if self.records:
            raise ValueError("AUDIT_CLIENT_ALREADY_USED")
        blocks = page.get("blocks") if isinstance(page, dict) else None
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("AUDIT_PAGE_BLOCKS")
        source_sha = page.get("sha256")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            raise ValueError("AUDIT_SOURCE_SHA256")
        samples = [blocks[0]]
        if blocks[-1]["header"]["number"] != blocks[0]["header"]["number"]:
            samples.append(blocks[-1])
        gap_candidates = []
        for block in blocks:
            traced = {trace.get("transactionIndex") for trace in block.get("traces", [])}
            for tx in block.get("transactions", []):
                missing = []
                if tx.get("status") is None:
                    missing.append("RECEIPT_STATUS")
                if tx["transactionIndex"] not in traced:
                    missing.append("TRACE_ROWS")
                if missing:
                    gap_candidates.append((block, tx, missing))
        selected = gap_candidates[:max_transactions]
        result = {"schema": "arbitrum-rpc-audit-v1", "at_ms": time.time_ns() // 1_000_000,
                  "source_sha256": source_sha, "provider": "OFFICIAL_PUBLIC_ARBITRUM_RPC",
                  "chain_id": CHAIN_ID, "independent_consensus_verification": False,
                  "graph_apply_allowed": False, "records": self.records,
                  "block_checks": [], "transaction_checks": [], "gaps": [],
                  "original_gaps": [{"block": b["header"]["number"], "transaction_hash": t["hash"],
                      "missing": missing} for b, t, missing in gap_candidates],
                  "unattempted_gap_count": len(gap_candidates) - len(selected),
                  "sampled_only": True, "unsampled_history_verified": False}
        if result["unattempted_gap_count"]:
            result["gaps"].append({"kind": "UNATTEMPTED_TRANSACTION_GAPS",
                                  "count": result["unattempted_gap_count"],
                                  "original_gap_resolved": False})
        identity = self._rpc("eth_chainId", [])
        if identity["outcome"] == "UNAVAILABLE" or _integer(identity.get("result")) != CHAIN_ID:
            result["outcome"] = "UNAVAILABLE" if identity["outcome"] == "UNAVAILABLE" else "MISMATCH"
            result["gaps"].append({"kind": "CHAIN_ID_UNCONFIRMED", "expected": CHAIN_ID})
            return self._finish(result)
        identity["outcome"] = "CORROBORATED"
        for block in samples:
            number = block["header"]["number"]
            check = {"block": number, "outcome": "UNAVAILABLE"}
            if not self.cooldown:
                record = self._rpc("eth_getBlockByNumber", [hex(number), False])
                value = record.get("result")
                if isinstance(value, dict):
                    checks = {
                        "number_matches": _integer(value.get("number")) == number,
                        "hash_matches": _same_hash(value.get("hash"), block["header"]["hash"]),
                        "parent_hash_matches": _same_hash(value.get("parentHash"), block["header"]["parentHash"]),
                        "transaction_hashes_match": value.get("transactions") == [t["hash"] for t in block.get("transactions", [])],
                    }
                    check.update(checks)
                    check["outcome"] = "CORROBORATED" if all(checks.values()) else "MISMATCH"
                    record["outcome"] = check["outcome"]
            result["block_checks"].append(check)
            if check["outcome"] != "CORROBORATED":
                result["gaps"].append({"kind": "BLOCK_CORROBORATION_" + check["outcome"], "block": number})
        for block, tx, missing in selected:
            number = block["header"]["number"]
            check = {"block": number, "transaction_hash": tx["hash"],
                     "original_gaps": missing, "receipt_outcome": "UNAVAILABLE",
                     "receipt_corroboration_scope": "IDENTITY_AND_STATUS_ONLY",
                     "trace_outcome": "NOT_REQUESTED", "graph_apply_allowed": False}
            if not self.cooldown:
                receipt = self._rpc("eth_getTransactionReceipt", [tx["hash"]])
                value = receipt.get("result")
                if isinstance(value, dict):
                    checks = {
                        "transaction_hash_matches": _same_hash(value.get("transactionHash"), tx["hash"]),
                        "block_hash_matches": _same_hash(value.get("blockHash"), block["header"]["hash"]),
                        "block_number_matches": _integer(value.get("blockNumber")) == number,
                        "transaction_index_matches": _integer(value.get("transactionIndex")) == tx["transactionIndex"],
                    }
                    observed_status = _integer(value.get("status"))
                    check["receipt_identity_checks"] = checks
                    check["observed_receipt_status"] = observed_status
                    if not all(checks.values()) or (tx.get("status") is not None and tx["status"] != observed_status):
                        check["receipt_outcome"] = "MISMATCH"
                    elif observed_status not in (0, 1):
                        check["receipt_outcome"] = "UNAVAILABLE"
                    elif tx.get("status") is None:
                        check["receipt_outcome"] = "CANDIDATE"
                    else:
                        check["receipt_outcome"] = "CORROBORATED"
                    receipt["outcome"] = check["receipt_outcome"]
            if "TRACE_ROWS" in missing:
                check["trace_outcome"] = "UNAVAILABLE"
                if not probe_traces:
                    check["trace_reason"] = "RECENT_METHOD_UNAVAILABLE_CAPABILITY"
                if probe_traces and not self.cooldown and check["receipt_outcome"] in ("CORROBORATED", "CANDIDATE"):
                    trace = self._rpc("debug_traceTransaction", [tx["hash"],
                                      {"tracer": "callTracer", "timeout": "5s"}])
                    value = trace.get("result")
                    if isinstance(value, dict) and isinstance(value.get("type"), str):
                        check["trace_outcome"] = "CANDIDATE"
                        check["trace_kind"] = "UNVERIFIED_RPC_CALL_TRACE"
                        trace["outcome"] = "CANDIDATE"
                    else:
                        check["trace_reason"] = trace.get("reason", "TRACE_SCHEMA_UNSUPPORTED")
                result["gaps"].append({"kind": "TRACE_RECOVERY_" + check["trace_outcome"],
                    "block": number, "transaction_hash": tx["hash"],
                    "original_gap_resolved": False, "graph_apply_allowed": False})
            if "RECEIPT_STATUS" in missing or check["receipt_outcome"] in ("UNAVAILABLE", "MISMATCH"):
                result["gaps"].append({"kind": "RECEIPT_RECOVERY_" + check["receipt_outcome"],
                    "block": number, "transaction_hash": tx["hash"], "original_gap_resolved": False})
            result["transaction_checks"].append(check)
        outcomes = [x["outcome"] for x in result["block_checks"]]
        outcomes += [x["receipt_outcome"] for x in result["transaction_checks"]]
        if "MISMATCH" in outcomes:
            result["outcome"] = "MISMATCH"
        elif result["gaps"]:
            result["outcome"] = "CANDIDATE" if any(x.get("trace_outcome") == "CANDIDATE" or x["receipt_outcome"] == "CANDIDATE" for x in result["transaction_checks"]) else "UNAVAILABLE"
        else:
            result["outcome"] = "CORROBORATED"
        return self._finish(result)

    def _finish(self, result):
        result["request_count"] = len(self.records)
        result["retry_after_seconds"] = self.cooldown
        result["all_original_gaps_resolved"] = False
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        result["raw"] = raw
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        return result


def audit_page(page, max_transactions=2, probe_traces=True):
    return AuditClient().audit_page(page, max_transactions=max_transactions,probe_traces=probe_traces)
