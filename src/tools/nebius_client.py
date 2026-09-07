import os
from openai import OpenAI, APIError

# Primary models — all NVIDIA via Nebius Token Factory
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
NANO  = "nvidia/Nemotron-3_5-Lightning"

# Fallback chain — all NVIDIA, used when primary is unavailable or errors
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


def call(model: str, system: str, user: str, temperature: float = 0.2) -> str:
    response = _get_client().chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


def _call_with_fallback(primary: str, fallback: str, system: str, user: str, temperature: float) -> str:
    try:
        return call(primary, system, user, temperature)
    except APIError as e:
        print(f"[nebius_client] {primary} failed ({e}), falling back to {fallback}")
        return call(fallback, system, user, temperature)


def ultra(system: str, user: str, temperature: float = 0.2) -> str:
    return _call_with_fallback(ULTRA, _ULTRA_FALLBACK, system, user, temperature)


def super_(system: str, user: str, temperature: float = 0.2) -> str:
    return _call_with_fallback(SUPER, _SUPER_FALLBACK, system, user, temperature)


def nano(system: str, user: str, temperature: float = 0.2) -> str:
    return _call_with_fallback(NANO, _NANO_FALLBACK, system, user, temperature)
