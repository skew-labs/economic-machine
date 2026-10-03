"""Read-only Arbitrum entitlement verification and unsigned purchase plans.

There is no private key, signer or broadcaster in this module. Native NFT payment
and x402 are alternative purchase paths; they must not both charge one sale.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from economic_machine.values import MachineError, canonical, digest

from .atlas import load_report

CHAIN_ID = 421614
USDC = "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"
RPCS = ("https://sepolia-rollup.arbitrum.io/rpc", "https://arbitrum-sepolia.drpc.org")
ROOT = Path(__file__).resolve().parents[2]


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value, 16) == 0:
        raise MachineError("NONZERO_EVM_ADDRESS_REQUIRED")
    return to_checksum_address(value)


def bytes32(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", value):
        raise MachineError("CONTENT_HASH_REQUIRED")
    return bytes.fromhex(value.removeprefix("0x"))


def token_number(value):
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,77}", value):
        value = int(value)
    if type(value) is not int or not 0 < value < 2**256:
        raise MachineError("BOUNDED_TOKEN_ID_REQUIRED")
    return value


def release_identity(report_hash, terms_hash):
    return keccak(canonical({"report": report_hash, "terms": terms_hash}))


def call_data(signature, types, arguments):
    return "0x" + (keccak(text=signature)[:4] + encode(types, arguments)).hex()


def rpc_read(url, method, params):
    if url not in RPCS or method not in {"eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call"}:
        raise MachineError("READ_ONLY_RPC_REQUIRED")
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client, client.stream(
        "POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ) as response:
        response.raise_for_status()
        payload = bytearray()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > 512000:
                raise MachineError("RPC_RESPONSE_LIMIT")
    result = json.loads(payload)
    if result.get("id") != 1 or result.get("jsonrpc") != "2.0" or "error" in result or "result" not in result:
        raise MachineError("RPC_READ_UNAVAILABLE")
    return result["result"]


class DataPassChain:
    def __init__(self, contract=None, code_hash=None, reader=rpc_read, clock=time.time):
        self.contract = address(contract) if contract else None
        self.code_hash = code_hash
        self.reader, self.clock = reader, clock
        if self.contract and (not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", code_hash)):
            raise MachineError("PINNED_DATAPASS_BYTECODE_REQUIRED")

    def status(self):
        return {"chain_id": CHAIN_ID, "contract": self.contract,
                "status": "CONFIGURED_REQUIRES_RPC_VERIFICATION" if self.contract else "NOT_DEPLOYED",
                "runtime_sha256": self.code_hash, "signing_authority": "NONE",
                "payment_mode": "ERC20_PURCHASE_ALTERNATIVE_TO_X402_NOT_DOUBLE_PAYMENT"}

    def read(self, calldata):
        values, evidence = self.read_many([calldata])
        return values[0], evidence

    def read_many(self, calldatas):
        if not self.contract:
            raise MachineError("DATAPASS_NOT_DEPLOYED")
        if (not isinstance(calldatas, list) or not 1 <= len(calldatas) <= 8
                or any(not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){4,2048}", data) for data in calldatas)):
            raise MachineError("BOUNDED_READ_ONLY_CALLDATA_REQUIRED")
        heads = []
        for source in RPCS:
            if int(self.reader(source, "eth_chainId", []), 16) != CHAIN_ID:
                raise MachineError("DATAPASS_WRONG_CHAIN")
            head = self.reader(source, "eth_getBlockByNumber", ["finalized", False])
            if not isinstance(head, dict) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", head.get("hash", "")):
                raise MachineError("FINALIZED_BLOCK_REQUIRED")
            heads.append(int(head["number"], 16))
        block = hex(min(heads))
        observations = []
        for source in RPCS:
            state = self.reader(source, "eth_getBlockByNumber", [block, False])
            if (not isinstance(state, dict) or int(state["number"], 16) != int(block, 16)
                    or not re.fullmatch(r"0x[0-9a-fA-F]{64}", state.get("hash", ""))):
                raise MachineError("PINNED_BLOCK_REQUIRED")
            age = int(self.clock()) - int(state["timestamp"], 16)
            if not -5 <= age <= 3600:
                raise MachineError("STALE_ENTITLEMENT_BLOCK")
            code = self.reader(source, "eth_getCode", [self.contract, block])
            if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", code):
                raise MachineError("DATAPASS_RUNTIME_UNAVAILABLE")
            if hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() != self.code_hash:
                raise MachineError("DATAPASS_RUNTIME_MISMATCH")
            results = []
            for calldata in calldatas:
                result = self.reader(source, "eth_call", [{"to": self.contract, "data": calldata}, block])
                if not isinstance(result, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", result):
                    raise MachineError("INVALID_ENTITLEMENT_RESULT")
                results.append(result.lower())
            observations.append({"rpc": source, "block_hash": state["hash"].lower(),
                "results": results, "timestamp": int(state["timestamp"], 16)})
        if (observations[0]["block_hash"] != observations[1]["block_hash"]
                or observations[0]["results"] != observations[1]["results"]):
            raise MachineError("ENTITLEMENT_RPC_DISAGREEMENT")
        return [bytes.fromhex(value[2:]) for value in observations[0]["results"]], {
            "block_number": int(block, 16), "block_hash": observations[0]["block_hash"],
            "block_timestamp": observations[0]["timestamp"], "sources": list(RPCS), "finality": "TWO_RPC_FINALIZED_L2",
            "checked_at": int(self.clock()), "runtime_sha256": self.code_hash, "calls_at_same_block": len(calldatas)}

    def entitled(self, token_id, holder, report_hash, terms_hash):
        token_id = token_number(token_id)
        holder = address(holder)
        data = call_data("entitlement(uint256,address,bytes32,bytes32)",
                         ["uint256", "address", "bytes32", "bytes32"],
                         [token_id, holder, bytes32(report_hash), bytes32(terms_hash)])
        results, evidence = self.read_many([data, call_data("licenses(uint256)", ["uint256"], [token_id])])
        result, license_result = results
        try:
            accepted = decode(["bool"], result)[0]
        except Exception as exc:
            raise MachineError("INVALID_ENTITLEMENT_ABI") from exc
        license_evidence = evidence
        try:
            release_id, expires = decode(["bytes32", "uint64"], license_result)
        except Exception as exc:
            raise MachineError("INVALID_LICENSE_ABI") from exc
        # A finalized block may predate wall-clock expiry. Never prolong a license
        # by the settlement lag; expiration is immutable for an issued token.
        accepted = accepted and int(self.clock()) < expires and release_id == release_identity(report_hash, terms_hash)
        return {"accepted": accepted, "holder": holder, "token_id": str(token_id),
                "report_sha256": report_hash, "terms_sha256": terms_hash, "expires_at": expires,
                "release_id": "0x" + release_id.hex(),
                "evidence": evidence, "license_evidence": license_evidence}

    def purchase_plan(self, holder, report, purchase_id):
        holder = address(holder)
        release_id = release_identity(report["report_sha256"], report["terms_sha256"])
        raw, evidence = self.read(call_data("releaseInfo(bytes32)", ["bytes32"], [release_id]))
        kinds = ["(address,address,bytes32,bytes32,bytes32,uint128,uint64,uint64,bool,bool,string)"]
        try:
            release = decode(kinds, raw)[0]
        except Exception as exc:
            raise MachineError("INVALID_RELEASE_ABI") from exc
        seller, asset, root, terms, provenance, price, duration, sale_ends, transferable, active, _uri = release
        if (root != bytes32(report["report_sha256"]) or terms != bytes32(report["terms_sha256"])
                or provenance != bytes32(report["derived"]["source_observation_root"])
                or asset.lower() != USDC.lower() or duration != report["terms"]["duration_seconds"]
                or transferable != report["terms"]["transferable"] or not active
                or sale_ends <= self.clock() or price <= 0 or price > 1_000_000
                or seller.lower() == holder.lower()):
            raise MachineError("ONCHAIN_RELEASE_TERMS_MISMATCH")
        order_id = bytes32(purchase_id)
        purchase = call_data("purchase(bytes32,bytes32,bytes32,uint128,bytes32)",
            ["bytes32", "bytes32", "bytes32", "uint128", "bytes32"], [release_id, root, terms, price, order_id])
        approve = call_data("approve(address,uint256)", ["address", "uint256"], [self.contract, price])
        plan = {"chain_id": CHAIN_ID, "from": holder, "asset": address(USDC), "amount_atoms": str(price),
                "report_sha256": report["report_sha256"], "terms_sha256": report["terms_sha256"],
                "purchase_id": "0x" + order_id.hex(), "release_id": "0x" + release_id.hex(),
                "expires_at": min(int(self.clock()) + 60, sale_ends), "evidence": evidence,
                "transactions": [{"to": address(USDC), "data": approve, "value": "0x0", "purpose": "EXACT_ALLOWANCE_IF_NEEDED"},
                                 {"to": self.contract, "data": purchase, "value": "0x0", "purpose": "VERSION_BOUND_LICENSE_PURCHASE"}],
                "signing_authority": "CUSTOMER_WALLET_ONLY", "broadcasts": 0,
                "fees": "WALLET_ESTIMATION_REQUIRED", "x402_payment_required": False}
        return plan | {"plan_sha256": digest(plan)}

    def resale_plan(self, holder, report, token_id, purchase_id, min_remaining_seconds):
        holder, token_id = address(holder), token_number(token_id)
        if type(min_remaining_seconds) is not int or not 60 <= min_remaining_seconds <= 86400:
            raise MachineError("BOUNDED_REMAINING_LICENSE_REQUIRED")
        raw, sale_evidence = self.read(call_data("sales(uint256)", ["uint256"], [token_id]))
        try:
            seller, price, expires, nonce = decode(["address", "uint128", "uint64", "uint64"], raw)
        except Exception as exc:
            raise MachineError("INVALID_RESALE_ABI") from exc
        if (int(seller, 16) == 0 or seller.lower() == holder.lower()
                or not 0 < price <= 1000000 or self.clock() >= expires):
            raise MachineError("RESALE_UNAVAILABLE_OR_PRICE_OUTSIDE_POLICY")
        grant = self.entitled(token_id, seller, report["report_sha256"], report["terms_sha256"])
        if not grant["accepted"] or grant["expires_at"] - int(self.clock()) < min_remaining_seconds:
            raise MachineError("REMAINING_LICENSE_OR_VERSION_MISMATCH")
        # The payment asset is independently checked. A matching content root
        # alone must never cause a different ERC-20 to be charged.
        release_id = release_identity(report["report_sha256"], report["terms_sha256"])
        release, release_evidence = self.read(call_data("releaseInfo(bytes32)", ["bytes32"], [release_id]))
        values = decode(["(address,address,bytes32,bytes32,bytes32,uint128,uint64,uint64,bool,bool,string)"], release)[0]
        if values[1].lower() != USDC or not values[8]:
            raise MachineError("RESALE_ASSET_OR_TRANSFER_TERMS_MISMATCH")
        order_id = bytes32(purchase_id)
        purchase = call_data("purchaseResale(uint256,address,uint128,uint64,bytes32,bytes32,bytes32)",
            ["uint256", "address", "uint128", "uint64", "bytes32", "bytes32", "bytes32"],
            [token_id, seller, price, nonce, bytes32(report["report_sha256"]), bytes32(report["terms_sha256"]), order_id])
        approve = call_data("approve(address,uint256)", ["address", "uint256"], [self.contract, price])
        body = {"chain_id": CHAIN_ID, "from": holder, "token_id": str(token_id), "seller": address(seller),
                "asset": address(USDC), "price_atoms": str(price), "listing_nonce": nonce,
                "license_expires": grant["expires_at"], "expires_at": min(int(self.clock()) + 60, expires),
                "report_sha256": report["report_sha256"], "terms_sha256": report["terms_sha256"],
                "transactions": [{"to": address(USDC), "data": approve, "value": "0x0", "purpose": "EXACT_ALLOWANCE_IF_NEEDED"},
                                 {"to": self.contract, "data": purchase, "value": "0x0", "purpose": "ATOMIC_LICENSE_RESALE"}],
                "evidence": {"sale": sale_evidence, "license": grant, "release": release_evidence},
                "signing_authority": "CUSTOMER_WALLET_ONLY", "broadcasts": 0, "x402_payment_required": False}
        return body | {"plan_sha256": digest(body)}


class DataProducts:
    def __init__(self, report_path=None, chain=None):
        self.path = Path(report_path or os.environ.get("MACHINE_ATLAS_RELEASE", ROOT / "artifacts/atlas-release/atlas.json"))
        self.chain = chain or DataPassChain(os.environ.get("DATAPASS_CONTRACT"), os.environ.get("DATAPASS_RUNTIME_SHA256"))

    def version(self, report_hash=None):
        if report_hash is None:
            return load_report(self.path)
        if not isinstance(report_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", report_hash):
            raise MachineError("CANONICAL_REPORT_VERSION_REQUIRED")
        versions = self.path.parent / "versions"
        if versions.is_symlink():
            raise MachineError("TRUSTED_RELEASE_DIRECTORY_REQUIRED")
        target = versions / (report_hash + ".json")
        report = load_report(target) if target.exists() else load_report(self.path)
        if report["report_sha256"] != report_hash:
            raise MachineError("DATASET_VERSION_UNAVAILABLE")
        return report

    def catalog(self):
        report = load_report(self.path)
        return {"products": [{"id": report["derived"]["product_id"], "name": "Atlas APAC Compute Brief",
                "version": report["report_sha256"], "terms_sha256": report["terms_sha256"],
                "source_observation_root": report["derived"]["source_observation_root"],
                "as_of": report["derived"]["as_of"], "coverage": report["derived"]["coverage"],
                "license": report["terms"], "rights_assurance": report["sale_admission"]}], "datapass": self.chain.status()}

    def delivery(self, token_id, identity, version=None):
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        report = self.version(version)
        grant = self.chain.entitled(token_id, identity["address"], report["report_sha256"], report["terms_sha256"])
        if not grant["accepted"]:
            raise PermissionError("CURRENT_DATA_LICENSE_REQUIRED")
        return {"artifact": report["derived"], "artifact_sha256": report["report_sha256"],
                "license_verification": grant, "delivery_assurance": "BYTE_INTEGRITY_AND_FINALIZED_ACCESS_NOT_ATOMIC_PAYMENT"}

    def plan(self, identity, purchase_id, version=None):
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        return self.chain.purchase_plan(identity["address"], self.version(version), purchase_id)

    def sell(self, identity, token_id, price_atoms, sale_duration_seconds, version=None):
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        token_id = token_number(token_id)
        if type(price_atoms) is not int or not 1 <= price_atoms <= 1000000:
            raise MachineError("TEST_USDC_PRICE_CAP")
        if type(sale_duration_seconds) is not int or not 60 <= sale_duration_seconds <= 86400:
            raise MachineError("BOUNDED_RESALE_DURATION_REQUIRED")
        report = self.version(version)
        grant = self.chain.entitled(token_id, identity["address"], report["report_sha256"], report["terms_sha256"])
        expires = min(int(self.chain.clock()) + sale_duration_seconds, grant["expires_at"])
        if not grant["accepted"] or expires <= self.chain.clock() or not report["terms"]["transferable"]:
            raise MachineError("OWNED_UNEXPIRED_TRANSFERABLE_LICENSE_REQUIRED")
        body = {"chain_id": CHAIN_ID, "from": address(identity["address"]), "to": self.chain.contract,
                "data": call_data("listSale(uint256,uint128,uint64)", ["uint256", "uint128", "uint64"], [token_id, price_atoms, expires]),
                "value": "0x0", "token_id": str(token_id), "price_atoms": str(price_atoms), "sale_expires": expires,
                "license_expires": grant["expires_at"], "evidence": grant,
                "status": "UNSIGNED_NOT_BROADCAST", "signing_authority": "CUSTOMER_WALLET_ONLY"}
        return body | {"plan_sha256": digest(body)}

    def resale(self, identity, token_id, purchase_id, min_remaining_seconds, version=None):
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        return self.chain.resale_plan(identity["address"], self.version(version), token_id, purchase_id, min_remaining_seconds)

    def registration(self, identity, price_atoms, sale_duration_seconds):
        if not identity or identity.get("chain_id") != CHAIN_ID:
            raise MachineError("AUTHENTICATED_ARBITRUM_WALLET_REQUIRED")
        if type(price_atoms) is not int or not 1 <= price_atoms <= 1000000:
            raise MachineError("TEST_USDC_PRICE_CAP")
        if type(sale_duration_seconds) is not int or not 3600 <= sale_duration_seconds <= 2592000:
            raise MachineError("BOUNDED_RELEASE_SALE_DURATION")
        holder = address(identity["address"])
        authorized, evidence = self.chain.read(call_data("publishers(address)", ["address"], [holder]))
        if decode(["bool"], authorized)[0] is not True:
            raise MachineError("ONCHAIN_PUBLISHER_AUTHORIZATION_REQUIRED")
        report = self.version()
        release_id = keccak(canonical({"report": report["report_sha256"], "terms": report["terms_sha256"]}))
        end = int(self.chain.clock()) + sale_duration_seconds
        uri = "https://machine.148-113-153-116.nip.io/commerce/demo/atlas?version=" + report["report_sha256"]
        release_type = "(address,address,bytes32,bytes32,bytes32,uint128,uint64,uint64,bool,bool,string)"
        signature = "registerRelease(bytes32," + release_type + ")"
        data = call_data(signature,
            ["bytes32", release_type],
            [release_id, (holder, address(USDC), bytes32(report["report_sha256"]), bytes32(report["terms_sha256"]),
             bytes32(report["derived"]["source_observation_root"]), price_atoms, report["terms"]["duration_seconds"],
             end, report["terms"]["transferable"], True, uri)])
        body = {"chain_id": CHAIN_ID, "from": holder, "to": self.chain.contract, "data": data, "value": "0x0",
                "release_id": "0x" + release_id.hex(), "report_sha256": report["report_sha256"],
                "price_atoms": str(price_atoms), "sale_ends": end, "evidence": evidence,
                "status": "UNSIGNED_NOT_BROADCAST", "signing_authority": "CUSTOMER_WALLET_ONLY"}
        return body | {"plan_sha256": digest(body)}


def deployment_draft(owner, compiler_path=None):
    owner = address(owner)
    artifacts = Path(compiler_path or ROOT / "artifacts/contracts.json")
    if artifacts.is_symlink() or artifacts.stat().st_size > 2_000_000:
        raise MachineError("TRUSTED_COMPILER_ARTIFACT_REQUIRED")
    compiled = json.loads(artifacts.read_text())["SkewDataPass"]
    data = "0x" + compiled["bytecode"] + encode(["address", "address"], [owner, address(USDC)]).hex()
    body = {"chain_id": CHAIN_ID, "from": owner, "value": "0x0", "data": data,
            "constructor": {"initial_owner": owner, "payment_asset": address(USDC)},
            "bytecode_sha256": hashlib.sha256(bytes.fromhex(compiled["bytecode"])).hexdigest(),
            "status": "UNSIGNED_NOT_BROADCAST", "signing_authority": "CUSTOMER_WALLET_ONLY",
            "gas_estimation": "WALLET_REQUIRED", "source": "contracts/SkewDataPass.sol"}
    return body | {"plan_sha256": digest(body)}
