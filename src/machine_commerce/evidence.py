"""Cross-linked, public evidence for an already completed agent purchase.

No signing, submission, live policy registration or automatic payment authority.
The builder joins recorded runtime state to separately observed chain evidence.
"""

import copy
import time

from economic_machine.values import digest

from .domain import money_atoms, money_string
from .market import negotiate

SCHEMA = "economic-machine-completed-trade-1"
CHAIN = 421614
ASSET = "0x75faf114eafb1bdbe2f0316df893fd58ce46aa4d"
RPCS = {"https://sepolia-rollup.arbitrum.io/rpc", "https://arbitrum-sepolia.drpc.org"}
PAYMENT_EVENTS = ["PAYMENT_PREPARED", "PAYMENT_CHALLENGE_ADMITTED",
                  "PAYMENT_AUTHORIZATION_SENT", "PAYMENT_SETTLEMENT_REPORTED"]


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def build_completed_trade(record, observations, signature_checks, archived_audit, checked_at=None):
    """Publish only this fixed, isolated test trade after every linkage passes."""
    proof, payment, match, mandate = (record[k] for k in ["proof", "payment", "match", "mandate"])
    demand, supply, events = record["demand"], record["supply"], record["events"]
    body, terms = payment["body"], match["body"]["terms"]
    binding, auth = body["binding"], body["authorization"]
    amount = binding["amount_atoms"]
    require(proof["verified"] is True and proof["mainnet"] is False and proof["chain_id"] == CHAIN
            and proof["token"].lower() == ASSET and proof["test_usdc"] == "0.01", "fixed testnet proof required")
    require(record["journal_verified"] is True, "runtime journal integrity required")
    require(payment["status"] == "SETTLED" and payment["id"] == proof["payment_id"]
            and payment["tx_hash"] == proof["tx_hash"] and payment["owner"] == demand["owner"]
            and payment["owner"] == mandate["owner"], "completed owned payment required")
    require(payment["match_id"] == match["id"] and payment["mandate_id"] == mandate["id"]
            and mandate["id"] == proof["mandate"]["id"] and match["demand_id"] == demand["id"]
            and match["supply_id"] == supply["id"] and terms["demand_id"] == demand["id"]
            and terms["supply_id"] == supply["id"] and terms["buyer_id"] == demand["owner"]
            and terms["seller_id"] == supply["owner"], "trade record IDs disagree")
    require(digest(terms) == match["terms_hash"] == proof["terms_hash"] == binding["terms_hash"],
            "terms hash does not link agreement and payment")
    require(amount == 10_000 == money_atoms(terms["total_price"])
            and binding["asset"].lower() == ASSET and binding["network"] == "eip155:421614"
            and terms["asset"] == mandate["payment_asset"]
            and auth["from"] == mandate["payer"] and auth["to"] == binding["pay_to"]
            and auth["value"] == str(amount) and auth["nonce"] == proof["authorization_nonce"],
            "economic authorization does not match the agreement")
    require(auth["from"].lower() == proof["before"]["buyer"]["address"].lower()
            and auth["to"].lower() == proof["before"]["seller"]["address"].lower(), "payer or seller changed")
    require(body["request"] == {"terms_hash": proof["terms_hash"], "data_version": terms["data_version"],
            "units": terms["units"], "purpose": terms["purpose"], "license": terms["license"]},
            "merchant request does not match negotiated conditions")
    require(amount <= mandate["max_order"] <= mandate["budget"] and mandate["reserved"] == 0
            and mandate["spent"] == amount and proof["mandate"]["spent"] == amount
            and proof["mandate"]["reserved"] == 0, "settled capital does not match the purchase")
    require(payment["created"] < min(match["expires"], demand["expires"], supply["expires"], mandate["expires"]),
            "agreement or mandate was already expired at payment preparation")
    replay = negotiate(demand["body"], supply["body"], payment["created"])
    require(replay["status"] == "AGREED", "recorded buyer conditions do not accept this seller")
    require(all(terms[k] == v for k, v in replay["terms"].items())
            and replay["trace"] == match["body"]["trace"], "policy replay differs from the recorded agreement")
    delivery = payment["delivery"]
    require(delivery and digest(delivery["artifact"]) == delivery["artifact_hash"] == proof["artifact_hash"]
            and delivery["artifact"] == proof["artifact"]
            and delivery["artifact"]["terms_hash"] == proof["terms_hash"]
            and delivery["artifact"]["data_version"] == terms["data_version"], "delivery is not bound to this trade")
    observation = payment["observation"]
    require(observation and observation["status"] == "PAID" and observation["tx_hash"] == proof["tx_hash"]
            and observation["amount_atoms"] == str(amount) and observation["nonce"] == auth["nonce"]
            and observation["payer"] == auth["from"] and observation["pay_to"] == auth["to"]
            and observation["asset"].lower() == ASSET
            and observation["block_number"] == proof["receipt_block"]
            and observation["block_hash"] == proof["receipt_block_hash"]
            and observation["finality"] == "finalized", "runtime chain reconciliation disagrees")
    require(len(observations) == 2 and {o["rpc"] for o in observations} == RPCS
            and all(o["chain_id"] == CHAIN and o["receipt_status"] == 1
                    and o["finalized_block"] >= proof["receipt_block"]
                    and o["nonce_consumed_at_finalized"] is True and o["public_snapshot_matches"] is True
                    and o["exact_transfer_logs"] == 1 and o["authorization_used_logs"] == 1 for o in observations),
            "two distinct approved RPC audits required")
    require(archived_audit["verified"] is True and archived_audit["read_only"] is True
            and archived_audit["tx_hash"] == proof["tx_hash"]
            and len(archived_audit["observations"]) == 2
            and {o["rpc"] for o in archived_audit["observations"]} == RPCS
            and all(o["chain_id"] == CHAIN and o["receipt_status"] == 1
                    and o["finalized_block"] >= proof["receipt_block"]
                    and o["exact_transfer_logs"] == 1 and o["authorization_used_logs"] == 1
                    and o["historical_balances_match"] is True for o in archived_audit["observations"]),
            "preserved historical balance audit required")
    require(len(signature_checks) == 2 and {s["rpc"] for s in signature_checks} == RPCS
            and all(s["recovered_payer"].lower() == auth["from"].lower()
                    and s["payload_hash"] == payment["signature_hash"]
                    and s["authorization_matches"] is True for s in signature_checks),
            "on-chain signature does not link to the runtime authorization payload")
    kinds = [e["kind"] for e in events]
    ordinals = [e["ordinal"] for e in events]
    require(ordinals == sorted(set(ordinals)), "event order is invalid")
    for kind in PAYMENT_EVENTS:
        require(kinds.count(kind) == 1, "missing or duplicated payment transition: " + kind)
    require([kinds.index(k) for k in PAYMENT_EVENTS] == sorted(kinds.index(k) for k in PAYMENT_EVENTS),
            "payment transitions occurred out of order")
    selected = {e["kind"]: e for e in events if e["kind"] in PAYMENT_EVENTS}
    require(all(e["event"].get("payment_id") == payment["id"] for e in selected.values()), "event payment ID changed")
    require(selected["PAYMENT_PREPARED"]["event"]["terms_hash"] == proof["terms_hash"]
            and selected["PAYMENT_PREPARED"]["event"]["amount_atoms"] == amount
            and selected["PAYMENT_PREPARED"]["event"]["nonce"] == auth["nonce"]
            and selected["PAYMENT_PREPARED"]["event"]["mandate_id"] == mandate["id"], "prepared event disagrees")
    require(selected["PAYMENT_AUTHORIZATION_SENT"]["event"]["payload_hash"] == payment["signature_hash"]
            and selected["PAYMENT_AUTHORIZATION_SENT"]["event"]["authorization_hash"] == digest(auth)
            and selected["PAYMENT_SETTLEMENT_REPORTED"]["event"]["tx_hash"] == proof["tx_hash"]
            and selected["PAYMENT_SETTLEMENT_REPORTED"]["event"]["delivery_received"] is True, "submission or delivery event disagrees")
    settled = [e for e in events if e["kind"] == "PAYMENT_CHAIN_OBSERVED" and e["event"].get("state") == "SETTLED"]
    require(len(settled) == 1 and settled[0]["event"]["observation"] == observation
            and settled[0]["ordinal"] > selected["PAYMENT_SETTLEMENT_REPORTED"]["ordinal"], "settled journal event required")
    result = {"schema": SCHEMA, "verified": True, "read_only": True, "checked_at": checked_at or int(time.time()),
        "execution_kind": "RECORDED_ISOLATED_AGENT_TESTNET_PURCHASE", "new_payment_authorized": False,
        "network": "Arbitrum Sepolia", "chain_id": CHAIN, "token": ASSET,
        "policy": {"budget": money_string(demand["body"]["max_total_atoms"]), "units": terms["units"],
            "max_age_seconds": demand["body"]["max_age_seconds"], "purpose": terms["purpose"], "license": terms["license"],
            "data_type": terms["data_type"], "evaluated_at": payment["created"]},
        "supplier": {"name": supply["body"]["name"], "ask": money_string(supply["body"]["ask_atoms"]),
            "counter": terms["unit_price"], "discount_bps": supply["body"]["discount_bps"],
            "data_version": terms["data_version"], "updated_at": supply["body"]["updated_at"]},
        "agreement": {"id": match["id"], "terms": copy.deepcopy(terms), "terms_hash": proof["terms_hash"],
            "trace": copy.deepcopy(replay["trace"]), "policy_replay_matches": True, "language_model_calls": 0},
        "authorization": {"scheme": "EIP-3009 / EIP-712", "signer": auth["from"], "recipient": auth["to"],
            "amount_atoms": str(amount), "nonce": auth["nonce"], "payload_hash": payment["signature_hash"],
            "recovered_from_chain": True, "signing_method": "EXTERNAL_DISPOSABLE_BUYER_OPERATOR_CLI",
            "customer_browser_signature": False},
        "payment": {"id": payment["id"], "status": payment["status"], "amount": money_string(amount),
            "tx_hash": proof["tx_hash"], "receipt_block": proof["receipt_block"], "receipt_block_hash": proof["receipt_block_hash"],
            "explorer": "https://sepolia.arbiscan.io/tx/" + proof["tx_hash"]},
        "capital": {"balance_evidence": "PRESERVED_EARLIER_TWO_RPC_AUDIT_NOT_A_FRESH_BALANCE_READ",
            "budget_atoms": mandate["budget"], "reserved_atoms": 0, "spent_atoms": amount,
            "buyer_before_atoms": proof["before"]["buyer"]["usdc_atoms"], "buyer_after_atoms": proof["after_at_receipt_block"]["buyer"]["usdc_atoms"],
            "seller_before_atoms": proof["before"]["seller"]["usdc_atoms"], "seller_after_atoms": proof["after_at_receipt_block"]["seller"]["usdc_atoms"]},
        "delivery": {"artifact": copy.deepcopy(delivery["artifact"]), "artifact_hash": delivery["artifact_hash"],
            "terms_and_version_match": True, "public_block_readback_matches": True},
        "audit": {"rpc_observations": copy.deepcopy(observations), "signature_checks": copy.deepcopy(signature_checks),
            "preserved_balance_audit": copy.deepcopy(archived_audit), "preserved_audit_hash": digest(archived_audit),
            "journal_verified": True, "timeline": [{k: e[k] for k in ["ordinal", "kind", "event_hash", "previous_hash"]} for e in events]},
        "deployment": {"hosted_app": "https://machine.148-113-153-116.nip.io/commerce/", "settlement_contract": ASSET,
            "custom_contract_address": None, "competition_eligibility_certified": False},
        "replay_inputs": {"demand": copy.deepcopy(demand["body"]), "supply": copy.deepcopy(supply["body"])},
        "scope": "Recorded agent purchase. No new wallet signature or payment. Off-chain delivery is not atomic with x402; RPC finality is not an independent L1 proof."}
    result["bundle_hash"] = digest(result)
    return result


def validate_bundle(bundle):
    require(isinstance(bundle, dict), "verified read-only trade object required")
    require(bundle.get("schema") == SCHEMA and bundle.get("verified") is True
            and bundle.get("read_only") is True and bundle.get("chain_id") == CHAIN
            and bundle.get("new_payment_authorized") is False, "verified read-only trade bundle required")
    require(bundle.get("bundle_hash") == digest({k: v for k, v in bundle.items() if k != "bundle_hash"}),
            "trade evidence bundle changed")
    return bundle


def replay_policy(bundle, raw):
    validate_bundle(bundle)
    require(isinstance(raw, dict) and set(raw) == {"budget", "max_age_seconds", "license"}, "three replay conditions required")
    require(type(raw["max_age_seconds"]) is int and 1 <= raw["max_age_seconds"] <= 86400
            and isinstance(raw["license"], str) and raw["license"] in {"internal-use", "commercial-use"},
            "bounded replay conditions required")
    budget = money_atoms(raw["budget"])
    require(0 < budget <= 1_000_000, "replay budget must be between 0 and 1 test USDC")
    demand = copy.deepcopy(bundle["replay_inputs"]["demand"])
    demand.update(max_total_atoms=budget, max_unit_atoms=budget, max_age_seconds=raw["max_age_seconds"], license=raw["license"])
    result = negotiate(demand, bundle["replay_inputs"]["supply"], bundle["policy"]["evaluated_at"])
    return {"historical_replay": True, "execution_authority": "NONE", "new_payment": False,
            "evaluated_at": bundle["policy"]["evaluated_at"], "result": result}
