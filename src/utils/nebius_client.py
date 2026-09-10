import logging
import os
from openai import OpenAI, APIError

logger = logging.getLogger(__name__)

# Primary models
ULTRA        = "nvidia/Nemotron-3-Ultra-550b-a55b"
SUPER        = "nvidia/nemotron-3-super-120b-a12b"
NANO         = "nvidia/Nemotron-3_5-Lightning"
DEEPSEEK_PRO = "deepseek-ai/DeepSeek-V4-Pro"

# Fallback chain
_ULTRA_FALLBACK = "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1"
_SUPER_FALLBACK = "nvidia/Nemotron-3-Ultra-550b-a55b"
_NANO_FALLBACK  = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://api.tokenfactory.nebius.com/v1/",
            api_key=os.environ["NEBIUS_API_KEY"],
        )
    return _client


def call(model: str, system: str, user: str, temperature: float = 0.2, max_tokens: int = 8192) -> str:
    response = _get_client().chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


def _call_with_fallback(primary: str, fallback: str, system: str, user: str, temperature: float, max_tokens: int = 8192) -> str:
    try:
        return call(primary, system, user, temperature, max_tokens=max_tokens)
    except APIError as e:
        logger.warning("%s failed (%s), falling back to %s", primary, e, fallback)
        return call(fallback, system, user, temperature, max_tokens=max_tokens)


def ultra(system: str, user: str, temperature: float = 0.2, max_tokens: int = 8192) -> str:
    return _call_with_fallback(ULTRA, _ULTRA_FALLBACK, system, user, temperature, max_tokens=max_tokens)


def deepseek_pro(system: str, user: str, temperature: float = 0.2, max_tokens: int = 16384) -> str:
    return _call_with_fallback(DEEPSEEK_PRO, ULTRA, system, user, temperature, max_tokens=max_tokens)


def super_(system: str, user: str, temperature: float = 0.2, max_tokens: int = 8192) -> str:
    return _call_with_fallback(SUPER, _SUPER_FALLBACK, system, user, temperature, max_tokens=max_tokens)


def nano(system: str, user: str, temperature: float = 0.2, max_tokens: int = 8192) -> str:
    return _call_with_fallback(NANO, _NANO_FALLBACK, system, user, temperature, max_tokens=max_tokens)


def strip_llm_wrapper(raw: str) -> str:
    """Strip <think> blocks and markdown fences from LLM output."""
    if not raw:
        return raw
    raw = raw.strip()
    # Strip all <think>...</think> blocks (model may emit multiple)
    import re
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip outermost markdown fence if present
    if raw.startswith("```"):
        parts = raw.split("```")
        # parts[0]="" parts[1]="json\n{...}\n" parts[2]=""
        inner = parts[1] if len(parts) > 1 else ""
        if inner.startswith("json"):
            inner = inner[4:]
        raw = inner.strip()
    return raw
