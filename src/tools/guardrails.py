"""
Two-stage pre-flight guardrails.

shallow_check(repo)      — runs in main.py before any pipeline state is created.
                           Only uses GitHub API (no file contents). Catches obvious
                           user errors fast: bad token, wrong repo, not a Python repo.

deep_check(local_path)   — runs in _discover() after the repo is cloned locally.
                           Has full file access; checks Flask/FastAPI entry point,
                           HTTP vendors, and env-var-based URLs.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from github import GithubException

from tools.ast_scanner import scan_directory, scan_result_to_dict
from tools.github_client import get_repo, get_tree


@dataclass
class GuardrailResult:
    passed: bool
    message: str


def shallow_check(repo: str) -> GuardrailResult:
    """Token, repo access, Python files present, repo size — all via GitHub API."""
    for fn in [
        _check_token,
        lambda: _check_repo_accessible(repo),
        lambda: _check_python_repo(repo),
        lambda: _check_repo_size(repo),
    ]:
        result = fn()
        if not result.passed:
            return result
    return GuardrailResult(passed=True, message="Shallow checks passed.")


def deep_check(repo: str, local_path: str) -> GuardrailResult:
    """Flask/FastAPI entry point, HTTP vendors, env-var URLs — requires local clone."""
    for fn in [
        lambda: _check_flask_or_fastapi(local_path),
        lambda: _check_http_vendors(repo, local_path),
        lambda: _check_env_var_urls(repo, local_path),
    ]:
        result = fn()
        if not result.passed:
            return result
    return GuardrailResult(passed=True, message="Deep checks passed.")


# ── Shallow checks (GitHub API only) ─────────────────────────────────────────

def _check_token() -> GuardrailResult:
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT")):
        return GuardrailResult(
            passed=False,
            message="GITHUB_TOKEN is not set. Set it in your .env file before running GhostVendor.",
        )
    return GuardrailResult(passed=True, message="GitHub token present.")


def _check_repo_accessible(repo: str) -> GuardrailResult:
    try:
        get_repo(repo)
        return GuardrailResult(passed=True, message=f"Repo {repo} is accessible.")
    except GithubException as e:
        if e.status == 404:
            return GuardrailResult(
                passed=False,
                message=f"Repo '{repo}' not found. Check the owner/name and that your token has access.",
            )
        if e.status == 401:
            return GuardrailResult(
                passed=False,
                message="GitHub token is invalid or expired. Update GITHUB_TOKEN in your .env file.",
            )
        return GuardrailResult(
            passed=False,
            message=f"Could not access repo '{repo}': {e.data.get('message', str(e))}",
        )
    except Exception as e:
        return GuardrailResult(passed=False, message=f"GitHub API error: {e}")


def _check_python_repo(repo: str) -> GuardrailResult:
    try:
        tree = get_tree(repo)
        py_files = [p for p in tree if p.endswith(".py")]
    except Exception:
        py_files = []
    if not py_files:
        return GuardrailResult(
            passed=False,
            message=f"No Python (.py) files found in '{repo}'. GhostVendor only supports Python repos.",
        )
    return GuardrailResult(passed=True, message=f"Python repo confirmed ({len(py_files)} .py files).")


def _check_repo_size(repo: str) -> GuardrailResult:
    MAX_PY_FILES = 500
    try:
        tree = get_tree(repo)
        py_files = [p for p in tree if p.endswith(".py")]
    except Exception:
        return GuardrailResult(passed=True, message="Repo size check skipped.")
    if len(py_files) > MAX_PY_FILES:
        return GuardrailResult(
            passed=False,
            message=(
                f"Repo '{repo}' has {len(py_files)} Python files — exceeds the {MAX_PY_FILES} file limit. "
                "GhostVendor is designed for focused service repos, not monorepos."
            ),
        )
    return GuardrailResult(passed=True, message=f"Repo size OK ({len(py_files)} .py files).")


# ── Deep checks (local clone required) ───────────────────────────────────────

def _check_flask_or_fastapi(local_path: str) -> GuardrailResult:
    for py_file in Path(local_path).rglob("*.py"):
        if any(skip in py_file.parts for skip in (".venv", "venv", "__pycache__")):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            if "app.run(" in content or "uvicorn.run(" in content:
                return GuardrailResult(passed=True, message="Flask or FastAPI entry point detected.")
        except OSError:
            pass
    return GuardrailResult(
        passed=False,
        message=(
            "No Flask (app.run()) or FastAPI (uvicorn.run()) entry point found. "
            "GhostVendor currently supports Flask and FastAPI apps only."
        ),
    )


def _check_http_vendors(repo: str, local_path: str) -> GuardrailResult:
    findings = scan_result_to_dict(scan_directory(local_path))
    if not findings.get("http_calls"):
        return GuardrailResult(
            passed=False,
            message=(
                f"No outbound HTTP calls found in '{repo}'. "
                "GhostVendor needs at least one vendor integration to attack."
            ),
        )
    return GuardrailResult(passed=True, message=f"Found {len(findings['http_calls'])} outbound HTTP call(s).")


def _check_env_var_urls(repo: str, local_path: str) -> GuardrailResult:
    findings = scan_result_to_dict(scan_directory(local_path))
    env_driven = [c for c in findings.get("http_calls", []) if c.get("base_url_env")]
    if not env_driven:
        return GuardrailResult(
            passed=False,
            message=(
                f"No env-var-based vendor URLs found in '{repo}'. "
                "GhostVendor redirects vendors via environment variables (os.environ.get()). "
                "At least one HTTP call must use an env var for its base URL."
            ),
        )
    return GuardrailResult(passed=True, message=f"Found {len(env_driven)} env-var-driven vendor URL(s).")
