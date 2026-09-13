"""Unit tests for the resilience scoring formula."""

import pytest
from models.resilience_result import (
    ResilienceReport, VendorResult, ScenarioResult, ScenarioOutcome,
)


def _vendor(name="stripe", criticality=80, baseline=True, outcomes=None):
    outcomes = outcomes or []
    scenarios = [ScenarioResult(mode=f"mode_{i}", outcome=o) for i, o in enumerate(outcomes)]
    return VendorResult(
        vendor_name=name,
        criticality_score=criticality,
        baseline_passed=baseline,
        scenarios=scenarios,
    )


class TestVendorScore:
    def test_all_pass(self):
        v = _vendor(outcomes=[ScenarioOutcome.PASS] * 5)
        assert v.resilience_score == 100

    def test_all_fail_no_baseline(self):
        v = _vendor(baseline=False, outcomes=[ScenarioOutcome.FAIL] * 5)
        assert v.resilience_score == 0

    def test_baseline_only_no_scenarios(self):
        v = _vendor(outcomes=[])
        # baseline 20 + 0 scenario pts / 5 * 80 = 20
        assert v.resilience_score == 20

    def test_degraded_worth_half(self):
        # 5 DEGRADED = 5*8 = 40 scenario pts -> int(40/80*80) = 40; +20 baseline = 60
        v = _vendor(outcomes=[ScenarioOutcome.DEGRADED] * 5)
        assert v.resilience_score == 60

    def test_mixed_outcomes(self):
        # 2 PASS (32) + 2 FAIL (0) + 1 DEGRADED (8) = 40 pts -> int(40/80*80)=40; +20=60
        v = _vendor(outcomes=[
            ScenarioOutcome.PASS, ScenarioOutcome.PASS,
            ScenarioOutcome.FAIL, ScenarioOutcome.FAIL,
            ScenarioOutcome.DEGRADED,
        ])
        assert v.resilience_score == 60

    def test_capped_at_100(self):
        v = _vendor(outcomes=[ScenarioOutcome.PASS] * 5)
        assert v.resilience_score <= 100


class TestOverallScore:
    def test_single_vendor(self):
        v = _vendor(criticality=100, outcomes=[ScenarioOutcome.PASS] * 5)
        report = ResilienceReport(repository="test/repo", vendor_results=[v])
        assert report.overall_score == 100

    def test_weighted_average(self):
        high = _vendor("stripe", criticality=90, outcomes=[ScenarioOutcome.PASS] * 5)   # 100
        low = _vendor("sendgrid", criticality=10, outcomes=[ScenarioOutcome.FAIL] * 5)  # 20
        report = ResilienceReport(repository="test/repo", vendor_results=[high, low])
        # weighted: (100*90 + 20*10) / 100 = 9200/100 = 92
        assert report.overall_score == 92

    def test_empty_vendors(self):
        report = ResilienceReport(repository="test/repo", vendor_results=[])
        assert report.overall_score == 0

    def test_failed_scenarios_list(self):
        v = _vendor(outcomes=[ScenarioOutcome.FAIL, ScenarioOutcome.PASS, ScenarioOutcome.DEGRADED])
        report = ResilienceReport(repository="test/repo", vendor_results=[v])
        failures = report.all_failed_scenarios
        assert len(failures) == 2
        assert all(name == "stripe" for name, _ in failures)
