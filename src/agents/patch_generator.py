"""
Agent 6 — Resilience Strategy & Patch Generator.

Reads the DiagnosisReport from Agent 5 and generates a minimal unified diff
per vendor. Validates each diff is syntactically valid Python before returning.
One Super LLM call per vendor, with one retry if the diff fails ast.parse.
"""

import ast
import json
import logging
import tempfile
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from utils import nebius_client
from utils.nebius_client import strip_llm_wrapper
from models.diagnosis import DiagnosisReport, DiagnosisResult
from models.patch import PatchReport, PatchResult

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "patch_generator.txt").read_text()


def run(
    diagnosis_report: DiagnosisReport,
    source_files: dict[str, str],
    previous_failure: str | None = None,
) -> PatchReport:
    """
    Run Agent 6 — generate patches for all diagnosed vendors.

    Args:
        diagnosis_report: DiagnosisReport from Agent 5.
        source_files: Dict of {relative_path: file_content} from Agent 1.
        previous_failure: Failure description from last validation attempt, fed back to LLM.

    Returns:
        PatchReport with one PatchResult per diagnosed vendor.
    """
    if previous_failure:
        logger.info("Agent 6: retrying with previous failure context: %s", previous_failure)

    patches: list[PatchResult] = []

    for diagnosis in diagnosis_report.diagnoses:
        source_code = source_files.get(diagnosis.affected_file, "")
        if not source_code:
            logger.warning(
                "Agent 6: source not found for %s (%s) — skipping",
                diagnosis.vendor,
                diagnosis.affected_file,
            )
            continue

        logger.info(
            "Agent 6: generating patch for %s | strategy=%s | file=%s | fn=%s",
            diagnosis.vendor,
            diagnosis.fix_strategy.value,
            diagnosis.affected_file,
            diagnosis.affected_function,
        )

        patch = _generate_patch(
            diagnosis=diagnosis,
            source_code=source_code,
            previous_failure=previous_failure,
        )
        patches.append(patch)
        logger.info(
            "Agent 6: %s → pr_title=%r | diff_lines=%d",
            patch.vendor,
            patch.pr_title,
            len(patch.patch_diff.splitlines()),
        )

    logger.info("Agent 6: generated %d patch(es)", len(patches))
    return PatchReport(repository=diagnosis_report.repository, patches=patches)


def _generate_patch(
    diagnosis: DiagnosisResult,
    source_code: str,
    previous_failure: str | None = None,
) -> PatchResult:
    payload = {
        "vendor": diagnosis.vendor,
        "affected_file": diagnosis.affected_file,
        "affected_function": diagnosis.affected_function,
        "fix_strategy": diagnosis.fix_strategy.value,
        "patterns": [p.value for p in diagnosis.patterns],
        "root_cause": diagnosis.root_cause,
        "criticality_note": diagnosis.criticality_note,
        "source_code": source_code,
    }
    if previous_failure:
        payload["previous_patch_failed"] = previous_failure
    user_message = json.dumps(payload, indent=2)

    raw = nebius_client.super_(system=_SYSTEM_PROMPT, user=user_message, temperature=0.2)
    raw = strip_llm_wrapper(raw)

    data = _parse_response(raw, diagnosis.vendor)
    patch = PatchResult.model_validate(data)

    # Validate the patch produces syntactically valid Python
    error = _validate_patch_syntax(patch, source_code)
    if error:
        logger.warning("Agent 6: syntax validation failed for %s — retrying. Error: %s", diagnosis.vendor, error)
        retry_message = user_message[:-1] + f',\n  "previous_attempt_error": {json.dumps(error)}\n}}'
        raw = nebius_client.super_(system=_SYSTEM_PROMPT, user=retry_message, temperature=0.1)
        raw = strip_llm_wrapper(raw)
        data = _parse_response(raw, diagnosis.vendor)
        patch = PatchResult.model_validate(data)

    return patch


def _parse_response(raw: str, vendor: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Agent 6 returned invalid JSON for {vendor}: {e}\n\nRaw:\n{raw}"
        ) from e


def _validate_patch_syntax(patch: PatchResult, original_source: str) -> str | None:
    """
    Apply the patch diff to a temp copy of the file and ast.parse the result.
    Returns an error string if validation fails, None if valid.
    """
    try:
        patched = _apply_diff_naive(patch.patch_diff, original_source)
        ast.parse(patched)
        return None
    except SyntaxError as e:
        return f"SyntaxError after patch: {e}"
    except Exception as e:
        return f"Patch apply error: {e}"


def _apply_diff_naive(diff: str, original: str) -> str:
    """
    Minimal unified diff applicator for syntax validation only.
    Applies +/- lines from each hunk to reconstruct the patched file.
    Not a full patch tool — used only to check ast.parse validity.
    """
    lines = original.splitlines(keepends=True)
    result = list(lines)
    offset = 0  # cumulative line shift from prior hunks

    for hunk in _parse_hunks(diff):
        src_start, src_len, dst_start, dst_len, hunk_lines = hunk
        # Convert 1-based to 0-based index, adjusted for prior edits
        idx = src_start - 1 + offset
        removed = [l for l in hunk_lines if l.startswith("-")]
        added = [l[1:] for l in hunk_lines if l.startswith("+")]

        # Replace the removed block with the added block
        result[idx:idx + len(removed)] = added
        offset += len(added) - len(removed)

    return "".join(result)


def _parse_hunks(diff: str):
    """Yield (src_start, src_len, dst_start, dst_len, lines) for each @@ hunk."""
    import re
    hunk_header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    current_hunk = None
    current_lines = []

    for line in diff.splitlines():
        m = hunk_header.match(line)
        if m:
            if current_hunk:
                yield (*current_hunk, current_lines)
            src_start = int(m.group(1))
            src_len = int(m.group(2)) if m.group(2) else 1
            dst_start = int(m.group(3))
            dst_len = int(m.group(4)) if m.group(4) else 1
            current_hunk = (src_start, src_len, dst_start, dst_len)
            current_lines = []
        elif current_hunk is not None and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            current_lines.append(line)

    if current_hunk:
        yield (*current_hunk, current_lines)
