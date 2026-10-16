from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Sequence, TypeVar

from dotenv import find_dotenv, load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from .calllog import log_call
from .pricing import estimate_cost
from .providers import resolve

load_dotenv(find_dotenv(usecwd=True))

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cost_usd: float
    raw: Any = field(default=None, repr=False)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code >= 500


class LLM:
    """One interface for openai / groq / gemini / local (llama.cpp).

    llm = LLM("gemini")                        # or LLM() -> LLM_PROVIDER from .env
    llm = LLM("groq", fallback=LLM("gemini"))  # fallback after retries fail
    """

    def __init__(
        self,
        provider: str | None = None,
        *,
        model: str | None = None,
        fallback: "LLM | None" = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        _client: Any = None,  # injected in tests
    ) -> None:
        self.provider = provider or os.getenv("LLM_PROVIDER", "gemini")
        if _client is None:
            base_url, api_key, self.model = resolve(self.provider, model)
            self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        else:
            self.model = model or "test-model"
            self._client = _client
        self.fallback = fallback
        self.max_retries = max_retries

    # ------------------------------------------------------------------ chat
    def chat(self, messages: Sequence[dict], **kwargs: Any) -> ChatResult:
        """messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
        kwargs go to chat.completions.create (temperature, max_tokens, tools, ...)."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(model=self.model, messages=list(messages), **kwargs)
                return self._wrap(resp, time.perf_counter() - t0)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _retryable(exc) or attempt == self.max_retries:
                    break
                time.sleep(min(2**attempt, 20) + random.random())  # backoff + jitter
        if self.fallback is not None:
            log_call({"event": "fallback", "from": self.provider, "to": self.fallback.provider, "error": repr(last_exc)})
            return self.fallback.chat(messages, **kwargs)
        assert last_exc is not None
        raise last_exc

    def _wrap(self, resp: Any, latency: float) -> ChatResult:
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        text = resp.choices[0].message.content or ""
        cost = estimate_cost(self.provider, self.model, pt, ct)
        log_call(
            {
                "provider": self.provider,
                "model": self.model,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "latency_s": round(latency, 3),
                "cost_usd": round(cost, 6),
            }
        )
        return ChatResult(text, self.provider, self.model, pt, ct, latency, cost, raw=resp)

    # ------------------------------------------------------------------ json
    def chat_json(self, messages: Sequence[dict], schema: type[T], **kwargs: Any) -> T:
        """Ask for JSON matching a pydantic model; validate; retry once with the error.
        NOTE: JSON-mode support differs per provider/model, so test each one."""
        schema_hint = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        msgs = [
            {"role": "system", "content": f"Reply with ONLY valid JSON matching this JSON schema:\n{schema_hint}"},
            *messages,
        ]
        for attempt in range(2):
            res = self.chat(msgs, response_format={"type": "json_object"}, **kwargs)
            try:
                return schema.model_validate_json(_strip_fences(res.text))
            except ValidationError as err:
                if attempt == 1:
                    raise
                msgs = [
                    *msgs,
                    {"role": "assistant", "content": res.text},
                    {"role": "user", "content": f"Invalid JSON for the schema: {err}. Return corrected JSON only."},
                ]
        raise RuntimeError("unreachable")


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
