"""
Agent 4 — Resilience Verifier.

Sense → Reason → Act loop against a live running application.

The agent does NOT inherit a fixed attack plan. It observes real baseline behavior,
asks Nemotron Ultra to plan the attack strategy based on that evidence, then after
each scenario asks Ultra whether to continue, escalate, or stop early.

Flow per vendor:
  1. Launch Evil Twin
  2. Start target app with Twin URLs injected
  3. Run baseline — observe real HTTP response
  4. Ultra call (Mode 1): given baseline, plan the attack
  5. For each planned scenario:
     a. Activate chaos on the twin
     b. Fire the real endpoint on the app
     c. Observe: status, body, timing, exception
     d. Ultra call (Mode 2): continue / escalate / stop_early?
     e. Act on the decision
  6. Store VendorResult with all scenario evidence
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

import requests

from models.vendor_spec import VendorSpec, Vendor, Endpoint
from models.resilience_result import (
    ResilienceReport, VendorResult, ScenarioResult, ScenarioOutcome,
)
from tools.evil_twin_runner import EvilTwinManager, EvilTwinProcess
from tools.demo_app_runner import DemoAppProcess
from tools.repo_cloner import RepoInfo
from tools import nebius_client
from agents.twin_generator import EvilTwinArtifact


_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "resilience_verifier.txt").read_text()

_AVAILABLE_MODES = ["502_burst", "timeout", "malformed_json", "429_rate_limit", "empty_response"]

_FALLBACK_PAYLOAD = {"test": "ghostvendor_probe"}


def run(
    spec: VendorSpec,
    evil_twins: dict[str, EvilTwinArtifact],
    twin_manager: EvilTwinManager,
    repo_info: RepoInfo,
    source_files: dict[str, str] | None = None,
) -> ResilienceReport:

    twin_env = {a.base_url_env: a.local_base_url for a in evil_twins.values()}
    extra_env = {**repo_info.extra_env, **_fake_credentials(spec)}
    app_path = repo_info.local_path
    app_port = repo_info.port
    app_cmd = repo_info.start_command

    # Launch all Evil Twins upfront — app starts once with all twin URLs injected
    for artifact in evil_twins.values():
        logger.info("Launching Evil Twin: %s on port %d", artifact.vendor_name, artifact.port)
        twin_manager.launch(
            vendor_name=artifact.vendor_name,
            port=artifact.port,
            code=artifact.code,
        )
        twin = twin_manager.get(artifact.vendor_name)
        healthy = twin.is_healthy() if twin else False
        logger.info("Twin %s health check: %s", artifact.vendor_name, "OK" if healthy else "FAIL")

    # Synthesize valid request payloads per vendor from source code (Ultra: Mode 0)
    vendor_payloads: dict[str, dict] = {}
    if source_files:
        for vendor in spec.discovered_vendors:
            vendor_payloads[vendor.name] = _synthesize_payload(vendor, source_files)

    vendor_results: list[VendorResult] = []

    logger.info("Twin env injected: %s", list(twin_env.keys()))
    logger.info("Extra env injected: %s", list(extra_env.keys()))

    for vendor in spec.vendors_by_criticality():
        twin = twin_manager.get(vendor.name)
        if not twin:
            logger.warning("No Evil Twin for %s — skipping", vendor.name)
            continue

        payload = vendor_payloads.get(vendor.name, _FALLBACK_PAYLOAD)
        logger.info("=== Starting vendor: %s (score=%d) ===", vendor.name, vendor.criticality_score)
        logger.info("  app_route=%s payload=%s", vendor.app_route, payload)

        # Fresh app instance per vendor — prevents hung threads from one vendor bleeding into the next
        logger.info("Starting app: cmd=%s port=%d path=%s", app_cmd, app_port, app_path)
        app = DemoAppProcess(
            app_path=app_path,
            start_command=app_cmd,
            port=app_port,
            twin_env=twin_env,
            extra_env=extra_env,
        )
        with app:
            logger.info("App started on port %d", app_port)
            result = _verify_vendor(vendor, twin, app, payload)

        logger.info("%s: score=%d | failures=%d/%d", vendor.name, result.resilience_score, len(result.failed_scenarios), len(result.scenarios))
        vendor_results.append(result)

        # Stop twin after vendor test — clean shutdown, don't leave it running
        logger.info("Stopping twin for %s", vendor.name)
        twin.stop()

    return ResilienceReport(repository=spec.repository, vendor_results=vendor_results)


def _synthesize_payload(vendor: Vendor, source_files: dict[str, str]) -> dict:
    """
    Ultra call (Mode 0): given the route source code, synthesize a minimal valid request payload.
    Falls back to _FALLBACK_PAYLOAD if LLM fails or returns non-dict.
    """
    # Find source files likely containing this vendor's route handler
    route_clues = {
        path: content for path, content in source_files.items()
        if vendor.app_route.lstrip("/").split("/")[0] in content
        or any(kw in path.lower() for kw in ("route", "app", "main", "view", "api"))
    }

    if not route_clues:
        return _FALLBACK_PAYLOAD

    user_message = json.dumps({
        "mode": "payload_synthesis",
        "vendor": vendor.name,
        "app_route": vendor.app_route,
        "source_files": {k: v[:3000] for k, v in list(route_clues.items())[:5]},
    })

    try:
        raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.1)
        raw = _strip_llm_wrapper(raw)
        result = json.loads(raw)
        payload = result.get("payload", result)
        if isinstance(payload, dict):
            logger.info("Synthesized payload for %s: %s", vendor.name, payload)
            return payload
    except Exception as e:
        logger.warning("Payload synthesis failed for %s (%s), using fallback", vendor.name, e)

    return _FALLBACK_PAYLOAD


def _verify_vendor(
    vendor: Vendor,
    twin: EvilTwinProcess,
    app: DemoAppProcess,
    payload: dict,
) -> VendorResult:
    healthy = twin.is_healthy()
    logger.info("Twin %s pre-baseline health: %s", vendor.name, "OK" if healthy else "FAIL — twin not responding")

    try:
        twin.clear_chaos()
        logger.debug("Twin %s chaos cleared", vendor.name)
    except Exception as e:
        logger.debug("Twin %s clear_chaos failed (may be fine if no chaos active): %s", vendor.name, e)

    baseline_passed, baseline_observations, baseline_body, baseline_ms = _run_baseline(vendor, app, payload)
    logger.info("Baseline: %s — observations: %s", "PASS" if baseline_passed else "FAIL", baseline_observations)

    # If baseline timed out the demo app has a hung thread AND the twin has a stuck connection.
    # Restart the twin to free its worker before running chaos scenarios.
    if not baseline_passed and any("ReadTimeout" in str(o.get("exception", "")) for o in baseline_observations):
        logger.warning("Baseline timed out for %s — restarting twin to clear stuck connection", vendor.name)
        try:
            twin.stop()
            twin.start()
            logger.info("Twin %s restarted successfully", vendor.name)
        except Exception as e:
            logger.error("Twin %s restart failed: %s", vendor.name, e)

    attack_plan = _plan_attack(vendor, baseline_observations)
    logger.info("Attack plan (%d scenarios): %s", len(attack_plan), [s['mode'] for s in attack_plan])

    scenario_results = _execute_attack(vendor, attack_plan, twin, app, payload, baseline_body, baseline_ms)

    return VendorResult(
        vendor_name=vendor.name,
        criticality_score=vendor.criticality_score,
        baseline_passed=baseline_passed,
        scenarios=scenario_results,
    )


def _run_baseline(vendor: Vendor, app: DemoAppProcess, payload: dict) -> tuple[bool, list[dict], dict | None, float | None]:
    """
    Hit the app route in normal mode.
    Returns (passed, observations, baseline_body, baseline_ms).
    baseline_body and baseline_ms are used by _observe() for structural diff and timing delta.
    """
    observations = []
    try:
        status, body, elapsed_ms = app.post(vendor.app_route, payload)
        passed = status < 500
        observations.append({
            "app_route": vendor.app_route,
            "http_status": status,
            "response_body": str(body)[:300],
            "response_time_ms": round(elapsed_ms, 1),
            "passed": passed,
        })
        baseline_body = body if isinstance(body, dict) else None
        return passed, observations, baseline_body, elapsed_ms
    except Exception as e:
        logger.error("Baseline exception: %s: %s", type(e).__name__, e)
        observations.append({"app_route": vendor.app_route, "exception": type(e).__name__, "detail": str(e)[:200], "passed": False})
        return False, observations, None, None


def _plan_attack(vendor: Vendor, baseline_observations: list[dict]) -> list[dict]:
    """
    Ultra call (Mode 1): given baseline evidence, return an ordered attack plan.
    Falls back to default ordering if LLM fails.
    """
    user_message = json.dumps({
        "mode": "attack_planning",
        "vendor": {
            "name": vendor.name,
            "criticality_score": vendor.criticality_score,
            "endpoints": [
                {
                    "path": e.path,
                    "expected_resilience": e.expected_resilience.model_dump(),
                }
                for e in vendor.endpoints
            ],
        },
        "baseline": baseline_observations,
        "available_modes": _AVAILABLE_MODES,
    })

    try:
        raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.2)
        raw = _strip_llm_wrapper(raw)
        plan = json.loads(raw)
        return plan["attack_plan"]
    except Exception as e:
        logger.warning("Attack planning LLM failed (%s), using default order", e)
        return [{"mode": m, "reasoning": "default fallback"} for m in _AVAILABLE_MODES[:5]]


def _execute_attack(
    vendor: Vendor,
    attack_plan: list[dict],
    twin: EvilTwinProcess,
    app: DemoAppProcess,
    payload: dict,
    baseline_body: dict | None,
    baseline_ms: float | None,
) -> list[ScenarioResult]:
    """
    Sense→reason→act loop.
    After each scenario, Ultra decides: continue / escalate / stop_early.
    """
    completed: list[ScenarioResult] = []
    remaining = list(attack_plan)

    while remaining:
        planned = remaining.pop(0)
        mode = planned["mode"]

        result = _run_scenario(mode, vendor, twin, app, payload, baseline_body, baseline_ms)
        completed.append(result)

        detail = f"status={result.http_status}, time={result.response_time_ms:.0f}ms" if result.response_time_ms is not None else f"exception={result.exception or result.notes or 'unknown'}"
        logger.info("  %s: %s (%s)", mode, result.outcome.value, detail)

        if not remaining:
            break

        decision = _decide_next(vendor, completed, result, remaining, baseline_body, baseline_ms)
        action = decision.get("decision", "continue")
        reasoning = decision.get("reasoning", "")

        logger.info("  Ultra decision: %s — %s", action, reasoning)

        if action == "stop_early":
            break
        elif action == "escalate":
            next_mode = decision.get("next_mode")
            if next_mode and next_mode in _AVAILABLE_MODES:
                remaining.insert(0, {"mode": next_mode, "reasoning": "escalation by Ultra"})

    return completed


def _decide_next(
    vendor: Vendor,
    completed: list[ScenarioResult],
    last_result: ScenarioResult,
    remaining: list[dict],
    baseline_body: dict | None,
    baseline_ms: float | None,
) -> dict:
    """
    Ultra call (Mode 2): given what happened so far, decide the next move.
    Falls back to 'continue' if LLM fails.
    """
    user_message = json.dumps({
        "mode": "mid_attack_decision",
        "vendor": {
            "name": vendor.name,
            "criticality_score": vendor.criticality_score,
        },
        "baseline": {
            "response_body": baseline_body,
            "response_time_ms": round(baseline_ms, 1) if baseline_ms else None,
        },
        "completed": [
            {
                "mode": r.mode,
                "outcome": r.outcome.value,
                "http_status": r.http_status,
                "response_body": r.response_body[:200] if r.response_body else "",
                "response_time_ms": r.response_time_ms,
                "notes": r.notes,
            }
            for r in completed
        ],
        "last_result": {
            "mode": last_result.mode,
            "outcome": last_result.outcome.value,
            "http_status": last_result.http_status,
            "response_body": last_result.response_body[:200] if last_result.response_body else "",
            "response_time_ms": last_result.response_time_ms,
            "exception": last_result.exception,
            "notes": last_result.notes,
        },
        "remaining_plan": [r["mode"] for r in remaining],
        "available_modes": _AVAILABLE_MODES,
    })

    try:
        raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.1)
        raw = _strip_llm_wrapper(raw)
        return json.loads(raw)
    except Exception as e:
        logger.warning("Mid-attack decision LLM failed (%s), continuing", e)
        return {"decision": "continue", "next_mode": None, "reasoning": "fallback"}


def _run_scenario(
    mode: str,
    vendor: Vendor,
    twin: EvilTwinProcess,
    app: DemoAppProcess,
    payload: dict,
    baseline_body: dict | None,
    baseline_ms: float | None,
) -> ScenarioResult:
    """Activate chaos, fire the app endpoint, observe, clear chaos."""
    logger.info("  activating chaos mode=%s on %s twin (port=%d)", mode, vendor.name, twin.port)
    try:
        twin.activate_chaos(mode=mode)
        logger.debug("  chaos activated OK")
    except Exception as e:
        logger.error("  chaos activation FAILED: %s: %s", type(e).__name__, e)
        return ScenarioResult(
            mode=mode,
            outcome=ScenarioOutcome.FAIL,
            notes=f"Failed to activate chaos: {e}",
        )

    logger.debug("  firing app.post(%s)", vendor.app_route)
    result = _observe(mode, vendor, app, payload, baseline_body, baseline_ms)

    clear_failed = False
    try:
        twin.clear_chaos()
        logger.debug("  chaos cleared after %s", mode)
    except Exception as e:
        logger.warning("  clear_chaos after %s failed: %s", mode, e)
        clear_failed = True

    # Restart the twin if:
    # - clear_chaos timed out (worker blocked by async sleep still running), OR
    # - the app request itself timed out (demo app's keep-alive socket is still held open,
    #   occupying the twin's single uvicorn worker for subsequent chaos control calls)
    app_timed_out = result.exception == "requests.exceptions.Timeout"
    if clear_failed or app_timed_out:
        try:
            logger.warning("  restarting twin for %s (clear_failed=%s, app_timed_out=%s)", vendor.name, clear_failed, app_timed_out)
            twin.stop()
            twin.start()
            logger.info("  twin %s restarted OK", vendor.name)
        except Exception as e:
            logger.error("  twin %s restart failed: %s", vendor.name, e)

    return result


def _structural_diff(baseline: dict | None, chaos: object) -> str | None:
    """
    Compare top-level keys of chaos response against baseline.
    Returns a human-readable diff string if structure changed, else None.
    Skips comparison if either side is not a dict (non-JSON or baseline unknown).
    """
    if not isinstance(baseline, dict) or not isinstance(chaos, dict):
        return None
    baseline_keys = set(baseline.keys())
    chaos_keys = set(chaos.keys())
    missing = baseline_keys - chaos_keys
    added = chaos_keys - baseline_keys
    parts = []
    if missing:
        parts.append(f"missing keys: {sorted(missing)}")
    if added:
        parts.append(f"new keys: {sorted(added)}")
    return "; ".join(parts) if parts else None


def _extract_vendor_error_code(body) -> int | None:
    """
    Extract a vendor HTTP status code silently forwarded inside a 2xx response body.
    Catches patterns like {"code": 429}, {"status_code": 502}, {"http_status": 503}.
    Returns the code as int if found and >= 400, else None.
    """
    if not isinstance(body, dict):
        return None
    for key in ("code", "status_code", "http_status", "statusCode", "httpStatus"):
        val = body.get(key)
        if isinstance(val, int) and val >= 400:
            return val
    return None


_TIMING_SLOWDOWN_FACTOR = 3.0  # response_time > 3× baseline triggers DEGRADED


def _observe(mode: str, vendor: Vendor, app: DemoAppProcess, payload: dict, baseline_body: dict | None, baseline_ms: float | None) -> ScenarioResult:
    """Fire the app route under chaos and classify the outcome using 4 detection layers."""
    try:
        status, body, elapsed_ms = app.post(vendor.app_route, payload)

        if status >= 500:
            outcome = ScenarioOutcome.FAIL
            notes = f"App returned HTTP {status} — unhandled vendor failure"
        elif status >= 400:
            outcome = ScenarioOutcome.DEGRADED
            notes = f"App returned HTTP {status} — partial handling"
        else:
            body_str = str(body).lower()
            vendor_error_code = _extract_vendor_error_code(body)
            struct_diff = _structural_diff(baseline_body, body)
            timing_flag = (
                baseline_ms is not None
                and elapsed_ms > baseline_ms * _TIMING_SLOWDOWN_FACTOR
                and elapsed_ms > 500  # ignore sub-500ms noise
            )

            if any(kw in body_str for kw in ("error", "failed", "exception")):
                outcome = ScenarioOutcome.DEGRADED
                notes = "App returned 2xx but body contains error indicators"
            elif vendor_error_code is not None:
                outcome = ScenarioOutcome.DEGRADED
                notes = f"Silent failure: app forwarded vendor error code {vendor_error_code} inside 2xx body"
            elif struct_diff:
                outcome = ScenarioOutcome.DEGRADED
                notes = f"Response structure changed under chaos: {struct_diff}"
            elif timing_flag:
                outcome = ScenarioOutcome.DEGRADED
                notes = f"Response {elapsed_ms:.0f}ms vs baseline {baseline_ms:.0f}ms ({elapsed_ms/baseline_ms:.1f}× slowdown) — possible silent hang"
            else:
                outcome = ScenarioOutcome.PASS
                notes = "App handled failure gracefully"

        return ScenarioResult(
            mode=mode,
            outcome=outcome,
            http_status=status,
            response_body=str(body)[:500],
            response_time_ms=elapsed_ms,
            notes=notes,
        )

    except requests.exceptions.Timeout:
        logger.error("  %s: app request timed out (no timeout handling)", mode)
        return ScenarioResult(
            mode=mode,
            outcome=ScenarioOutcome.FAIL,
            exception="requests.exceptions.Timeout",
            notes="App request timed out — no timeout handling in app",
        )
    except requests.exceptions.ConnectionError as e:
        logger.error("  %s: connection error — %s", mode, e)
        return ScenarioResult(
            mode=mode,
            outcome=ScenarioOutcome.FAIL,
            exception=f"ConnectionError: {str(e)[:100]}",
            notes="App connection failed — possibly crashed",
        )
    except Exception as e:
        logger.error("  %s: unexpected exception %s: %s", mode, type(e).__name__, e)
        return ScenarioResult(
            mode=mode,
            outcome=ScenarioOutcome.FAIL,
            exception=type(e).__name__,
            notes=str(e)[:200],
        )


def _strip_llm_wrapper(raw: str) -> str:
    raw = raw.strip()
    if "<think>" in raw and "</think>" in raw:
        raw = raw[raw.index("</think>") + len("</think>"):].strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _fake_credentials(spec: VendorSpec) -> dict[str, str]:
    return {
        f"{v.name.upper()}_API_KEY": f"ghostvendor_fake_{v.name.lower()}"
        for v in spec.discovered_vendors
    }
