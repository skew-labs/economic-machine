"""Allowlisted service workers and objective artifact checks.

CSV validation proves transformation fidelity, not business truth. RPC checks
prove consistency with the configured source, not an independent consensus.
"""

import csv
import io
import re

import httpx

from economic_machine.values import MachineError, canonical, decimal, decstr, digest, require_keys

RPC_URL = "https://sepolia-rollup.arbitrum.io/rpc"


def csv_rows(request):
    reader = csv.reader(io.StringIO(request["csv"], newline=""), strict=True)
    try:
        header = next(reader)
        if header != request["columns"]:
            raise MachineError("CSV header must exactly match requested columns")
        result = []
        for row in reader:
            if len(row) != len(header) or len(result) >= 2000:
                raise MachineError("CSV row width or row limit violated")
            item = dict(zip(header, row))
            for column in request["numeric_columns"]:
                item[column] = decstr(decimal(item[column], signed=True))
            result.append(item)
        if not result:
            raise MachineError("CSV has no data rows")
        return result
    except (csv.Error, StopIteration) as exc:
        raise MachineError("CSV cannot be parsed") from exc


def rpc(method, params):
    with (httpx.Client(timeout=6, follow_redirects=False, trust_env=False) as client,
          client.stream("POST", RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                "method": method, "params": params}) as response):
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 200_000:
                raise MachineError("RPC response exceeds size limit")
            chunks.append(chunk)
    import json
    body = json.loads(b"".join(chunks))
    if body.get("jsonrpc") != "2.0" or body.get("id") != 1 or "error" in body or "result" not in body:
        raise MachineError("RPC result unavailable")
    return body["result"]


def hex_integer(value):
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value) is None:
        raise MachineError("invalid RPC integer")
    return int(value, 16)


def block_hash(value):
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]{64}", value) is None:
        raise MachineError("invalid block hash")
    return value.lower()


class Workers:
    def __init__(self, clock, rpc_call=rpc):
        self.clock, self.rpc = clock, rpc_call

    def fulfill(self, offer_id, request):
        if offer_id == "apac-compute-brief":
            from .datapass import DataProducts
            from .atlas import load_report
            release = load_report(DataProducts().path)
            if release["report_sha256"] != request["report_sha256"] or not 0 <= self.clock() - release["derived"]["as_of"] <= request["max_age_seconds"]:
                raise MachineError("ATLAS_VERSION_UNAVAILABLE_OR_STALE")
            return {"schema_version": "atlas-delivery-1", "report": release["derived"],
                    "content_sha256": release["report_sha256"], "terms_sha256": release["terms_sha256"],
                    "input_sha256": digest(request), "generated_at": self.clock()}
        if offer_id == "csv-normalize":
            rows = csv_rows(request)
            return {"schema_version": "normalized-csv-1", "columns": request["columns"],
                "numeric_columns": request["numeric_columns"], "rows": rows, "row_count": len(rows),
                "input_sha256": digest(request), "generated_at": self.clock()}
        if offer_id == "arbitrum-state":
            chain_id = hex_integer(self.rpc("eth_chainId", []))
            if chain_id != request["chain_id"]:
                raise MachineError("RPC chain differs from request")
            block = self.rpc("eth_getBlockByNumber", ["latest", False])
            if not isinstance(block, dict):
                raise MachineError("RPC block unavailable")
            return {"schema_version": "arbitrum-state-1", "chain_id": chain_id,
                "block_number": hex_integer(block.get("number")),
                "block_hash": block_hash(block.get("hash")),
                "block_timestamp": hex_integer(block.get("timestamp")),
                "gas_price_wei": str(hex_integer(self.rpc("eth_gasPrice", []))),
                "source": RPC_URL, "finality": "LATEST_L2_NOT_L1_FINALIZED",
                "input_sha256": digest(request), "generated_at": self.clock()}
        raise MachineError("worker does not support service")

    def verify(self, offer_id, request, artifact):
        checks, reasons = [], []
        try:
            if offer_id == "apac-compute-brief":
                require_keys(artifact, {"schema_version", "report", "content_sha256", "terms_sha256", "input_sha256", "generated_at"}, "Atlas delivery")
                from .atlas import TERMS, build_report, load_report
                from .datapass import DataProducts
                release = load_report(DataProducts().path)
                rebuilt = build_report(release["observations"], release["derived"]["collection"], release["derived"]["as_of"])
                checks = [artifact["schema_version"] == "atlas-delivery-1",
                    artifact["content_sha256"] == request["report_sha256"] == rebuilt["report_sha256"],
                    artifact["terms_sha256"] == digest(TERMS), artifact["input_sha256"] == digest(request),
                    artifact["report"] == rebuilt["derived"],
                    0 <= self.clock() - artifact["report"]["as_of"] <= request["max_age_seconds"]]
            elif offer_id == "csv-normalize":
                require_keys(artifact, {"schema_version", "columns", "numeric_columns", "rows", "row_count",
                    "input_sha256", "generated_at"}, "CSV artifact")
                checks = [artifact["schema_version"] == "normalized-csv-1",
                    artifact["columns"] == request["columns"],
                    artifact["numeric_columns"] == request["numeric_columns"],
                    artifact["input_sha256"] == digest(request),
                    type(artifact["row_count"]) is int,
                    artifact["rows"] == csv_rows(request),
                    artifact["row_count"] == len(artifact["rows"])]
            elif offer_id == "arbitrum-state":
                require_keys(artifact, {"schema_version", "chain_id", "block_number", "block_hash",
                    "block_timestamp", "gas_price_wei", "source", "finality", "input_sha256", "generated_at"},
                    "chain artifact")
                observed = self.rpc("eth_getBlockByHash", [block_hash(artifact["block_hash"]), False])
                if not isinstance(observed, dict):
                    raise MachineError("delivered block cannot be retrieved")
                checks = [artifact["schema_version"] == "arbitrum-state-1",
                    type(artifact["chain_id"]) is int and artifact["chain_id"] == request["chain_id"],
                    artifact["input_sha256"] == digest(request), artifact["source"] == RPC_URL,
                    artifact["finality"] == "LATEST_L2_NOT_L1_FINALIZED",
                    hex_integer(self.rpc("eth_chainId", [])) == request["chain_id"],
                    type(artifact["block_number"]) is int,
                    artifact["block_number"] == hex_integer(observed.get("number")),
                    artifact["block_hash"] == block_hash(observed.get("hash")),
                    type(artifact["block_timestamp"]) is int,
                    artifact["block_timestamp"] == hex_integer(observed.get("timestamp")),
                    -30 <= self.clock() - artifact["block_timestamp"] <= request["max_age_seconds"],
                    isinstance(artifact["gas_price_wei"], str)
                    and re.fullmatch(r"[0-9]{1,78}", artifact["gas_price_wei"]) is not None]
            else:
                raise MachineError("verification rule not found")
            checks.extend([type(artifact["generated_at"]) is int,
                           -5 <= self.clock() - artifact["generated_at"] <= 30,
                           len(canonical(artifact)) <= 200_000])
            if not all(checks):
                reasons.append("ARTIFACT_CONTRACT_MISMATCH")
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            reasons.append("ARTIFACT_CHECK_UNAVAILABLE")
        return {"rule_version": "commerce-verifier-1", "accepted": not reasons and bool(checks),
            "checks_passed": sum(checks), "checks_total": len(checks), "reason_codes": reasons,
            "artifact_hash": digest(artifact), "request_hash": digest(request), "verified_at": self.clock(),
            "assurance": "SOURCE_BOUND_DERIVED_REPORT_NOT_PRICE_OR_CAPACITY_ORACLE" if offer_id == "apac-compute-brief" else "TRANSFORMATION_FIDELITY" if offer_id == "csv-normalize"
                         else "CONFIGURED_RPC_CONSISTENCY_NOT_INDEPENDENT_ORACLE"}


class CommerceEngine:
    def __init__(self, store, workers):
        self.store, self.workers = store, workers

    def run(self, sid, oid):
        claim = self.store.claim(sid, oid)
        if claim is None:
            return self.store.order(sid, oid)
        try:
            artifact = self.workers.fulfill(claim["offer_id"], claim["request"])
            verification = self.workers.verify(claim["offer_id"], claim["request"], artifact)
            return self.store.complete(sid, oid, claim["lease"], artifact, verification)
        except (MachineError, httpx.HTTPError, ValueError, TypeError, KeyError):
            return self.store.fail(sid, oid, claim["lease"], "PROVIDER_OR_VERIFIER_UNAVAILABLE")
