"""
Agent 5 — Runtime Debugger.

Reads all failed/degraded scenarios from the ResilienceReport and performs
root cause analysis per vendor. Produces a DiagnosisReport that Agent 6
uses to generate patches.

One Ultra LLM call per vendor with failures — precise, cites source code,
names the exact fix strategy.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from tools import nebius_client
from tools.nebius_client import strip_llm_wrapper
from models.resilience_result import ResilienceReport, ScenarioResult
from models.diagnosis import DiagnosisReport, DiagnosisResult

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "runtime_debugger.txt").read_text()


def run(report: ResilienceReport, source_files: dict[str, str]) -> DiagnosisReport:
    """
    Run Agent 5 — diagnose root causes for all vendor failures.

    Args:
        report: ResilienceReport from Agent 4 containing all scenario results.
        source_files: Dict of {relative_path: file_content} from Agent 1.

    Returns:
        DiagnosisReport with one DiagnosisResult per vendor that had failures.
    """
    diagnoses: list[DiagnosisResult] = []

    for vendor_result in report.vendor_results:
        failed = vendor_result.failed_scenarios
        if not failed:
            logger.info("Agent 5: %s — no failures, skipping", vendor_result.vendor_name)
            continue

        logger.info(
            "Agent 5: diagnosing %s — %d failure(s): %s",
            vendor_result.vendor_name,
            len(failed),
            [s.mode for s in failed],
        )

        diagnosis = _diagnose_vendor(
            vendor_name=vendor_result.vendor_name,
            criticality_score=vendor_result.criticality_score,
            failed_scenarios=failed,
            source_files=source_files,
        )
        diagnoses.append(diagnosis)
        logger.info(
            "Agent 5: %s → patterns=%s strategy=%s file=%s fn=%s",
            diagnosis.vendor,
            [p.value for p in diagnosis.patterns],
            diagnosis.fix_strategy.value,
            diagnosis.affected_file,
            diagnosis.affected_function,
        )

    logger.info("Agent 5: diagnosed %d vendor(s)", len(diagnoses))
    return DiagnosisReport(repository=report.repository, diagnoses=diagnoses)


def _diagnose_vendor(
    vendor_name: str,
    criticality_score: int,
    failed_scenarios: list[ScenarioResult],
    source_files: dict[str, str],
) -> DiagnosisResult:
    user_message = json.dumps(
        {
            "vendor": {"name": vendor_name, "criticality_score": criticality_score},
            "source_files": source_files,
            "failed_scenarios": [
                {
                    "mode": s.mode,
                    "outcome": s.outcome.value,
                    "http_status": s.http_status,
                    "response_body": s.response_body[:300] if s.response_body else "",
                    "exception": s.exception,
                    "response_time_ms": s.response_time_ms,
                    "notes": s.notes,
                }
                for s in failed_scenarios
            ],
        },
        indent=2,
    )

    raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.1)
    raw = strip_llm_wrapper(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Agent 5 returned invalid JSON for {vendor_name}: {e}\n\nRaw:\n{raw}"
        ) from e

    try:
        return DiagnosisResult.model_validate(data)
    except Exception as e:
        raise ValueError(
            f"Agent 5 output failed schema validation for {vendor_name}: {e}\n\nData:\n{data}"
        ) from e


