"""
Deterministic AST-based dependency scanner.

Walks Python source files to find all outbound HTTP calls across multiple
HTTP libraries: requests, httpx, aiohttp, urllib. Resolves env var references
through variable assignment chains so the LLM receives precise base URL env
var names rather than raw variable names.

LLM enrichment (Agent 1) runs on top of these findings — AST proves the
dependency exists, the LLM identifies the vendor and contract details.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HttpCall:
    """A single outbound HTTP call found in source code."""
    file: str
    line: int
    method: str          # GET, POST, PUT, DELETE, PATCH, etc.
    url_expr: str        # Raw URL expression as found in the AST
    base_url_env: str    # Env var name driving the base URL, if resolved
    library: str         # requests | httpx | aiohttp | urllib | unknown


@dataclass
class ScanResult:
    """Aggregated output of the AST scan across all Python files in a repo."""
    http_calls: list[HttpCall] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)        # All os.environ references found
    string_urls: list[str] = field(default_factory=list)     # Hardcoded URL strings found
    source_files: list[str] = field(default_factory=list)    # All .py files scanned
    import_aliases: dict[str, str] = field(default_factory=dict)  # import x as y → {y: x}


# HTTP methods across all supported libraries
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "request",
                 "send", "fetch"}

# Libraries and their known HTTP-calling patterns
_REQUESTS_ROOTS = {"requests"}          # requests.get(...)
_HTTPX_ROOTS = {"httpx"}               # httpx.get(...), httpx.AsyncClient().get(...)
_AIOHTTP_ROOTS = {"aiohttp"}           # aiohttp.ClientSession().get(...)
_URLLIB_MODULES = {"urllib", "urllib.request"}  # urllib.request.urlopen(...)

# Directories to skip during scanning
_SKIP_DIRS = {".venv", "venv", "__pycache__", "site-packages", ".git", "node_modules",
              "dist", "build", ".eggs"}

# URL pattern for detecting hardcoded URLs in string constants
_URL_PATTERN = re.compile(r"https?://[^\s\"'`]+")


def _extract_env_var(node: ast.expr) -> str:
    """
    Extract the env var name from common env access patterns:
      os.environ['KEY']
      os.environ.get('KEY', ...)
      os.getenv('KEY', ...)
      environ['KEY']  (after from os import environ)

    Returns empty string if the node is not an env var reference.
    """
    if isinstance(node, ast.Subscript):
        # os.environ['KEY'] or environ['KEY']
        val = node.value
        if isinstance(val, ast.Attribute) and val.attr == "environ":
            if isinstance(node.slice, ast.Constant):
                return str(node.slice.value)
        if isinstance(val, ast.Name) and val.id == "environ":
            if isinstance(node.slice, ast.Constant):
                return str(node.slice.value)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        # os.environ.get('KEY', ...) or environ.get('KEY', ...)
        if func.attr == "get" and isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
        if func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "environ":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
        # os.getenv('KEY', ...)
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)

    return ""


def _build_var_env_map(tree: ast.AST) -> dict[str, str]:
    """
    Build a map of {variable_name: env_var_name} for assignments like:
      base_url = os.environ.get("STRIPE_BASE_URL", "https://api.stripe.com")
      STRIPE_URL = os.getenv("STRIPE_BASE_URL")
      api_base = environ["SENDGRID_BASE_URL"]
    """
    var_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env = _extract_env_var(node.value)
                    if env:
                        var_map[target.id] = env
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value:
                env = _extract_env_var(node.value)
                if env:
                    var_map[node.target.id] = env
    return var_map


def _find_base_url_env(url_node: ast.expr, var_map: dict[str, str]) -> str:
    """
    Resolve a URL expression to its underlying env var name.

    Handles:
      - f"{base_url}/v1/payment_intents" → looks up base_url in var_map
      - os.environ.get("STRIPE_BASE_URL") + "/v1/..." → direct extraction
      - "https://api.stripe.com/v1/..." → hardcoded, returns ""
      - STRIPE_BASE_URL + "/v1/..." → looks up STRIPE_BASE_URL in var_map
    """
    if isinstance(url_node, ast.JoinedStr):  # f-string
        for part in url_node.values:
            if isinstance(part, ast.FormattedValue):
                env = _extract_env_var(part.value)
                if env:
                    return env
                if isinstance(part.value, ast.Name):
                    return var_map.get(part.value.id, "")

    # Direct env var reference as the URL
    env = _extract_env_var(url_node)
    if env:
        return env

    # Name reference — look up in var_map
    if isinstance(url_node, ast.Name):
        return var_map.get(url_node.id, "")

    # BinOp: base_url + "/path" or STRIPE_BASE + "/path"
    if isinstance(url_node, ast.BinOp) and isinstance(url_node.op, ast.Add):
        env = _find_base_url_env(url_node.left, var_map)
        if env:
            return env

    return ""


def _ast_node_to_str(node: ast.expr) -> str:
    """Best-effort string representation of a URL AST node."""
    try:
        return ast.unparse(node)
    except Exception:
        return "<complex_expr>"


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """
    Collect import aliases: `import requests as req` → {"req": "requests"}.
    Used to detect HTTP calls made through aliased imports.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    if alias.asname:
                        aliases[alias.asname] = f"{node.module}.{alias.name}"
    return aliases


def _get_library(obj_name: str, aliases: dict[str, str]) -> str:
    """Determine which HTTP library a call object belongs to."""
    real_name = aliases.get(obj_name, obj_name)
    if real_name in _REQUESTS_ROOTS or real_name.startswith("requests"):
        return "requests"
    if real_name in _HTTPX_ROOTS or real_name.startswith("httpx"):
        return "httpx"
    if real_name in _AIOHTTP_ROOTS or real_name.startswith("aiohttp"):
        return "aiohttp"
    if real_name in _URLLIB_MODULES or real_name.startswith("urllib"):
        return "urllib"
    return "unknown"


def _extract_calls_from_tree(
    tree: ast.AST,
    rel_path: str,
    var_map: dict[str, str],
    aliases: dict[str, str],
    result: ScanResult,
) -> None:
    """
    Walk the AST and extract all HTTP calls into result.
    Handles: requests, httpx, aiohttp, urllib patterns.
    """
    for node in ast.walk(tree):
        # Collect os.environ references
        if isinstance(node, (ast.Subscript, ast.Call)):
            env = _extract_env_var(node)
            if env and env not in result.env_vars:
                result.env_vars.append(env)

        # Collect hardcoded URL strings
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _URL_PATTERN.match(node.value):
                url = node.value
                if url not in result.string_urls:
                    result.string_urls.append(url)

        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        method = func.attr.lower()
        if method not in _HTTP_METHODS:
            continue

        # Determine the root object being called on
        root = func.value
        library = "unknown"

        if isinstance(root, ast.Name):
            library = _get_library(root.id, aliases)
        elif isinstance(root, ast.Attribute):
            # session.get(...) or client.get(...) — check the attr chain
            if isinstance(root.value, ast.Name):
                library = _get_library(root.value.id, aliases)
        elif isinstance(root, ast.Call):
            # httpx.Client().get(...) or aiohttp.ClientSession().get(...)
            if isinstance(root.func, ast.Attribute) and isinstance(root.func.value, ast.Name):
                library = _get_library(root.func.value.id, aliases)

        if library == "unknown":
            # urllib.request.urlopen(url) pattern
            if isinstance(root, ast.Attribute) and isinstance(root.value, ast.Attribute):
                if isinstance(root.value.value, ast.Name) and root.value.value.id in ("urllib", "urllib.request"):
                    library = "urllib"

        if library == "unknown":
            continue

        # Extract URL argument
        url_node = None
        if node.args:
            url_node = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == "url":
                    url_node = kw.value
                    break

        # urllib.request.urlopen special case — first arg is url or Request object
        if url_node is None:
            continue

        url_expr = _ast_node_to_str(url_node)
        base_url_env = _find_base_url_env(url_node, var_map)

        result.http_calls.append(HttpCall(
            file=rel_path,
            line=node.lineno,
            method=method.upper(),
            url_expr=url_expr,
            base_url_env=base_url_env,
            library=library,
        ))


def scan_directory(root: str) -> ScanResult:
    """
    Recursively scan all Python files under `root` for outbound HTTP calls.

    Supports: requests, httpx, aiohttp, urllib.
    Resolves env var references through variable assignment chains.
    Collects hardcoded URL strings for LLM enrichment.

    Args:
        root: Absolute or relative path to the repository root.

    Returns:
        ScanResult with all discovered HTTP calls, env var references, and URL strings.
    """
    result = ScanResult()
    root_path = Path(root).resolve()

    for py_file in sorted(root_path.rglob("*.py")):
        parts = set(py_file.parts)
        if parts & _SKIP_DIRS:
            continue

        rel_path = str(py_file.relative_to(root_path))
        result.source_files.append(rel_path)

        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        var_map = _build_var_env_map(tree)
        aliases = _collect_import_aliases(tree)
        _extract_calls_from_tree(tree, rel_path, var_map, aliases, result)

    return result


def scan_result_to_dict(result: ScanResult) -> dict:
    """Serialize a ScanResult to a plain dict for passing to LLM prompts."""
    return {
        "source_files": result.source_files,
        "env_vars": result.env_vars,
        "string_urls": result.string_urls,
        "http_calls": [
            {
                "file": c.file,
                "line": c.line,
                "method": c.method,
                "url_expr": c.url_expr,
                "base_url_env": c.base_url_env,
                "library": c.library,
            }
            for c in result.http_calls
        ],
    }
