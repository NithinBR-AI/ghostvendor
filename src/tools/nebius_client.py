import os
from openai import OpenAI

ULTRA = "nvidia/Llama-3_1-Nemotron-Ultra-253B-v1"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
NANO  = "nvidia/Nemotron-3_5-Lightning"

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


def ultra(system: str, user: str, temperature: float = 0.2) -> str:
    return call(ULTRA, system, user, temperature)


def super_(system: str, user: str, temperature: float = 0.2) -> str:
    return call(SUPER, system, user, temperature)


def nano(system: str, user: str, temperature: float = 0.2) -> str:
    return call(NANO, system, user, temperature)
