"""External settlement bus contract. There is deliberately no live verifier here."""

from abc import ABC, abstractmethod
from datetime import datetime

from .values import MachineError, digest, ident, require_keys, utc


class SettlementVerifier(ABC):
    """A chain-specific implementation must verify signatures and finality itself."""

    @abstractmethod
    def verify_owner_authorization(self, intent: dict, authorization: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def verify_finalized_transaction(self, proof: dict) -> bool:
        raise NotImplementedError


def settle(receipt: dict, authorization: dict, proof: dict,
           verifier: SettlementVerifier) -> dict:
    """Link verified user authorization and finality to a prior intent.

    This never sends a transaction. A caller must provide real chain-specific
    proof validation; without one there is no SETTLED result.
    """
    if not isinstance(verifier, SettlementVerifier):
        raise MachineError("chain-specific settlement verifier required")
    if (receipt.get("terminal_state") != "AWAITING_AUTHORIZATION"
            or receipt.get("chain_status") != "NOT_SUBMITTED"
            or not isinstance(receipt.get("intent"), dict)):
        raise MachineError("receipt has no executable settlement intent")
    if digest({k: v for k, v in receipt.items() if k != "receipt_hash"}) != receipt.get("receipt_hash"):
        raise MachineError("decision receipt hash mismatch")
    intent = receipt["intent"]
    if digest({k: v for k, v in intent.items() if k != "intent_hash"}) != intent.get("intent_hash"):
        raise MachineError("intent hash mismatch")
    require_keys(authorization, {"owner_id", "intent_hash", "signed_at", "signature_ref"},
                 "authorization")
    if (authorization["owner_id"] != intent["owner_id"]
            or authorization["intent_hash"] != intent["intent_hash"]):
        raise MachineError("authorization does not bind owner and intent")
    signed_at = utc(authorization["signed_at"])
    if signed_at > intent["expires_at"]:
        raise MachineError("authorization after intent expiry")
    ident(authorization["signature_ref"], "signature reference")
    if not verifier.verify_owner_authorization(intent, authorization):
        raise MachineError("owner authorization not verified")
    require_keys(proof, {"network", "intent_hash", "txid", "submitted_at",
                         "finalized_at", "block_height", "actual_post_state",
                         "raw_receipt_hash"}, "settlement proof")
    if proof["network"] != intent["network"] or proof["intent_hash"] != intent["intent_hash"]:
        raise MachineError("chain proof is for a different network or intent")
    ident(proof["txid"], "transaction id")
    submitted_at, finalized_at = utc(proof["submitted_at"]), utc(proof["finalized_at"])
    if (submitted_at > intent["expires_at"]
            or datetime.fromisoformat(finalized_at) < datetime.fromisoformat(submitted_at)):
        raise MachineError("invalid submission/finality timing")
    if type(proof["block_height"]) is not int or proof["block_height"] < 0:
        raise MachineError("invalid block height")
    if not isinstance(proof["raw_receipt_hash"], str) or len(proof["raw_receipt_hash"]) != 64:
        raise MachineError("raw receipt hash required")
    if not verifier.verify_finalized_transaction(proof):
        raise MachineError("chain finality not verified")
    expected = {key: intent["expected"][key] for key in
                ("balance_after", "exposure_after", "daily_loss_after")}
    if not isinstance(proof["actual_post_state"], dict):
        raise MachineError("actual post-state required")
    actual = {key: proof["actual_post_state"].get(key) for key in expected}
    status = "SETTLED" if actual == expected else "POSTCONDITION_FAILED"
    result = {"schema_version": "economic-settlement-1",
              "decision_receipt_hash": receipt["receipt_hash"],
              "intent_hash": intent["intent_hash"], "network": proof["network"],
              "txid": proof["txid"], "block_height": proof["block_height"],
              "finalized_at": finalized_at, "raw_receipt_hash": proof["raw_receipt_hash"],
              "expected_post_state": expected, "actual_post_state": actual,
              "postcondition": status}
    result["settlement_receipt_hash"] = digest(result)
    return result
