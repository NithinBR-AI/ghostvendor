"""
Resilience result schema — structured output from Agent 4 (Resilience Verifier).

One ScenarioResult per chaos scenario per vendor.
One VendorResult per vendor.
ResilienceReport is the top-level artifact stored in ArtifactStore.
"""

from enum import Enum
from pydantic import BaseModel


class ScenarioOutcome(str, Enum):
    PASS = "pass"           # App handled the failure gracefully
    FAIL = "fail"           # App crashed, errored, or behaved incorrectly
    DEGRADED = "degraded"   # App returned a response but not the expected one


class ScenarioResult(BaseModel):
    mode: str
    outcome: ScenarioOutcome
    http_status: int | None = None
    response_body: str = ""
    exception: str | None = None
    response_time_ms: float | None = None
    notes: str = ""


class VendorResult(BaseModel):
    vendor_name: str
    criticality_score: int
    baseline_passed: bool
    scenarios: list[ScenarioResult]

    @property
    def failed_scenarios(self) -> list[ScenarioResult]:
        return [s for s in self.scenarios if s.outcome in (ScenarioOutcome.FAIL, ScenarioOutcome.DEGRADED)]

    @property
    def resilience_score(self) -> int:
        """
        Compute resilience score (0-100) for this vendor.

        Scoring formula (documented in SCORING.md):
          - Baseline pass:          20 pts (app works under normal conditions)
          - Per chaos scenario:     16 pts each (5 scenarios max = 80 pts)
            - PASS  = 16 pts (full graceful handling)
            - DEGRADED = 8 pts (partial handling — responded but incorrectly)
            - FAIL  = 0 pts (crashed or unhandled exception)

        Total = baseline_pts + sum(scenario_pts)
        """
        baseline_pts = 20 if self.baseline_passed else 0

        scenario_pts_map = {
            ScenarioOutcome.PASS: 16,
            ScenarioOutcome.DEGRADED: 8,
            ScenarioOutcome.FAIL: 0,
        }

        total_scenario_pts = sum(
            scenario_pts_map[s.outcome] for s in self.scenarios
        )

        # Score against fixed 5-scenario denominator regardless of how many ran.
        # Fewer scenarios run = lower coverage = proportionally lower score.
        # This prevents early-stop (2 passes out of 2) from scoring the same as 5 passes out of 5.
        _MAX_SCENARIOS = 5
        scenario_score = int((total_scenario_pts / (_MAX_SCENARIOS * 16)) * 80)
        return min(100, baseline_pts + scenario_score)


class ResilienceReport(BaseModel):
    repository: str
    vendor_results: list[VendorResult]

    @property
    def overall_score(self) -> int:
        """Weighted average across vendors — critical vendors weighted by criticality_score."""
        if not self.vendor_results:
            return 0
        total_weight = sum(v.criticality_score for v in self.vendor_results)
        if total_weight == 0:
            return 0
        weighted = sum(v.resilience_score * v.criticality_score for v in self.vendor_results)
        return int(weighted / total_weight)

    @property
    def all_failed_scenarios(self) -> list[tuple[str, ScenarioResult]]:
        """Flat list of (vendor_name, scenario) for all failures — Agent 5 input."""
        failures = []
        for vr in self.vendor_results:
            for s in vr.failed_scenarios:
                failures.append((vr.vendor_name, s))
        return failures
