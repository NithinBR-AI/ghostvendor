"""
Demo app runner — starts the target application as a subprocess with Evil Twin URLs
injected as environment variables, then fires real HTTP requests at it.

This is how Agent 4 tests the real application behavior against real Evil Twins
without mocking — the app makes real HTTP calls to the twins, we observe what happens.

Generic by design: the startup command, port, and env vars all come from configuration,
not hardcoded assumptions about the target app's framework or vendor set.
"""

import logging
import os
import sys
import time
import subprocess
import requests
from pathlib import Path

logger = logging.getLogger(__name__)


STARTUP_TIMEOUT = 60    # seconds to wait for app to become ready
HEALTH_INTERVAL = 0.5
REQUEST_TIMEOUT = 12    # seconds — baseline verify; override per call for patched validation (needs 35s for retry scenarios)


class DemoAppProcess:
    """
    Manages the lifecycle of the target app subprocess during resilience testing.

    The app is started with Evil Twin base URLs injected as env vars so all
    vendor HTTP calls go to the local Evil Twins instead of real APIs.
    """

    def __init__(
        self,
        app_path: str,
        start_command: list[str],
        port: int,
        twin_env: dict[str, str],
        extra_env: dict[str, str] | None = None,
    ):
        """
        Args:
            app_path: Absolute path to the target app root directory.
            start_command: Command to start the app (e.g. ["python", "-m", "flask", "run"]).
            port: Port the app listens on.
            twin_env: Evil Twin base URL env vars (e.g. {"STRIPE_BASE_URL": "http://localhost:8001"}).
            extra_env: Any additional env vars needed to start the app (e.g. fake API keys).
        """
        self.app_path = app_path
        self.start_command = start_command
        self.port = port
        self.twin_env = twin_env
        self.extra_env = extra_env or {}
        self._process: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self) -> None:
        """Start the app subprocess with Evil Twin env vars injected."""
        self._wait_for_port_free()
        env = {
            **os.environ,
            **self.extra_env,
            **self.twin_env,  # Twin URLs override anything in extra_env
        }

        logger.info("Starting: %s in %s on port %d", self.start_command, self.app_path, self.port)
        self._process = subprocess.Popen(
            self.start_command,
            cwd=self.app_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("Process PID=%d, waiting for ready...", self._process.pid)
        self._wait_for_ready()
        logger.info("App ready on port %d", self.port)

    def _wait_for_port_free(self, timeout: float = 10.0) -> None:
        """
        Wait until port is not bound — guards against a previous process still shutting down.
        Raises RuntimeError immediately if port is still bound after timeout, rather than
        letting _wait_for_ready burn another 15s before failing.
        """
        import socket
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(("localhost", self.port)) != 0:
                    return  # port is free
            time.sleep(0.3)
        raise RuntimeError(
            f"Port {self.port} still bound after {timeout:.0f}s — previous app process did not release it. "
            f"Cannot start new app instance."
        )

    def _wait_for_ready(self) -> None:
        """
        Poll the app until it responds to any HTTP request or timeout is reached.

        Tries common health check paths first, falls back to the app root.
        Any HTTP response (even 404) means the server is up.
        """
        probe_paths = ["/health", "/healthz", "/ping", "/"]
        deadline = time.monotonic() + STARTUP_TIMEOUT
        last_error = None

        while time.monotonic() < deadline:
            for path in probe_paths:
                try:
                    resp = requests.get(f"{self.base_url}{path}", timeout=1)
                    logger.debug("probe %s → %d", path, resp.status_code)
                    if resp.status_code < 500:
                        return
                except requests.exceptions.RequestException as e:
                    last_error = e
            time.sleep(HEALTH_INTERVAL)

        self.stop()
        raise RuntimeError(
            f"App at {self.app_path} did not become ready within {STARTUP_TIMEOUT}s. "
            f"Last error: {last_error}"
        )

    def post(self, path: str, payload: dict, timeout: float | None = None) -> tuple[int, dict | str, float]:
        url = f"{self.base_url}{path}"
        logger.debug("POST %s payload=%s", url, payload)
        start = time.monotonic()
        resp = requests.post(
            url,
            json=payload,
            timeout=timeout if timeout is not None else REQUEST_TIMEOUT,
        )
        elapsed = round((time.monotonic() - start) * 1000, 1)
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        logger.info("POST %s → %d in %sms body=%r", path, resp.status_code, elapsed, str(body)[:100])
        return resp.status_code, body, elapsed

    def stop(self) -> None:
        """Terminate the app subprocess."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            try:
                stderr_out = self._process.stderr.read().decode("utf-8", errors="replace")
                if stderr_out.strip():
                    logger.info("App stderr: %s", stderr_out[-2000:])
            except Exception:
                pass
            self._process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


def find_app_path(repo_name: str) -> str:
    """
    Locate the target app on disk — checks directories adjacent to the ghostvendor repo.

    Args:
        repo_name: Repository name (last segment of owner/repo, e.g. "ghostvendor-demo-app").

    Returns:
        Absolute path to the app root.

    Raises:
        FileNotFoundError: If the app cannot be found locally.
    """
    candidates = [
        Path(__file__).parent.parent.parent.parent / repo_name,
        Path(__file__).parent.parent.parent / repo_name,
        Path.cwd().parent / repo_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        f"App '{repo_name}' not found locally. "
        f"Expected it adjacent to the ghostvendor repo. "
        f"Checked: {[str(c) for c in candidates]}"
    )
