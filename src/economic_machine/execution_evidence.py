"""External proof boundary for a capital-locked execution lifecycle.

Implementations must independently authenticate the owner, submitted payload,
chain finality and account snapshot. No network adapter or signer is bundled.
"""

from abc import ABC, abstractmethod


class ExecutionEvidenceVerifier(ABC):
    @abstractmethod
    def verify_authorization(self, intent: dict, evidence: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def verify_submission(self, intent: dict, authorization: dict,
                          evidence: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def verify_finality(self, submission: dict, evidence: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def verify_account_snapshot(self, finality: dict, state: dict,
                                evidence: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def verify_execution_exception(self, intent: dict, submission: dict,
                                   finality: dict | None, evidence: dict) -> bool:
        """Authenticate a reverted, partial or orphaned transaction claim."""
        raise NotImplementedError
