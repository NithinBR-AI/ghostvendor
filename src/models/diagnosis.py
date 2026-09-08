"""
Diagnosis schema — structured output from Agent 5 (Runtime Debugger).

One DiagnosisResult per vendor. DiagnosisReport is the top-level artifact
stored in ArtifactStore and passed to Agent 6 (Patch Generator).
"""

from enum import Enum
from pydantic import BaseModel


class FailurePattern(str, Enum):
    MISSING_TIMEOUT = "missing_timeout"
    SILENT_FAILURE_SWALLOWING = "silent_failure_swallowing"
    NO_ERROR_PROPAGATION = "no_error_propagation"
    MISSING_RETRY = "missing_retry"
    MISSING_CIRCUIT_BREAKER = "missing_circuit_breaker"
    JSON_PARSE_UNGUARDED = "json_parse_unguarded"
    EMPTY_RESPONSE_UNGUARDED = "empty_response_unguarded"


class FixStrategy(str, Enum):
    CIRCUIT_BREAKER = "circuit_breaker"
    TIMEOUT_WITH_RETRY = "timeout_with_retry"
    FIRE_AND_FORGET = "fire_and_forget"
    GRACEFUL_DEGRADATION = "graceful_degradation"
    INPUT_VALIDATION = "input_validation"


class DiagnosisResult(BaseModel):
    vendor: str
    patterns: list[FailurePattern]
    root_cause: str
    affected_file: str
    affected_function: str
    fix_description: str
    fix_strategy: FixStrategy
    criticality_note: str


class DiagnosisReport(BaseModel):
    repository: str
    diagnoses: list[DiagnosisResult]

    def for_vendor(self, vendor_name: str) -> DiagnosisResult | None:
        for d in self.diagnoses:
            if d.vendor == vendor_name:
                return d
        return None
