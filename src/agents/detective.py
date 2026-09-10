"""
Agent 1 — Vendor & Repository Detective.

Two-layer dependency discovery:
  Layer 1 (deterministic): AST scanner finds every outbound HTTP call and env var reference.
  Layer 2 (semantic):      Nemotron Ultra identifies the vendor, contract, criticality, and chaos scenarios.

The LLM enriches what the AST proves — it never invents a dependency.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from tools.ast_scanner import scan_directory, scan_result_to_dict
from tools.github_client import get_repo, get_tree, get_file
from utils import nebius_client
from utils.nebius_client import strip_llm_wrapper
from models.vendor_spec import VendorSpec


_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "detective.txt").read_text()

# Source files worth sending to the LLM for semantic enrichment
_INTERESTING_PATHS = {"clients", "routes", "config", "app", "services", "integrations"}


def _is_interesting(path: str) -> bool:
    """Return True for source files that likely contain vendor integration logic."""
    parts = Path(path).parts
    if any(p in _INTERESTING_PATHS for p in parts):
        return True
    name = Path(path).stem.lower()
    return any(kw in name for kw in ("client", "vendor", "api", "integration", "service", "provider", "gateway"))


def _collect_source_files(repo_path: str, local: bool = False) -> dict[str, str]:
    """
    Collect contents of interesting source files.

    Args:
        repo_path: Local directory path (local=True) or GitHub owner/repo (local=False).
        local: If True, read from filesystem. If False, fetch from GitHub API.

    Returns:
        Dict of {relative_path: file_content}.
    """
    files: dict[str, str] = {}

    if local:
        root = Path(repo_path)
        for py_file in root.rglob("*.py"):
            parts = py_file.parts
            if any(p in parts for p in (".venv", "venv", "__pycache__")):
                continue
            rel = str(py_file.relative_to(root))
            if _is_interesting(rel):
                try:
                    files[rel] = py_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
    else:
        tree = get_tree(repo_path)
        for item in tree:
            if item.get("type") != "blob" or not item["path"].endswith(".py"):
                continue
            if _is_interesting(item["path"]):
                try:
                    files[item["path"]] = get_file(repo_path, item["path"])
                except Exception:
                    pass

    return files


def run(repo: str, local_path: str | None = None) -> tuple[VendorSpec, dict[str, str]]:
    """
    Run Agent 1 — discover vendors, enrich semantically, return a validated VendorSpec.

    Args:
        repo: GitHub repository identifier (owner/name).
        local_path: If provided, scan the local clone instead of fetching from GitHub.
                    Used when ghostvendor-demo-app is cloned locally for faster iteration.

    Returns:
        VendorSpec — the validated vendor_spec.json data contract.

    Raises:
        ValueError: If the LLM response cannot be parsed or validated.
    """
    scan_root = local_path if local_path else _clone_or_fetch(repo)
    logger.info("scan_root=%s", scan_root)

    # Layer 1: deterministic AST scan
    scan_result = scan_directory(scan_root)
    ast_findings = scan_result_to_dict(scan_result)
    logger.info("AST scan: %d HTTP calls, %d env vars", len(ast_findings.get('http_calls', [])), len(ast_findings.get('env_vars', [])))

    if not ast_findings["http_calls"]:
        raise ValueError(f"No outbound HTTP calls found in {repo}. Nothing to attack.")

    # Collect source files for LLM context
    source_files = _collect_source_files(scan_root, local=True)
    logger.info("Source files collected (%d): %s", len(source_files), list(source_files.keys()))

    # Layer 2: LLM semantic enrichment
    logger.info("Calling Ultra for vendor enrichment...")
    user_message = json.dumps({
        "repository": repo,
        "ast_findings": ast_findings,
        "source_files": source_files,
    }, indent=2)

    raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.1)
    raw = strip_llm_wrapper(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Agent 1 returned invalid JSON: {e}\n\nRaw output:\n{raw}") from e

    try:
        spec = VendorSpec.model_validate(data)
    except Exception as e:
        raise ValueError(f"Agent 1 output failed schema validation: {e}\n\nData:\n{data}") from e

    logger.info("Discovered %d vendors:", len(spec.discovered_vendors))
    for v in spec.discovered_vendors:
        logger.info("  %s: criticality=%d, base_url_env=%s, app_route=%s, endpoints=%d", v.name, v.criticality_score, v.base_url_env, v.app_route, len(v.endpoints))
    return spec, source_files


def _clone_or_fetch(repo: str) -> str:
    """
    Return a local path to the repository source.
    For hackathon: expects the repo to be cloned adjacent to the ghostvendor repo.
    Falls back to fetching individual files via GitHub API if not found locally.
    """
    # Check if repo is cloned locally (e.g. ../ghostvendor-demo-app)
    repo_name = repo.split("/")[-1]
    candidates = [
        Path(__file__).parent.parent.parent.parent / repo_name,
        Path(__file__).parent.parent.parent / repo_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    # Fallback: use a temp dir and fetch files via GitHub API
    import tempfile
    tmp = tempfile.mkdtemp(prefix=f"ghostvendor_{repo_name}_")
    tree = get_tree(repo)
    for item in tree:
        if item.get("type") != "blob" or not item["path"].endswith(".py"):
            continue
        try:
            content = get_file(repo, item["path"])
            dest = Path(tmp) / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except Exception:
            pass
    return tmp
