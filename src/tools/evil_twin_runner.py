"""
Evil Twin runner — launches, monitors, controls, and tears down Evil Twin processes.

Each Evil Twin runs as an isolated subprocess (uvicorn). The runner manages
the process lifecycle and exposes a clean API for the state machine to:
  - start the twin
  - activate a chaos scenario
  - clear chaos (return to normal)
  - verify the twin is healthy
  - stop the twin
"""

import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

import requests


class EvilTwinProcess:
    """
    Manages the lifecycle of a single Evil Twin subprocess.

    The twin runs as: uvicorn evil_twin_<vendor>:app --port <port>
    in an isolated temp directory so multiple twins can run concurrently.
    """

    STARTUP_TIMEOUT = 10   # seconds to wait for the twin to become healthy
    HEALTH_INTERVAL = 0.5  # polling interval during startup

    def __init__(self, vendor_name: str, port: int, code: str):
        self.vendor_name = vendor_name
        self.port = port
        self.code = code
        self._process: subprocess.Popen | None = None
        self._work_dir: str | None = None
        self._module_name = f"evil_twin_{vendor_name.lower()}"

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def chaos_url(self) -> str:
        return f"{self.base_url}/chaos"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"

    def start(self) -> None:
        """
        Write the Evil Twin code to a temp directory and launch uvicorn.

        Raises:
            RuntimeError: If the twin does not become healthy within STARTUP_TIMEOUT.
        """
        self._work_dir = tempfile.mkdtemp(prefix=f"ghostvendor_twin_{self.vendor_name.lower()}_")
        twin_file = Path(self._work_dir) / f"{self._module_name}.py"
        twin_file.write_text(self.code, encoding="utf-8")

        self._process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                f"{self._module_name}:app",
                "--port", str(self.port),
                "--log-level", "warning",
            ],
            cwd=self._work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": self._work_dir},
        )

        self._wait_for_health()

    def _wait_for_health(self) -> None:
        """Poll /health until the twin responds or timeout is reached."""
        deadline = time.monotonic() + self.STARTUP_TIMEOUT
        last_error = None
        while time.monotonic() < deadline:
            try:
                resp = requests.get(self.health_url, timeout=1)
                if resp.status_code == 200:
                    return
            except requests.exceptions.RequestException as e:
                last_error = e
            time.sleep(self.HEALTH_INTERVAL)

        self.stop()
        raise RuntimeError(
            f"Evil Twin for {self.vendor_name} did not become healthy within "
            f"{self.STARTUP_TIMEOUT}s on port {self.port}. Last error: {last_error}"
        )

    def activate_chaos(self, mode: str, duration_seconds: int | None = None, delay_seconds: int | None = None) -> None:
        """
        Activate a chaos scenario on the running twin.

        Args:
            mode: Chaos mode (502_burst, timeout, malformed_json, 429_rate_limit, etc.)
            duration_seconds: How long to stay in chaos mode (None = indefinite).
            delay_seconds: Delay for timeout mode.

        Raises:
            RuntimeError: If the chaos activation request fails.
        """
        payload: dict = {"mode": mode}
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if delay_seconds is not None:
            payload["delay_seconds"] = delay_seconds

        resp = requests.post(self.chaos_url, json=payload, timeout=5)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to activate chaos mode '{mode}' on {self.vendor_name} twin: "
                f"HTTP {resp.status_code} — {resp.text}"
            )

    def clear_chaos(self) -> None:
        """Return the twin to normal mode."""
        resp = requests.delete(self.chaos_url, timeout=5)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to clear chaos on {self.vendor_name} twin: HTTP {resp.status_code}"
            )

    def chaos_state(self) -> dict:
        """Return the current chaos state from the twin."""
        resp = requests.get(self.chaos_url, timeout=5)
        return resp.json()

    def is_healthy(self) -> bool:
        """Return True if the twin's /health endpoint responds with 200."""
        try:
            resp = requests.get(self.health_url, timeout=2)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def stop(self) -> None:
        """Terminate the Evil Twin subprocess and clean up temp files."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def __repr__(self) -> str:
        return f"EvilTwinProcess(vendor={self.vendor_name}, port={self.port})"


class EvilTwinManager:
    """
    Manages multiple Evil Twin processes — one per vendor.

    The state machine holds one EvilTwinManager per pipeline run.
    All twins are stopped on context exit or explicit teardown.
    """

    def __init__(self):
        self._twins: dict[str, EvilTwinProcess] = {}

    def launch(self, vendor_name: str, port: int, code: str) -> EvilTwinProcess:
        """
        Launch an Evil Twin for the given vendor.

        Args:
            vendor_name: Vendor identifier (e.g. "Stripe").
            port: Port to listen on.
            code: Generated FastAPI source code.

        Returns:
            The running EvilTwinProcess.
        """
        twin = EvilTwinProcess(vendor_name=vendor_name, port=port, code=code)
        twin.start()
        self._twins[vendor_name] = twin
        print(f"[ghostvendor] Evil Twin for {vendor_name} running on port {port}")
        return twin

    def get(self, vendor_name: str) -> EvilTwinProcess | None:
        return self._twins.get(vendor_name)

    def stop_all(self) -> None:
        """Terminate all running Evil Twin processes."""
        for twin in self._twins.values():
            twin.stop()
        self._twins.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop_all()
