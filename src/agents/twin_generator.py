"""
Agent 2 — Adversarial Twin Generator.

Takes a validated VendorSpec and generates a stateful FastAPI Evil Twin
that impersonates the vendor's API. Normal behavior is preserved until
chaos is activated via the /chaos control endpoint.

The generated code is written to a temp file and returned as a string
for Agent 3 (Context Guard) to inspect before execution.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

from tools import nebius_client
from models.vendor_spec import Vendor, VendorSpec


_SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "twin_generator.txt").read_text()

# Deterministic port assignment per vendor so Evil Twin URLs are predictable across runs.
# The demo app's CI workflow and the state machine both need to know the port before the
# twin starts — hardcoding well-known vendors avoids a runtime port-negotiation step.
# Unknown vendors start at 8010 and increment to avoid collisions with reserved ports.
_VENDOR_PORTS: dict[str, int] = {
    "stripe": 8001,
    "sendgrid": 8002,
    "twilio": 8003,
    "auth0": 8004,
    "slack": 8005,
}
_DEFAULT_PORT_START = 8010  # Unknown vendors allocated from here upward


def _assign_port(vendor_name: str, existing_ports: set[int]) -> int:
    """Assign a deterministic port to a vendor, avoiding conflicts."""
    base = _VENDOR_PORTS.get(vendor_name.lower())
    if base and base not in existing_ports:
        return base
    port = _DEFAULT_PORT_START
    while port in existing_ports:
        port += 1
    return port


def _clean_output(raw: str) -> str:
    """
    Strip any chain-of-thought blocks or markdown fences from model output.
    The prompt instructs output-only Python, but models sometimes add wrappers.
    """
    raw = raw.strip()

    # Strip <think>...</think> reasoning blocks
    if "<think>" in raw and "</think>" in raw:
        raw = raw[raw.index("</think>") + len("</think>"):].strip()

    # Strip markdown code fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Remove opening fence line
        lines = lines[1:]
        # Remove closing fence if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    return raw


def run(spec: VendorSpec) -> dict[str, "EvilTwinArtifact"]:
    """
    Generate Evil Twin code for every vendor in the spec, ordered by criticality.

    Args:
        spec: Validated VendorSpec from Agent 1.

    Returns:
        Dict of {vendor_name: EvilTwinArtifact} — code + port for each vendor.
        Ordered highest criticality first (attack order).

    Raises:
        ValueError: If the generated code fails basic syntax validation.
    """
    artifacts: dict[str, "EvilTwinArtifact"] = {}
    used_ports: set[int] = set()

    for vendor in spec.vendors_by_criticality():
        port = _assign_port(vendor.name, used_ports)
        used_ports.add(port)

        logger.info("Generating Evil Twin for %s on port %d...", vendor.name, port)
        code = _generate_twin(vendor, spec.repository, port)
        logger.info("%s: %d chars, uvicorn.run pattern fixed=%s", vendor.name, len(code), 'uvicorn.run(app' in code)
        artifacts[vendor.name] = EvilTwinArtifact(
            vendor_name=vendor.name,
            port=port,
            code=code,
            base_url_env=vendor.base_url_env,
        )

    return artifacts


def _generate_twin(vendor: Vendor, repository: str, port: int) -> str:
    """
    Call Nemotron Super to generate Evil Twin code for a single vendor.

    Args:
        vendor: Single vendor from the VendorSpec.
        repository: GitHub repository identifier for context.
        port: Port the Evil Twin will listen on.

    Returns:
        Raw Python source code for the Evil Twin FastAPI app.

    Raises:
        ValueError: If output is empty or fails syntax check.
    """
    user_message = json.dumps({
        "repository": repository,
        "port": port,
        "vendor": vendor.model_dump(),
    }, indent=2)

    # Ultra is primary for twin generation — produces better code and is more reliable.
    # Super is the fallback.
    raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=user_message, temperature=0.2)
    code = _clean_output(raw)

    if not code:
        raw = nebius_client.super_(system=_SYSTEM_PROMPT, user=user_message, temperature=0.2)
        code = _clean_output(raw)

    if not code:
        raise ValueError(f"Agent 2 returned empty output for vendor: {vendor.name}")

    # Deterministic fix: ensure uvicorn.run uses the app object, not a string module reference.
    # LLMs sometimes generate uvicorn.run("module_name:app", ...) which breaks when the file
    # is saved under a different name (e.g. /tmp/twin.py in the Contree sandbox).
    code = _fix_uvicorn_run(code)

    # Auto-repair: retry once with the syntax error as context
    for attempt in range(2):
        try:
            _validate_syntax(code, vendor.name)
            return code
        except ValueError as e:
            if attempt == 1:
                raise
            repair_prompt = (
                f"The following Python code has a syntax error:\n\n{code}\n\n"
                f"Error: {e}\n\n"
                f"Fix the syntax error and output ONLY the corrected Python source code. "
                f"No explanation, no markdown fences."
            )
            raw = nebius_client.ultra(system=_SYSTEM_PROMPT, user=repair_prompt, temperature=0.1)
            code = _clean_output(raw)

    return code


def _fix_uvicorn_run(code: str) -> str:
    """
    Replace uvicorn.run("module:app", ...) with uvicorn.run(app, ...).
    LLMs generate string references that break when the file is saved under a different name.
    """
    return re.sub(
        r'uvicorn\.run\(\s*["\'][^"\']+:app["\']',
        'uvicorn.run(app',
        code,
    )


def _validate_syntax(code: str, vendor_name: str) -> None:
    """
    Compile the generated Python to catch syntax errors before passing to Agent 3.

    Args:
        code: Generated Python source.
        vendor_name: Used in error messages.

    Raises:
        ValueError: If the code has a syntax error.
    """
    try:
        compile(code, f"evil_twin_{vendor_name.lower()}.py", "exec")
    except SyntaxError as e:
        raise ValueError(
            f"Agent 2 generated syntactically invalid Python for {vendor_name}: {e}\n\n"
            f"Code:\n{code[:500]}..."
        ) from e


class EvilTwinArtifact:
    """Holds the generated Evil Twin code and metadata for a single vendor."""

    def __init__(self, vendor_name: str, port: int, code: str, base_url_env: str):
        self.vendor_name = vendor_name
        self.port = port
        self.code = code
        self.base_url_env = base_url_env

    @property
    def local_base_url(self) -> str:
        return f"http://localhost:{self.port}"

    def write(self, output_dir: str) -> Path:
        """
        Write the Evil Twin code to disk.

        Args:
            output_dir: Directory to write the file into.

        Returns:
            Path to the written file.
        """
        dest = Path(output_dir) / f"evil_twin_{self.vendor_name.lower()}.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.code, encoding="utf-8")
        return dest

    def __repr__(self) -> str:
        return f"EvilTwinArtifact(vendor={self.vendor_name}, port={self.port})"
