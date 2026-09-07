"""
Clones a GitHub repository and detects how to start it.

Returns RepoInfo — local path + detected startup command and port.
Agent 4 uses this to start the app as a subprocess without any manual config.
"""

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RepoInfo:
    local_path: str
    start_command: list[str]
    port: int
    extra_env: dict = None  # e.g. {"PYTHONPATH": "src"} for src-layout projects

    def __post_init__(self):
        if self.extra_env is None:
            self.extra_env = {}


def clone(repo: str) -> RepoInfo:
    """
    Clone a GitHub repository (or reuse existing local clone) and detect startup config.

    Args:
        repo: GitHub owner/repo identifier (e.g. "nithinbr33/ghostvendor-demo-app").

    Returns:
        RepoInfo with local_path, start_command, and port.
    """
    repo_name = repo.split("/")[-1]
    local_path = _find_or_clone(repo, repo_name)
    _ensure_dependencies(local_path)
    start_command, port = _detect_startup(local_path)
    extra_env = _detect_extra_env(local_path)
    logger.info("RepoInfo: local_path=%s start_command=%s port=%d extra_env=%s", local_path, start_command, port, extra_env)
    return RepoInfo(local_path=local_path, start_command=start_command, port=port, extra_env=extra_env)


def _find_or_clone(repo: str, repo_name: str) -> str:
    # Local dev shortcut — only active when GHOSTVENDOR_LOCAL_DEV=1 is set explicitly.
    # Keeps the pipeline portable: on any other machine it always does a real git clone.
    if os.environ.get("GHOSTVENDOR_LOCAL_DEV") == "1":
        candidates = [
            Path(__file__).parent.parent.parent.parent / repo_name,
            Path(__file__).parent.parent.parent / repo_name,
        ]
        for candidate in candidates:
            if candidate.exists():
                logger.info("Using existing local clone: %s", candidate)
                return str(candidate)

    token = os.environ.get("GITHUB_TOKEN", "")
    clone_url = f"https://{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"
    dest = Path(tempfile.mkdtemp(prefix=f"ghostvendor_{repo_name}_"))
    logger.info("Cloning %s → %s", repo, dest)

    result = subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(dest)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed for {repo}: {result.stderr.strip()}")

    return str(dest)


def _detect_startup(local_path: str) -> tuple[list[str], int]:
    """
    Detect how to start the app by reading main.py or app.py.

    Looks for:
      - app.run(port=XXXX) or app.run(..., port=XXXX, ...)
      - uvicorn.run(..., port=XXXX)
      - flask run --port XXXX in a Makefile or Procfile

    Falls back to: python main.py on port 5000.
    """
    root = Path(local_path)
    python = _find_python(root)

    # Find entry point
    for entry in ("main.py", "app.py", "run.py", "server.py"):
        entry_path = root / entry
        if entry_path.exists():
            port = _extract_port(entry_path.read_text(encoding="utf-8", errors="ignore"))
            return [python, entry], port

    # Flask app factory pattern — no explicit main.py
    if (root / "wsgi.py").exists():
        port = _extract_port((root / "wsgi.py").read_text(encoding="utf-8", errors="ignore"))
        return [python, "wsgi.py"], port

    return [python, "main.py"], 5000


def _extract_port(source: str) -> int:
    """Extract port number from app.run() or uvicorn.run() calls."""
    match = re.search(r"port\s*=\s*(\d+)", source)
    if match:
        return int(match.group(1))
    return 5000


def _ensure_dependencies(local_path: str) -> None:
    """
    Install project dependencies if no venv exists yet.
    Supports pyproject.toml (pip install -e .) and requirements.txt.
    Skips silently if venv already present — assumes deps are installed.
    """
    root = Path(local_path)
    venv_exists = (root / ".venv").exists() or (root / "venv").exists()
    if venv_exists:
        return

    python = _find_python(root)
    if (root / "pyproject.toml").exists():
        cmd = [python, "-m", "pip", "install", "-e", ".", "--quiet"]
    elif (root / "requirements.txt").exists():
        cmd = [python, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"]
    else:
        return

    logger.info("Installing dependencies in %s", local_path)
    result = subprocess.run(cmd, cwd=local_path, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("Dependency install failed: %s", result.stderr.strip()[:200])


def _detect_extra_env(local_path: str) -> dict[str, str]:
    """Detect if the project uses a src layout and needs PYTHONPATH set."""
    root = Path(local_path)
    env = {}
    if (root / "src").is_dir():
        env["PYTHONPATH"] = str(root / "src")
    return env


def _find_python(root: Path) -> str:
    """Return the venv python if present, otherwise sys.executable."""
    import sys
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",   # Windows
        root / ".venv" / "bin" / "python",            # Unix
        root / "venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable
