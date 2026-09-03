"""Pydantic schema for vendor_spec.json — the central data contract between all agents."""

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class Criticality(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    NON_CRITICAL_NOTIFICATION = "non_critical_notification"


class NormalContract(BaseModel):
    success_status: int
    response_type: str


class ChaosScenario(BaseModel):
    mode: str
    duration_seconds: int | None = None
    delay_seconds: int | None = None


class ExpectedResilience(BaseModel):
    graceful_degradation: bool
    max_retry_attempts: int
    fallback_behavior: str | None = None


class Endpoint(BaseModel):
    path: str
    method: str
    criticality: Criticality
    normal_contract: NormalContract
    chaos_scenarios: list[ChaosScenario]
    expected_resilience: ExpectedResilience


class Vendor(BaseModel):
    name: str
    criticality: Criticality
    criticality_score: int = Field(ge=0, le=100)
    base_url_env: str
    endpoints: list[Endpoint]


class VendorSpec(BaseModel):
    repository: str
    discovered_vendors: list[Vendor]

    def vendors_by_criticality(self) -> list[Vendor]:
        """Return vendors sorted highest criticality_score first — attack order for Agent 2."""
        return sorted(self.discovered_vendors, key=lambda v: v.criticality_score, reverse=True)
