"""
Integration tests for the state machine orchestration layer.

All agents and external calls are mocked. Tests assert state transitions
and artifact handoffs — not LLM output content.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from pipeline.state_machine import StateMachine, State
from models.vendor_spec import VendorSpec
from models.resilience_result import (
    ResilienceReport, VendorResult, ScenarioResult, ScenarioOutcome,
)


def _make_vendor_spec():
    return VendorSpec.model_validate({
        "repository": "owner/repo",
        "discovered_vendors": [{
            "name": "stripe",
            "criticality": "critical",
            "criticality_score": 90,
            "base_url_env": "STRIPE_BASE_URL",
            "app_route": "/charge",
            "endpoints": [{
                "path": "/v1/charges",
                "method": "POST",
                "criticality": "critical",
                "normal_contract": {"success_status": 200, "response_type": "json"},
                "expected_resilience": {"graceful_degradation": True, "max_retry_attempts": 3},
            }],
        }]
    })


def _make_resilience_report(outcome: ScenarioOutcome = ScenarioOutcome.FAIL):
    return ResilienceReport(
        repository="owner/repo",
        vendor_results=[VendorResult(
            vendor_name="stripe",
            criticality_score=90,
            baseline_passed=True,
            scenarios=[ScenarioResult(mode="timeout", outcome=outcome)],
        )]
    )


def _make_machine():
    return StateMachine(repo="owner/repo", triggered_by="testuser")


class TestHappyPath:
    """Pipeline reaches DONE when all agents succeed and patch validates."""

    def test_transitions_to_done(self):
        machine = _make_machine()

        with (
            patch.object(machine, "_discover") as mock_discover,
            patch.object(machine, "_attack") as mock_attack,
            patch.object(machine, "_guard") as mock_guard,
            patch.object(machine, "_verify") as mock_verify,
            patch.object(machine, "_diagnose") as mock_diagnose,
            patch.object(machine, "_remediate") as mock_remediate,
            patch.object(machine, "_validate") as mock_validate,
        ):
            def do_discover():
                machine.artifacts.vendor_spec = _make_vendor_spec()
                machine.transition(State.ATTACK)

            def do_attack():
                machine.transition(State.GUARD)

            def do_guard():
                machine.transition(State.VERIFY)

            def do_verify():
                machine.artifacts.resilience_report = _make_resilience_report(ScenarioOutcome.FAIL)
                machine.artifacts.resilience_score_before = 23
                machine.transition(State.DIAGNOSE)

            def do_diagnose():
                machine.transition(State.REMEDIATE)

            def do_remediate():
                machine.transition(State.VALIDATE)

            def do_validate():
                machine.artifacts.resilience_score_after = 81
                machine.artifacts.pr_url = "https://github.com/owner/repo/pull/99"
                machine.transition(State.DONE)

            mock_discover.side_effect = do_discover
            mock_attack.side_effect = do_attack
            mock_guard.side_effect = do_guard
            mock_verify.side_effect = do_verify
            mock_diagnose.side_effect = do_diagnose
            mock_remediate.side_effect = do_remediate
            mock_validate.side_effect = do_validate

            machine.run()

        assert machine.state == State.DONE
        assert machine.artifacts.pr_url == "https://github.com/owner/repo/pull/99"
        assert machine.artifacts.resilience_score_after == 81


class TestFindingsFallback:
    """Pipeline opens findings PR and reaches FAILED when max retries exhausted."""

    def test_transitions_to_failed_after_max_retries(self):
        machine = _make_machine()
        machine.artifacts.resilience_report = _make_resilience_report(ScenarioOutcome.FAIL)
        machine.artifacts.resilience_score_before = 20

        with (
            patch.object(machine, "_discover") as mock_discover,
            patch.object(machine, "_attack") as mock_attack,
            patch.object(machine, "_guard") as mock_guard,
            patch.object(machine, "_verify") as mock_verify,
            patch.object(machine, "_diagnose") as mock_diagnose,
            patch.object(machine, "_remediate") as mock_remediate,
            patch.object(machine, "_validate") as mock_validate,
            patch.object(machine, "_open_findings_pr") as mock_findings_pr,
        ):
            def do_discover():
                machine.artifacts.vendor_spec = _make_vendor_spec()
                machine.transition(State.ATTACK)

            def do_attack():
                machine.transition(State.GUARD)

            def do_guard():
                machine.transition(State.VERIFY)

            def do_verify():
                machine.artifacts.resilience_report = _make_resilience_report(ScenarioOutcome.FAIL)
                machine.artifacts.resilience_score_before = 20
                machine.transition(State.DIAGNOSE)

            def do_diagnose():
                machine.transition(State.REMEDIATE)

            def do_remediate():
                machine.transition(State.VALIDATE)

            def do_validate():
                # Simulate max retries exhausted -> fall to FAILED
                machine.fail("Validation failed after 3 retries")

            mock_discover.side_effect = do_discover
            mock_attack.side_effect = do_attack
            mock_guard.side_effect = do_guard
            mock_verify.side_effect = do_verify
            mock_diagnose.side_effect = do_diagnose
            mock_remediate.side_effect = do_remediate
            mock_validate.side_effect = do_validate
            mock_findings_pr.return_value = None

            machine.run()

        assert machine.state == State.FAILED
        mock_findings_pr.assert_called_once()
