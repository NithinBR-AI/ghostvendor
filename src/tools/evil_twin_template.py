"""
Evil Twin harness template — GhostVendor owns this, LLM never generates it.

The LLM generates only vendor route handler(s) + a ROUTES list.
_assemble_twin() splices them into this fixed harness before execution.

Consumers:
  - evil_twin_runner.EvilTwinProcess.start() — writes assembled twin to disk
  - context_guard._sandbox_execute() — runs assembled twin in Contree sandbox
  - context_guard._llm_review() — sends assembled twin to LLM for security review
"""

import re

_HARNESS_TEMPLATE = '''\
import asyncio
import json
import logging
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

logger = logging.getLogger("evil_twin")

app = FastAPI()

# Chaos state — owned by the harness, never by LLM-generated code.
# asyncio.Lock prevents event-loop deadlocks when handlers await while holding the lock.
_chaos_state: dict = {{"active": False, "mode": None, "delay_seconds": None}}
_chaos_lock = asyncio.Lock()


@app.get("/health")
async def health():
    return {{"status": "ok"}}


@app.post("/chaos")
async def activate_chaos(request: Request):
    body = await request.json()
    async with _chaos_lock:
        _chaos_state["active"] = True
        _chaos_state["mode"] = body.get("mode")
        _chaos_state["delay_seconds"] = body.get("delay_seconds")
    return {{"activated": True, "mode": _chaos_state["mode"]}}


@app.delete("/chaos")
async def clear_chaos():
    async with _chaos_lock:
        _chaos_state["active"] = False
        _chaos_state["mode"] = None
        _chaos_state["delay_seconds"] = None
    return {{"cleared": True}}


@app.get("/chaos")
async def chaos_state():
    async with _chaos_lock:
        return dict(_chaos_state)


# --- LLM-generated vendor handler(s) injected below ---
{handler_code}

# Register vendor routes
for _method, _path, _handler in ROUTES:
    app.add_api_route(_path, _handler, methods=[_method])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port={port}, log_level="info")
'''


def assemble_twin(handler_code: str, vendor_name: str) -> str:
    """Assemble the full Evil Twin server from the fixed harness + LLM handler code."""
    port_match = re.search(r'#\s*port[:\s=]+(\d+)', handler_code, re.IGNORECASE)
    port = int(port_match.group(1)) if port_match else 8000
    return _HARNESS_TEMPLATE.format(handler_code=handler_code.strip(), port=port)
