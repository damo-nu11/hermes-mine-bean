"""Inference provider bootstrap for hermes-mine-bean.

Sets Venice as the default inference provider when the plugin is enabled,
while respecting any user-set override. This is the multi-provider hook:
the plugin nudges Venice into place only when nothing else is configured.

The bootstrap touches process env only. It never writes to the user's
~/.hermes/.env file. Persistent configuration is the user's call.

v0.3 ships the lightweight bootstrap. A full Venice client adapter +
abstraction over openai/anthropic/openrouter/ollama/lmstudio is on the
v0.4 roadmap.
"""
from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER: Final[str] = "venice"

# Known providers a user can pin via HERMES_INFERENCE_PROVIDER. The plugin
# does not validate against this list (Hermes Agent itself owns the source
# of truth) but the list informs the status tool output.
KNOWN_PROVIDERS: Final[tuple[str, ...]] = (
    "venice",
    "openai",
    "anthropic",
    "openrouter",
    "ollama",
    "lmstudio",
)

_ENV_PROVIDER_KEY: Final[str] = "HERMES_INFERENCE_PROVIDER"
_ENV_VENICE_API_KEY: Final[str] = "HERMES_VENICE_API_KEY"
_ENV_VENICE_NO_LOG: Final[str] = "HERMES_VENICE_NO_LOG"


def bootstrap_inference_defaults() -> str:
    """Resolve the active inference provider, defaulting to Venice.

    If `HERMES_INFERENCE_PROVIDER` is unset, set it to "venice" in the
    process env. Otherwise leave the user's choice untouched. Returns the
    active provider name.
    """
    current = os.environ.get(_ENV_PROVIDER_KEY, "").strip().lower()
    if current:
        logger.info("inference provider already set to %s, leaving it", current)
        return current

    os.environ[_ENV_PROVIDER_KEY] = DEFAULT_PROVIDER
    logger.info(
        "inference provider defaulted to %s. Override with %s=<provider>.",
        DEFAULT_PROVIDER,
        _ENV_PROVIDER_KEY,
    )
    if not os.environ.get(_ENV_VENICE_API_KEY):
        logger.info(
            "%s is not set. Set it in ~/.hermes/.env to enable Venice inference.",
            _ENV_VENICE_API_KEY,
        )
    return DEFAULT_PROVIDER


def get_active_provider() -> str:
    """Return the resolved inference provider, or "venice" if nothing set."""
    return (os.environ.get(_ENV_PROVIDER_KEY) or DEFAULT_PROVIDER).strip().lower()


def venice_configured() -> bool:
    """True if HERMES_VENICE_API_KEY is present in env."""
    return bool(os.environ.get(_ENV_VENICE_API_KEY))


def venice_no_log_enabled() -> bool:
    """True if HERMES_VENICE_NO_LOG is set to a truthy value."""
    raw = os.environ.get(_ENV_VENICE_NO_LOG, "").strip().lower()
    return raw in ("1", "true", "yes", "on")
