"""Append one JSON line per LLM call (provider, model, tokens, latency, cost)."""
from __future__ import annotations

import json
import os
import time


def log_call(record: dict) -> None:
    path = os.getenv("LLM_LOG_PATH", "llm_calls.jsonl")
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break the app
