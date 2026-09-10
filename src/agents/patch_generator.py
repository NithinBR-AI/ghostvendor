"""
Agent 6 — Resilience Strategy & Patch Generator.

Asks the LLM for a complete rewritten file (fixed_source), validates it with
ast.parse, then computes the unified diff in Python using difflib. No LLM diff
generation — diffs are always correct by construction.

Retry contract: up to MAX_ATTEMPTS attempts per vendor. Each attempt is one LLM
call. JSON parse errors AND syntax errors both feed into the next attempt's context
so the model has compounding information. If all attempts are exhausted, the vendor
is skipped with an empty diff — never a crash, never broken code written to disk.
"""

import ast
import difflib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from utils import nebius_client
from utils.nebius_client import strip_llm_wrapper, deepseek_pro
from models.diagnosis import DiagnosisReport, DiagnosisResult
from models.patch import PatchReport, PatchResult, ExtraPatch

_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "patch_generator.txt").read_text()

MAX_ATTEMPTS = 3


def _resolve_affected_file(affected: str, source_files: dict[str, str]) -> tuple[str, str] | None:
    """
    Return (canonical_key, source_code) for the affected file.
    Tries exact match first (after forward-slash normalization), then basename match.
    Returns None if no match — caller skips the vendor rather than crashing.
    """
    norm = affected.replace("\\", "/")
    # Exact match
    for k, v in source_files.items():
        if k.replace("\\", "/") == norm:
            return k, v
    # Basename fallback — handles minor path prefix differences
    stem = norm.split("/")[-1]
    candidates = [(k, v) for k, v in source_files.items() if k.replace("\\", "/").split("/")[-1] == stem]
    if len(candidates) == 1:
        logger.info("Agent 6: resolved %s by basename → %s", affected, candidates[0][0])
        return candidates[0]
    if len(candidates) > 1:
        logger.warning("Agent 6: ambiguous basename %s matches %d files — skipping", stem, len(candidates))
    return None


def run(
    diagnosis_report: DiagnosisReport,
    source_files: dict[str, str],
    previous_failure: str | None = None,
    skip_vendors: set[str] | None = None,
) -> PatchReport:
    if previous_failure:
        logger.info("Agent 6: retrying with previous failure context: %s", previous_failure)
    if skip_vendors:
        logger.info("Agent 6: skipping already-validated vendors: %s", skip_vendors)

    # Build a per-vendor failure index so each vendor only sees its own prior failure context.
    # previous_failure is the full failure_summary string from ValidationResult — parse it into
    # per-vendor entries by splitting on "; " and matching the "VendorName/" prefix.
    vendor_failure_context: dict[str, str] = {}
    if previous_failure:
        for segment in previous_failure.split("; "):
            segment = segment.strip()
            if not segment:
                continue
            for diag in diagnosis_report.diagnoses:
                if segment.startswith(diag.vendor + "/") or segment.startswith(diag.vendor + ":"):
                    vendor_failure_context[diag.vendor] = vendor_failure_context.get(diag.vendor, "") + segment + "; "
                    break

    patches: list[PatchResult] = []

    for diagnosis in diagnosis_report.diagnoses:
        if skip_vendors and diagnosis.vendor in skip_vendors:
            logger.info("Agent 6: %s already validated — skipping re-patch", diagnosis.vendor)
            continue

        resolved = _resolve_affected_file(diagnosis.affected_file, source_files)
        if resolved is None:
            logger.warning("Agent 6: source not found for %s (%s) — skipping", diagnosis.vendor, diagnosis.affected_file)
            continue
        canonical_key, source_code = resolved

        logger.info(
            "Agent 6: generating patch for %s | strategy=%s | file=%s | fn=%s",
            diagnosis.vendor, diagnosis.fix_strategy.value, diagnosis.affected_file, diagnosis.affected_function,
        )

        # Per-vendor context preferred; fall back to full summary so no vendor loses failure context
        # when its prefix didn't parse (e.g. state machine used a different format string).
        per_vendor_failure = vendor_failure_context.get(diagnosis.vendor) or previous_failure
        patch = _generate_patch(
            diagnosis=diagnosis,
            source_code=source_code,
            source_files=source_files,
            previous_failure=per_vendor_failure,
            canonical_key=canonical_key,
        )
        patches.append(patch)
        logger.info("Agent 6: %s → pr_title=%r | diff_lines=%d", patch.vendor, patch.pr_title, len(patch.patch_diff.splitlines()))

    logger.info("Agent 6: generated %d patch(es)", len(patches))
    return PatchReport(repository=diagnosis_report.repository, patches=patches)


def _generate_patch(
    diagnosis: DiagnosisResult,
    source_code: str,
    source_files: dict[str, str] | None = None,
    previous_failure: str | None = None,
    canonical_key: str | None = None,
) -> PatchResult:
    affected_module = Path(diagnosis.affected_file).stem
    caller_files: dict[str, str] = {}
    if source_files:
        affected_norm = (canonical_key or diagnosis.affected_file).replace("\\", "/")
        for path, content in source_files.items():
            norm_path = path.replace("\\", "/")
            if norm_path == affected_norm:
                continue
            # Only include files that actually import this module — not every file that
            # happens to contain the stem string as a substring (Issue 6).
            if (f"from {affected_module} import" in content or
                    f"import {affected_module}" in content or
                    f"from .{affected_module} import" in content):
                caller_files[path] = content[:4000]

    base_payload = {
        "vendor": diagnosis.vendor,
        "affected_file": diagnosis.affected_file,
        "affected_function": diagnosis.affected_function,
        "fix_strategy": diagnosis.fix_strategy.value,
        "patterns": [p.value for p in diagnosis.patterns],
        "root_cause": diagnosis.root_cause,
        "criticality_note": diagnosis.criticality_note,
        "source_code": source_code,
    }
    if caller_files:
        base_payload["caller_files"] = caller_files
    if previous_failure:
        base_payload["previous_patch_failed"] = previous_failure

    # Unified attempt loop — JSON errors, syntax errors, and zero-diff results
    # all feed the next attempt with compounding context.
    error_context: str | None = None
    data: dict | None = None
    fixed_source: str = ""

    for attempt in range(MAX_ATTEMPTS):
        try:
            raw = _call_llm_raw(base_payload, diagnosis.vendor, error_context)
            data = _parse_json(raw, diagnosis.vendor)
        except ValueError as e:
            error_context = f"Attempt {attempt + 1} JSON error: {e}"
            logger.warning("Agent 6: %s attempt %d/%d JSON error — %s", diagnosis.vendor, attempt + 1, MAX_ATTEMPTS, e)
            continue

        fixed_source = data.get("fixed_source", "")
        syntax_err = _validate_syntax(fixed_source, diagnosis.vendor)
        if syntax_err:
            error_context = f"Attempt {attempt + 1} syntax error in fixed_source: {syntax_err}"
            logger.warning("Agent 6: %s attempt %d/%d syntax error — %s", diagnosis.vendor, attempt + 1, MAX_ATTEMPTS, syntax_err)
            data = None
            fixed_source = ""
            continue

        # Reject zero-diff patches — model returned the original file verbatim (Issue 4).
        # A patch with no actual changes is silently broken; the affected_function was never touched.
        trial_diff = _compute_diff(source_code, fixed_source, diagnosis.affected_file)
        if not trial_diff.strip():
            error_context = (
                f"Attempt {attempt + 1}: fixed_source is identical to source_code — no changes were made. "
                f"You MUST modify {diagnosis.affected_function} in the file to implement the fix strategy. "
                "Return a fixed_source that is genuinely different from the original."
            )
            logger.warning("Agent 6: %s attempt %d/%d produced zero diff — retrying", diagnosis.vendor, attempt + 1, MAX_ATTEMPTS)
            data = None
            fixed_source = ""
            continue

        logger.info("Agent 6: %s valid on attempt %d/%d", diagnosis.vendor, attempt + 1, MAX_ATTEMPTS)
        break

    if not data or not fixed_source:
        logger.error("Agent 6: all %d attempts failed for %s — skipping vendor", MAX_ATTEMPTS, diagnosis.vendor)
        return _skip_result(diagnosis)

    patch_diff = _compute_diff(source_code, fixed_source, diagnosis.affected_file)

    # Validate and normalize extra_patches — model may return arbitrary path strings (Issue 3).
    extra_patches: list[ExtraPatch] = []
    known_files = {k.replace("\\", "/"): k for k in (source_files or {})}
    known_basenames = {k.split("/")[-1]: k for k in known_files}

    for ep in data.get("extra_patches", []):
        ep_file_raw = ep.get("affected_file", "")
        ep_source = ep.get("fixed_source", "")
        if not ep_file_raw or not ep_source:
            continue

        ep_norm = ep_file_raw.replace("\\", "/")
        # Resolve against known source files — exact, then basename
        ep_file = ep_norm
        if ep_norm not in known_files:
            stem = ep_norm.split("/")[-1]
            if stem in known_basenames:
                ep_file = known_basenames[stem].replace("\\", "/")
                logger.info("Agent 6: extra_patch path %s resolved to %s by basename", ep_file_raw, ep_file)
            else:
                logger.warning("Agent 6: extra_patch %s not in repo — dropping", ep_file_raw)
                continue

        ep_err = _validate_syntax(ep_source, f"{diagnosis.vendor}/{ep_file}")
        if ep_err:
            logger.warning("Agent 6: extra_patch %s invalid syntax — dropping. %s", ep_file, ep_err)
            continue
        extra_patches.append(ExtraPatch(affected_file=ep_file, fixed_source=ep_source))
        logger.info("Agent 6: extra patch accepted for %s", ep_file)

    return PatchResult(
        vendor=data.get("vendor", diagnosis.vendor),
        affected_file=canonical_key or data.get("affected_file", diagnosis.affected_file),
        fix_strategy=data.get("fix_strategy", diagnosis.fix_strategy.value),
        patch_diff=patch_diff,
        fixed_source=fixed_source,
        extra_patches=extra_patches,
        pr_title=data.get("pr_title", f"fix({diagnosis.vendor.lower()}): resilience patch"),
        pr_description=data.get("pr_description", ""),
    )


def _skip_result(diagnosis: DiagnosisResult) -> PatchResult:
    """Return an empty-diff result when all attempts are exhausted. Never crashes the pipeline."""
    return PatchResult(
        vendor=diagnosis.vendor,
        affected_file=diagnosis.affected_file,
        fix_strategy=diagnosis.fix_strategy.value,
        patch_diff="",
        fixed_source="",
        extra_patches=[],
        pr_title=f"fix({diagnosis.vendor.lower()}): resilience patch (generation failed)",
        pr_description="Agent 6 could not produce a valid patch after 3 attempts.",
    )


def _call_llm_raw(payload: dict, vendor: str, error_context: str | None = None) -> str:
    """Single LLM call. No internal retry. Error context from prior attempts is appended to payload."""
    msg = dict(payload)
    if error_context:
        msg["previous_attempt_error"] = error_context
    raw = deepseek_pro(system=_SYSTEM_PROMPT, user=json.dumps(msg, indent=2), temperature=0.2, max_tokens=16384)
    logger.debug("Agent 6 raw for %s (%d chars): %.300s", vendor, len(raw or ""), raw or "")
    return strip_llm_wrapper(raw or "")


def _parse_json(raw: str, vendor: str) -> dict:
    """Parse JSON from LLM output. Attempts repair before giving up. Raises ValueError on failure."""
    if not raw.strip():
        raise ValueError("empty response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON after repair for {vendor}: {e}") from e


def _repair_json(raw: str) -> str:
    """
    Best-effort JSON repair:
    1. Replace unescaped control characters inside string values (literal newlines/tabs).
    2. Auto-close truncated JSON by appending missing closing quotes, brackets, and braces.
    3. Auto-close unclosed objects inside arrays before processing the closing bracket.
    """
    result = []
    in_string = False
    stack: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and in_string:
            result.append(ch)
            i += 1
            if i < len(raw):
                next_ch = raw[i]
                # JSON only recognises these escape sequences — anything else is a bare backslash
                # (e.g. Windows paths in comments). Escape it so the JSON parser doesn't choke.
                if next_ch not in ('"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u'):
                    result.append('\\')
                result.append(next_ch)
                i += 1
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if in_string and ch == '\n':
            result.append('\\n')
            i += 1
            continue
        if in_string and ch == '\t':
            result.append('\\t')
            i += 1
            continue
        if in_string and ch == '\r':
            i += 1
            continue
        if not in_string:
            if ch in ('{', '['):
                stack.append(ch)
            elif ch == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif ch == ']':
                while stack and stack[-1] == '{':
                    result.append('}')
                    stack.pop()
                if stack and stack[-1] == '[':
                    stack.pop()
        result.append(ch)
        i += 1

    if in_string:
        result.append('"')
    for opener in reversed(stack):
        result.append('}' if opener == '{' else ']')

    return ''.join(result)


def _validate_syntax(source: str, vendor: str) -> str | None:
    if not source or not source.strip():
        return f"fixed_source is empty for {vendor}"
    try:
        ast.parse(source)
        return None
    except SyntaxError as e:
        return f"SyntaxError: {e}"


def _compute_diff(original: str, fixed: str, file_path: str) -> str:
    original_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        fixed_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    return "".join(diff)


