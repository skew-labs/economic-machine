"""Replayable mined artifacts and publisher-provisioned source grants.

Legal permission is an issuer attestation. The score is recomputed, never taken
from the miner. This module has no key, broadcast or token-mint authority.
"""

import json
import re
from pathlib import Path

from economic_machine.values import MachineError, canonical, digest, require_keys
from machine_engine.mining import EDGE_FIELDS, JOB_FIELDS, LIMIT, normalize

from .datapass import DataProducts, address, bytes32, call_data, release_identity

TERMS = {
    "schema": "datapass-work-terms-1",
    "grant": "internal-use-derived-frozen-execution-recipe",
    "duration_seconds": 86400,
    "transferable": True,
    "redistribute_upstream_archives": False,
    "live_execution_authority": False,
}
MODEL = "SNAPSHOT_ROUTE_V1"


def work_input(raw):
    require_keys(raw, {"model", "title", "source_root", "source_description", "constraints", "edges"}, "work input")
    checked = normalize(raw | {"reward_units": "1", "bond_units": "1"})
    return {key: checked[key] for key in raw}


def score(raw, path):
    """The same bounded integer model as C++ and SkewArtifactMining.score."""
    raw = work_input(raw)
    t = {key: int(raw["constraints"][key]) for key in JOB_FIELDS}
    if (not isinstance(path, list) or not 1 <= len(path) <= t["maximum_hops"]
            or any(type(i) is not int or not 0 <= i < len(raw["edges"]) for i in path)):
        raise MachineError("WORK_PATH_BOUND")
    amount, asset, cost, used = t["amount_in"], t["asset_in"], 0, set()
    for index in path:
        e = {key: int(raw["edges"][index][key]) for key in EDGE_FIELDS}
        if e["pool_id"] in used or e["asset_in"] != asset:
            raise MachineError("WORK_ROUTE_DISCONNECTED_OR_REPEATED")
        used.add(e["pool_id"])
        fee = (amount * e["fee_ppm"] + 999999) // 1000000
        if amount <= fee or e["reserve_in"] + amount > LIMIT:
            raise MachineError("WORK_INPUT_OVERFLOW")
        output = e["reserve_out"] * (amount - fee) // (e["reserve_in"] + amount - fee)
        spot = e["reserve_out"] * amount // e["reserve_in"]
        if not 0 < output < e["reserve_out"] or not 0 < spot <= LIMIT:
            raise MachineError("WORK_ILLIQUID")
        if ((spot - output) * 10000 + spot - 1) // spot > t["maximum_impact_bps"]:
            raise MachineError("WORK_IMPACT")
        if (amount * 1000000 + output - 1) // output > LIMIT:
            raise MachineError("WORK_PRICE_OVERFLOW")
        if (e["reserve_in"] + amount) * (e["reserve_out"] - output) < e["reserve_in"] * e["reserve_out"]:
            raise MachineError("WORK_POOL_INVARIANT")
        amount, asset = output, e["asset_out"]
        cost += e["cost"]
        if cost > min(LIMIT, t["maximum_cost"]):
            raise MachineError("WORK_COST")
    if asset != t["asset_out"] or amount <= cost or amount - cost < t["minimum_net"]:
        raise MachineError("WORK_TERMINAL")
    return {"path": path, "net_output": str(amount - cost), "gross_output": str(amount), "cost": str(cost)}


def check_grant(grant, raw):
    require_keys(grant, {"schema", "input_sha256", "source_root", "publisher", "license", "evidence_sha256",
                         "valid_until", "allow_derived_sale", "allow_miner_distribution"}, "source grant")
    if (grant["schema"] != "skew-source-grant-1" or grant["input_sha256"] != digest(raw)
            or grant["source_root"] != raw["source_root"] or grant["allow_derived_sale"] is not True
            or grant["allow_miner_distribution"] is not True or grant["license"] != "publisher-owned-or-explicitly-licensed"
            or type(grant["valid_until"]) is not int or grant["valid_until"] <= 0):
        raise MachineError("RIGHTS_GRANT_REJECTED")
    address(grant["publisher"])
    if not any(bytes32(grant["evidence_sha256"])):
        raise MachineError("RIGHTS_EVIDENCE_REQUIRED")
    return digest(grant)


def build_artifact(raw, path, grant):
    raw = work_input(raw)
    rights_root = check_grant(grant, raw)
    result = score(raw, path)
    derived = {"schema": "skew-work-artifact-1", "product_id": "frozen-execution-recipe",
               "input": raw, "result": result, "source_observation_root": rights_root,
               "input_sha256": digest(raw), "assurance": "FROZEN_MODEL_REPLAY_NOT_LIVE_TRADE_OR_GLOBAL_OPTIMALITY"}
    return {"schema": "skew-work-release-1", "derived": derived, "report_sha256": digest(derived),
            "terms": TERMS, "terms_sha256": digest(TERMS), "rights_grant": grant,
            "sale_admission": "PUBLISHER_ATTESTED_RIGHTS_AND_DETERMINISTIC_REPLAY"}


def verify_artifact(report):
    require_keys(report, {"schema", "derived", "report_sha256", "terms", "terms_sha256", "rights_grant",
                          "sale_admission"}, "mined artifact")
    try:
        expected = build_artifact(report["derived"]["input"], report["derived"]["result"]["path"], report["rights_grant"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MachineError("MALFORMED_WORK_ARTIFACT") from exc
    if canonical(report) != canonical(expected):
        raise MachineError("WORK_ARTIFACT_REPLAY_MISMATCH")
    return expected


def safe_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128000:
        raise MachineError("BOUNDED_REGULAR_ARTIFACT_REQUIRED")
    return json.loads(path.read_text())


class WorkProducts(DataProducts):
    """Reuses the existing finalized license purchase/delivery path."""

    def __init__(self, directory, grants, chain):
        self.directory, self.grants, self.chain = Path(directory), Path(grants), chain
        if self.directory.is_symlink() or self.grants.is_symlink():
            raise MachineError("TRUSTED_ARTIFACT_DIRECTORY_REQUIRED")

    def version(self, report_hash=None):
        if not isinstance(report_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", report_hash):
            raise MachineError("EXPLICIT_WORK_VERSION_REQUIRED")
        report = verify_artifact(safe_json(self.directory / (report_hash + ".json")))
        if report["report_sha256"] != report_hash:
            raise MachineError("WORK_VERSION_MISMATCH")
        rights_root = report["derived"]["source_observation_root"]
        # Only operator-provisioned grants admit a sale. A miner-supplied JSON assertion is insufficient.
        approved = safe_json(self.grants / (rights_root + ".json"))
        if canonical(approved) != canonical(report["rights_grant"]):
            raise MachineError("PUBLISHER_PROVISIONED_RIGHTS_REQUIRED")
        return report

    def publication(self, identity, version, price_atoms, sale_duration_seconds):
        if not identity or identity.get("chain_id") != self.chain.chain_id:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        if type(price_atoms) is not int or not 1 <= price_atoms <= 1000000:
            raise MachineError("USDC_PRICE_CAP")
        if type(sale_duration_seconds) is not int or not 60 <= sale_duration_seconds <= 86400:
            raise MachineError("BOUNDED_SALE_DURATION_REQUIRED")
        report = self.version(version)
        holder = address(identity["address"])
        if holder != address(report["rights_grant"]["publisher"]):
            raise MachineError("RIGHTS_PUBLISHER_MISMATCH")
        from eth_abi import decode
        permission, evidence = self.chain.read(call_data("publishers(address)", ["address"], [holder]))
        if decode(["bool"], permission)[0] is not True:
            raise MachineError("ONCHAIN_PUBLISHER_AUTHORIZATION_REQUIRED")
        end = int(self.chain.clock()) + sale_duration_seconds
        if end + TERMS["duration_seconds"] > report["rights_grant"]["valid_until"]:
            raise MachineError("RIGHTS_EXPIRE_BEFORE_LICENSE")
        rid = release_identity(version, report["terms_sha256"])
        release_type = "(address,address,bytes32,bytes32,bytes32,uint128,uint64,uint64,bool,bool,string)"
        data = call_data("registerRelease(bytes32," + release_type + ")", ["bytes32", release_type],
            [rid, (holder, address(self.chain.asset), bytes32(version), bytes32(report["terms_sha256"]),
                   bytes32(report["derived"]["source_observation_root"]), price_atoms, TERMS["duration_seconds"],
                   end, True, True, "urn:skew:work:sha256:" + version)])
        body = {"chain_id": self.chain.chain_id, "from": holder, "to": self.chain.contract, "data": data,
                "value": "0x0", "release_id": "0x" + rid.hex(), "report_sha256": version,
                "rights_assurance": "PUBLISHER_ATTESTATION_NOT_LEGAL_PROOF", "evidence": evidence,
                "status": "UNSIGNED_NOT_BROADCAST", "signing_authority": "CUSTOMER_WALLET_ONLY"}
        return body | {"plan_sha256": digest(body)}
