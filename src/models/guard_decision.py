"""Output schema for Agent 3 (Context Guard) — one decision per Evil Twin."""

from enum import Enum
from pydantic import BaseModel


class RiskLevel(str, Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class ASTFinding(BaseModel):
    line: int
    code: str
    reason: str


class GuardDecision(BaseModel):
    vendor_name: str
    approved: bool
    risk_level: RiskLevel
    ast_findings: list[ASTFinding]
    llm_verdict: str
    llm_reasoning: str
    sandbox_exit_code: int | None = None
    sandbox_stdout: str = ""
    sandbox_stderr: str = ""
    sandbox_unexpected: list[str] = []

    @property
    def summary(self) -> str:
        status = "APPROVED" if self.approved else "BLOCKED"
        findings = len(self.ast_findings)
        unexpected = len(self.sandbox_unexpected)
        return (
            f"{self.vendor_name}: {status} | risk={self.risk_level.value} | "
            f"ast_findings={findings} | sandbox_unexpected={unexpected}"
        )
