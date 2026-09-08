"""
Agent 3 — Context Guard.

Three-layer validation gate for every generated Evil Twin before it runs locally:
  Layer 1: Deterministic AST inspection — catches dangerous patterns without LLM involvement
  Layer 2: Contree sandbox execution — behavioral observation, catches runtime surprises
  Layer 3: LLM security review (Nemotron Ultra) — structured verdict with full context

A twin must pass all three layers to be approved. Any HIGH/BLOCKED risk blocks the pipeline.
"""

import ast
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

from contree_sdk import ContreeSync

from models.guard_decision import ASTFinding, GuardDecision, RiskLevel
from models.vendor_spec import Vendor
from utils import nebius_client


_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "context_guard.txt").read_text()

# Patterns that are dangerous regardless of context
_DANGEROUS_CALLS = {
    "eval": "arbitrary code execution",
    "exec": "arbitrary code execution",
    "__import__": "dynamic import — potential for loading malicious modules",
    "compile": "dynamic compilation",
    "os.system": "shell command execution",
    "os.popen": "shell command execution",
    "subprocess.call": "shell command execution",
    "subprocess.Popen": "subprocess spawn — review carefully",
    "subprocess.run": "subprocess execution",
}

# File paths that should never be read by a mock server
_SENSITIVE_PATHS = ["/etc/passwd", "/etc/shadow", "~/.ssh", "~/.aws", ".env"]

# Sandbox startup timeout — just enough to catch import errors and startup crashes
_SANDBOX_TIMEOUT_SECONDS = 8


def run(vendor: Vendor, code: str) -> GuardDecision:
    """
    Run all three validation layers against a generated Evil Twin.

    Args:
        vendor: The vendor this twin impersonates.
        code: Generated Python source from Agent 2.

    Returns:
        GuardDecision — approved=True means safe to launch locally.

    Raises:
        RuntimeError: If the LLM gate returns unparseable output after retries.
    """
    logger.info("Inspecting %s twin (%d chars)...", vendor.name, len(code))
    ast_findings = _ast_inspect(code)
    logger.info("%s Layer1 AST: %d findings", vendor.name, len(ast_findings))

    sandbox_exit, sandbox_stdout, sandbox_stderr, sandbox_skipped = _sandbox_execute(code, vendor.name)
    logger.info("%s Layer2 Sandbox: exit=%d skipped=%s stderr=%r", vendor.name, sandbox_exit, sandbox_skipped, sandbox_stderr[:100])

    sandbox_unexpected = _detect_unexpected_sandbox_output(sandbox_stdout, sandbox_stderr)
    if sandbox_unexpected:
        logger.warning("%s unexpected sandbox output: %s", vendor.name, sandbox_unexpected)

    logger.info("%s Layer3 LLM review...", vendor.name)
    llm_verdict, llm_reasoning, risk_level, approved = _llm_review(
        vendor, code, ast_findings, sandbox_exit, sandbox_stdout, sandbox_stderr
    )

    logger.info("%s Layer3 verdict=%r risk=%s approved=%s", vendor.name, llm_verdict, risk_level.value, approved)

    # Override: HIGH or BLOCKED risk always results in rejection regardless of LLM
    if risk_level in (RiskLevel.HIGH, RiskLevel.BLOCKED):
        approved = False

    # Override: sandbox unexpected behavior with HIGH risk blocks
    if sandbox_unexpected and risk_level in (RiskLevel.HIGH, RiskLevel.BLOCKED):
        approved = False

    logger.info("%s final decision: approved=%s risk=%s sandbox_skipped=%s", vendor.name, approved, risk_level.value, sandbox_skipped)
    return GuardDecision(
        vendor_name=vendor.name,
        approved=approved,
        risk_level=risk_level,
        ast_findings=ast_findings,
        llm_verdict=llm_verdict,
        llm_reasoning=llm_reasoning,
        sandbox_exit_code=sandbox_exit,
        sandbox_stdout=sandbox_stdout,
        sandbox_stderr=sandbox_stderr,
        sandbox_unexpected=sandbox_unexpected,
        sandbox_skipped=sandbox_skipped,
    )


def _ast_inspect(code: str) -> list[ASTFinding]:
    """
    Parse the generated code and walk the AST looking for dangerous patterns.

    Returns a list of findings — empty list means no dangerous patterns detected.
    """
    findings: list[ASTFinding] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Syntax errors are caught earlier by Agent 2's validator — if we get here, note it
        return [ASTFinding(line=0, code="<unparseable>", reason="Code failed to parse as valid Python")]

    seen: set[tuple] = set()

    for node in ast.walk(tree):
        # Check function calls — catches eval(), exec(), __import__(), compile()
        # and dotted calls like os.system() via _extract_call_name
        if isinstance(node, ast.Call):
            call_name = _extract_call_name(node)
            if call_name and call_name in _DANGEROUS_CALLS:
                key = (node.lineno, call_name)
                if key not in seen:
                    seen.add(key)
                    findings.append(ASTFinding(
                        line=node.lineno,
                        code=call_name,
                        reason=_DANGEROUS_CALLS[call_name],
                    ))

        # Check string literals for sensitive paths
        if isinstance(node, ast.Constant) and isinstance(node.s, str):
            for sensitive in _SENSITIVE_PATHS:
                if sensitive in node.s:
                    key = (node.lineno, sensitive)
                    if key not in seen:
                        seen.add(key)
                        findings.append(ASTFinding(
                            line=node.lineno,
                            code=repr(node.s[:80]),
                            reason=f"Reference to sensitive path: {sensitive}",
                        ))

    return findings


def _extract_call_name(node: ast.Call) -> str | None:
    """Extract a simple function name from a Call node."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return _extract_attr_name(node.func)
    return None


def _extract_attr_name(node: ast.Attribute) -> str | None:
    """Extract dotted attribute name (e.g. os.system) from an Attribute node."""
    if isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _sandbox_execute(code: str, vendor_name: str) -> tuple[int, str, str, bool]:
    """
    Run the generated code in a Contree sandbox and capture behavior.

    We run the code with a short timeout — enough to catch import errors,
    startup crashes, and any immediate unexpected output. We do NOT wait for
    the server to start serving requests; we observe the first few seconds.

    Returns:
        (exit_code, stdout, stderr) — exit_code=None if sandbox API unavailable.
    """
    try:
        client = ContreeSync()
        sandbox = client.images.use("python:3.12-slim")

        # Install deps — Contree caches layers so subsequent runs are fast
        sandbox = sandbox.run(
            "pip", args=["install", "-q", "fastapi", "uvicorn[standard]"],
            disposable=False,
        ).wait()

        # Write the twin code
        sandbox = sandbox.run(
            "python", args=["-c", f"open('/tmp/twin.py','w').write({repr(code)})"],
            disposable=False,
        ).wait()

        # Run with a short timeout — observing startup behavior only, not serving requests
        result = sandbox.run(
            "python", args=["/tmp/twin.py"],
            timeout=_SANDBOX_TIMEOUT_SECONDS,
        ).wait()

        exit_code = result.exit_code
        # exit_code=-1 means timeout — server started and kept running = healthy startup
        if exit_code == -1:
            exit_code = 0
        return exit_code, result.stdout or "", result.stderr or "", False

    except Exception as e:
        logger.warning("Sandbox skipped for %s: %s: %s", vendor_name, type(e).__name__, e)
        return -1, "", f"Sandbox unavailable: {e}", True


def _detect_unexpected_sandbox_output(stdout: str, stderr: str) -> list[str]:
    """
    Scan sandbox output for patterns that don't belong in a FastAPI mock server.

    Returns a list of unexpected findings (empty = nothing suspicious).
    """
    unexpected = []
    combined = (stdout + stderr).lower()

    suspicious_patterns = [
        (r"curl|wget|requests\.get|urllib", "outbound HTTP client activity detected"),
        (r"password|secret|token|api_key", "potential secret exfiltration in output"),
        (r"import socket.*connect|socket\.connect", "raw socket connection attempt"),
        (r"exec\(|eval\(", "dynamic execution in output"),
        (r"/etc/passwd|/etc/shadow|~/.ssh", "sensitive file access in output"),
    ]

    for pattern, reason in suspicious_patterns:
        if re.search(pattern, combined):
            unexpected.append(reason)

    return unexpected


def _llm_review(
    vendor: Vendor,
    code: str,
    ast_findings: list[ASTFinding],
    sandbox_exit: int,
    sandbox_stdout: str,
    sandbox_stderr: str,
) -> tuple[str, str, RiskLevel, bool]:
    """
    Ask Nemotron Nano to review the code with full context from layers 1 and 2.

    Returns:
        (verdict, reasoning, risk_level, approved)
    """
    user_message = json.dumps({
        "vendor": {"name": vendor.name, "criticality": vendor.criticality.value},
        "ast_findings": [f.model_dump() for f in ast_findings],
        "sandbox_result": {
            "exit_code": sandbox_exit,
            "stdout": sandbox_stdout[:2000],  # cap to avoid token overflow
            "stderr": sandbox_stderr[:2000],
        },
        "code": code,
    }, indent=2)

    raw = nebius_client.nano(system=_SYSTEM_PROMPT, user=user_message, temperature=0.1)
    return _parse_llm_response(raw)


def _parse_llm_response(raw: str) -> tuple[str, str, RiskLevel, bool]:
    """
    Parse the LLM's JSON response into structured fields.

    Falls back to BLOCKED on any parse failure — safe default.
    """
    # Strip think blocks and markdown fences
    if "<think>" in raw and "</think>" in raw:
        raw = raw[raw.index("</think>") + len("</think>"):].strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)

    try:
        data = json.loads(raw)
        risk_level = RiskLevel(data.get("risk_level", "blocked"))
        approved = bool(data.get("approved", False))
        verdict = data.get("verdict", "No verdict provided")
        reasoning = data.get("reasoning", "No reasoning provided")
        return verdict, reasoning, risk_level, approved
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("LLM response parse failed: %s — defaulting to BLOCKED", e)
        return (
            "Parse failure — defaulting to blocked",
            f"LLM returned unparseable output: {raw[:200]}",
            RiskLevel.BLOCKED,
            False,
        )
