"""Provider registry. All providers speak the OpenAI chat-completions format,
so switching = changing base_url + api key + model name."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None
    model_env: str
    base_url_env: str | None = None  # optional override (e.g. local server port)


PROVIDERS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "OPENAI_MODEL"),
    "groq": ProviderConfig("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL"),
    "gemini": ProviderConfig(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
    ),
    "local": ProviderConfig("local", "http://localhost:8080/v1", None, "LOCAL_MODEL", "LOCAL_BASE_URL"),
}


def resolve(provider: str, model: str | None = None) -> tuple[str, str, str]:
    """Return (base_url, api_key, model) for a provider, reading from env."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Choose from {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    base_url = (os.getenv(cfg.base_url_env) if cfg.base_url_env else None) or cfg.base_url
    if cfg.api_key_env:
        api_key = os.getenv(cfg.api_key_env, "")
        if not api_key:
            raise RuntimeError(f"{cfg.api_key_env} is not set (check your .env)")
    else:
        api_key = "not-needed"
    model = model or os.getenv(cfg.model_env, "")
    if not model:
        raise RuntimeError(
            f"{cfg.model_env} is not set. Copy the exact model name from the provider's docs into .env"
        )
    return base_url, api_key, model
