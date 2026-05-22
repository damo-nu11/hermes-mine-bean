"""Inference client adapter for hermes-mine-bean.

A thin OpenAI-compatible client wrapper that lets the plugin call any of
six supported inference providers from inside the agent loop. Used by the
`minebean_chat` tool and any future strategy-reasoning calls.

The architecture mirrors botcoin's flat if/elif solver dispatch but uses
`openai-python` instead of raw `urllib.request`, since `openai>=2.0` is
already a transitive dependency of `hermes-agent` and brings retry,
streaming, and 429 handling out of the box.

Providers
---------
- venice      OpenAI-compatible at api.venice.ai/api/v1
- openai      api.openai.com/v1
- anthropic   api.anthropic.com/v1 (OpenAI-compatible endpoint)
- openrouter  openrouter.ai/api/v1
- ollama      local at OLLAMA_HOST or http://127.0.0.1:11434/v1
- lmstudio    local at LMSTUDIO_HOST or http://127.0.0.1:1234/v1

Each provider has a base URL, an env var holding the API key, and a
default chat-completion model id. The active provider is chosen by
`HERMES_INFERENCE_PROVIDER` (set at plugin bootstrap to `venice`).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Final, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    """Static configuration for a single inference provider."""

    name: str
    base_url: str
    env_key: str
    default_model: str
    # Some providers (Venice) expose extra request fields under a custom key.
    # The chat() helper forwards `extra_body` to openai-python when set.
    supports_extra_body: bool = False


# Canonical provider table. Order is the natural fallback preference if we
# ever need it; current call paths always resolve a specific provider name
# from HERMES_INFERENCE_PROVIDER, never fall through this list.
PROVIDERS: Final[dict[str, ProviderConfig]] = {
    "venice": ProviderConfig(
        name="venice",
        base_url="https://api.venice.ai/api/v1",
        env_key="VENICE_API_KEY",
        default_model="venice-uncensored",
        supports_extra_body=True,
    ),
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
    ),
    "anthropic": ProviderConfig(
        name="anthropic",
        # Anthropic's OpenAI-compat shim lives at /v1, takes Bearer auth.
        base_url="https://api.anthropic.com/v1",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-haiku-4-5-20251001",
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        default_model="anthropic/claude-haiku-4.5",
    ),
    # Local providers use sentinel base URLs that get resolved at
    # make_client() time from OLLAMA_HOST / LMSTUDIO_HOST env. We don't
    # read env at import so runtime env changes are visible and the
    # module has zero import-time side effects.
    "ollama": ProviderConfig(
        name="ollama",
        base_url="__lazy_ollama__",
        env_key="OLLAMA_API_KEY",  # ignored by ollama, kept for shape
        default_model="llama3.1",
    ),
    "lmstudio": ProviderConfig(
        name="lmstudio",
        base_url="__lazy_lmstudio__",
        env_key="LMSTUDIO_API_KEY",  # ignored, kept for shape
        default_model="local-model",
    ),
}

DEFAULT_PROVIDER: Final[str] = "venice"
_ENV_PROVIDER_KEY: Final[str] = "HERMES_INFERENCE_PROVIDER"


def _resolve_provider_name(override: Optional[str] = None) -> str:
    """Resolve a provider name from override or env, falling back to default."""
    if override:
        return override.strip().lower()
    return (os.environ.get(_ENV_PROVIDER_KEY) or DEFAULT_PROVIDER).strip().lower()


def get_provider_config(name: Optional[str] = None) -> ProviderConfig:
    """Return the ProviderConfig for the resolved provider, or raise KeyError."""
    resolved = _resolve_provider_name(name)
    if resolved not in PROVIDERS:
        raise KeyError(
            f"unknown inference provider: {resolved}. "
            f"Known: {', '.join(PROVIDERS)}."
        )
    return PROVIDERS[resolved]


def _resolve_base_url(cfg: ProviderConfig) -> str:
    """Resolve the actual base URL for a provider config.

    Local providers (ollama, lmstudio) carry sentinel base URLs that get
    swapped for the user's OLLAMA_HOST / LMSTUDIO_HOST env at call time
    so the module has no import-time side effects.
    """
    if cfg.base_url == "__lazy_ollama__":
        return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434/v1")
    if cfg.base_url == "__lazy_lmstudio__":
        return os.environ.get("LMSTUDIO_HOST", "http://127.0.0.1:1234/v1")
    return cfg.base_url


def provider_configured(name: Optional[str] = None) -> bool:
    """True if the resolved provider has an API key available in env.

    Local providers (ollama, lmstudio) always return True because they
    don't require a key.
    """
    try:
        cfg = get_provider_config(name)
    except KeyError:
        return False
    if cfg.name in ("ollama", "lmstudio"):
        return True
    return bool(os.environ.get(cfg.env_key))


def make_client(name: Optional[str] = None) -> Any:
    """Build an openai.OpenAI client pointed at the resolved provider.

    Raises:
        KeyError: provider name is not in PROVIDERS.
        RuntimeError: openai-python is not installed.
        ValueError: required API key env var is missing for a remote provider.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai-python is required for the minebean inference client. "
            "Install with: pip install openai>=2.0"
        ) from exc

    cfg = get_provider_config(name)
    api_key = os.environ.get(cfg.env_key) or "sk-noop"
    # Local providers (ollama, lmstudio) accept any string for api_key; the
    # openai client requires it to be non-empty.
    if cfg.name not in ("ollama", "lmstudio") and not os.environ.get(cfg.env_key):
        raise ValueError(
            f"{cfg.env_key} is not set. Configure it in ~/.hermes/.env to "
            f"use {cfg.name} as the inference provider."
        )
    base_url = _resolve_base_url(cfg)
    return OpenAI(api_key=api_key, base_url=base_url)


def chat(
    messages: list[dict[str, str]],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    extra_body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Send a chat-completion request to the resolved provider.

    Returns a serializable dict with the response text, model used, finish
    reason, and any provider-side metadata extracted from response headers
    (Venice surfaces balance + rate limit headers; other providers vary).
    """
    cfg = get_provider_config(provider)
    client = make_client(provider)
    selected_model = model or cfg.default_model

    # Venice supports an `extra_body.venice_parameters` block. We mirror
    # botcoin's defaults: don't inject Venice's system prompt over ours,
    # don't enable web search unless caller asks.
    body_extras: dict[str, Any] = {}
    if cfg.supports_extra_body and cfg.name == "venice":
        body_extras["venice_parameters"] = {
            "include_venice_system_prompt": False,
            "enable_web_search": "off",
        }
    if extra_body:
        # Caller-supplied extra_body wins. Allows tests + future callers to
        # opt back into web search or system-prompt injection.
        body_extras.update(extra_body)

    request_kwargs: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
    }
    if body_extras:
        request_kwargs["extra_body"] = body_extras

    started = time.monotonic()
    response = client.chat.completions.create(**request_kwargs)
    duration = time.monotonic() - started

    choice = response.choices[0]
    text = (choice.message.content or "").strip()
    # Some Venice reasoning models put the answer under reasoning_content
    # and leave content empty. Fall back to whichever has text.
    if not text:
        reasoning = getattr(choice.message, "reasoning_content", None)
        if reasoning:
            text = reasoning.strip()

    return {
        "ok": True,
        "provider": cfg.name,
        "model": selected_model,
        "base_url": _resolve_base_url(cfg),
        "text": text,
        "finish_reason": choice.finish_reason,
        "usage": {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
            "completion_tokens": getattr(response.usage, "completion_tokens", None),
            "total_tokens": getattr(response.usage, "total_tokens", None),
        },
        "duration_seconds": round(duration, 3),
    }


# --- model discovery -------------------------------------------------------

_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
_MODELS_TTL_SECONDS: Final[float] = 300.0  # 5 minutes


def list_models(name: Optional[str] = None, *, force_refresh: bool = False) -> list[str]:
    """Return the provider's available text-model IDs.

    Cached for 5 minutes per provider so repeat calls (e.g. inside the
    status tool) stay cheap. Errors are swallowed and surface as an empty
    list; callers should treat the default_model as the source of truth.
    """
    cfg = get_provider_config(name)
    cached = _MODELS_CACHE.get(cfg.name)
    now = time.monotonic()
    if cached and not force_refresh and (now - cached[0]) < _MODELS_TTL_SECONDS:
        return cached[1]

    try:
        client = make_client(name)
        listing = client.models.list()
        ids = [m.id for m in listing.data] if hasattr(listing, "data") else []
    except Exception as exc:
        logger.info("list_models(%s) failed: %s", cfg.name, exc)
        ids = []

    _MODELS_CACHE[cfg.name] = (now, ids)
    return ids
