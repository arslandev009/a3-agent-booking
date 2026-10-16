"""Provider failover for a hosted demo: when one provider's quota is used up, the next one answers.

    llm = FailoverLLM([LLM("groq"), LLM("gemini")])      # same .chat(messages, **kw) as a single client

How it behaves
  * The first provider in the list is preferred. When a call fails with a quota / transient / auth error
    (429, 5xx, timeouts, 401/403) that provider is put on cooldown and the same call is retried on the next one.
  * Cooldown = the wait the provider suggests ("try again in 4m", "retryDelay: 28s") or, if it gives none (or a short one
    although a daily quota is gone), an exponentially growing wait: 60s, 120s, ... capped at one hour. A success resets it.
  * When the cooldown ends the preferred provider is tried again, so the demo keeps working while either one has quota.
  * Errors that are not about availability (for example a malformed request) are NOT retried elsewhere: they propagate.
  * If every provider is cooling down, `ProvidersBusy` (status 503) is raised without calling anyone; the API shows its
    friendly "busy" message.
Messages are the OpenAI-style history the agent already keeps, so a conversation can continue on the other provider.
Whether each real provider accepts the other's tool-call history is checked by scripts/check_failover.py.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

log = logging.getLogger("clinicagent.failover")

MIN_COOLDOWN_S, MAX_COOLDOWN_S, BASE_COOLDOWN_S = 15.0, 3600.0, 60.0


class ProvidersBusy(Exception):
    """Every configured provider is cooling down. status_code 503 so the agent and API treat it as transient."""
    status_code = 503


def suggested_wait_seconds(message: str) -> float | None:
    """Wait hinted by the provider: Groq 'try again in 4m32.1s', Gemini 'retry in 28.5s' / "retryDelay": "28s"."""
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", message or "", re.I)
    if m and any(m.groups()):
        h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + sec
    m = re.search(r"retry(?:Delay)?\W{1,6}(?:in\s+)?(\d+(?:\.\d+)?)\s*s", message or "", re.I)
    return float(m.group(1)) if m else None


def failoverable(exc: BaseException) -> bool:
    """Quota, transient and credential problems of one provider: another provider may still work."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    return status in (401, 403, 429, 500, 502, 503, 504) or any(
        k in name for k in ("RateLimit", "Timeout", "Connection", "InternalServer", "ServiceUnavailable",
                            "Authentication", "PermissionDenied", "ProvidersBusy"))


class FailoverLLM:
    def __init__(self, llms: list[Any], *, clock: Callable[[], float] = time.monotonic) -> None:
        if len(llms) < 1:
            raise ValueError("FailoverLLM needs at least one provider")
        self.llms = list(llms)
        self.clock = clock
        self._until = [0.0] * len(self.llms)   # cooldown end per provider
        self._fails = [0] * len(self.llms)     # consecutive failures per provider
        self._lock = threading.Lock()
        self.last_provider: str | None = None  # which provider answered the latest call (for checks and logs)
        self.served: list[str] = []            # provider of every successful call, in order (bounded)

    # the agent and the suite read these two attributes
    @property
    def provider(self) -> str:
        return "failover(" + ">".join(str(getattr(l, "provider", "?")) for l in self.llms) + ")"

    @property
    def model(self) -> str:
        return ",".join(str(getattr(l, "model", "?")) for l in self.llms)

    def _name(self, i: int) -> str:
        return str(getattr(self.llms[i], "provider", f"provider{i}"))

    def force_cooldown(self, name: str, seconds: float) -> None:
        """Take a provider out of rotation (used by scripts/check_failover.py and tests)."""
        with self._lock:
            for i in range(len(self.llms)):
                if self._name(i) == name:
                    self._until[i] = self.clock() + seconds

    def status(self) -> dict:
        now = self.clock()
        with self._lock:
            return {"ready": [self._name(i) for i in range(len(self.llms)) if self._until[i] <= now],
                    "cooling": {self._name(i): int(self._until[i] - now) for i in range(len(self.llms)) if self._until[i] > now}}

    def _mark_failed(self, i: int, exc: BaseException) -> float:
        with self._lock:
            self._fails[i] += 1
            backoff = BASE_COOLDOWN_S * 2 ** (self._fails[i] - 1)
            hint = suggested_wait_seconds(str(exc)) or 0.0
            wait = min(MAX_COOLDOWN_S, max(MIN_COOLDOWN_S, hint, backoff))
            self._until[i] = self.clock() + wait
            return wait

    def chat(self, messages: list[dict], **kwargs: Any) -> Any:
        last_exc: BaseException | None = None
        tried = 0
        for i, llm in enumerate(self.llms):
            with self._lock:
                cooling = self._until[i] > self.clock()
            if cooling:
                continue
            tried += 1
            try:
                res = llm.chat(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not failoverable(exc):
                    raise
                wait = self._mark_failed(i, exc)
                log.warning("provider=%s unavailable (%s); cooling down %ds", self._name(i), type(exc).__name__, wait)
                last_exc = exc
                continue
            with self._lock:
                self._fails[i] = 0
            if self.last_provider not in (None, self._name(i)):
                log.info("now answering with provider=%s", self._name(i))
            self.last_provider = self._name(i)
            self.served = (self.served + [self.last_provider])[-200:]
            return res
        if last_exc is not None:
            raise last_exc
        raise ProvidersBusy("all providers are cooling down")
