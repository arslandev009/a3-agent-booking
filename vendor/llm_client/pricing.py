"""Cost estimation. Prices change often, so NOTHING is hardcoded.
Optionally set LLM_PRICES_FILE to a JSON like:
{"openai:<model>": {"input_per_1m": 0.0, "output_per_1m": 0.0}}
Unknown models cost 0.0 (free tiers / local)."""
from __future__ import annotations

import json
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _load() -> dict:
    path = os.getenv("LLM_PRICES_FILE")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = _load().get(f"{provider}:{model}")
    if not price:
        return 0.0
    return (prompt_tokens * price["input_per_1m"] + completion_tokens * price["output_per_1m"]) / 1_000_000
