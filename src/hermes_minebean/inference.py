"""Inference provider bootstrap for hermes-mine-bean.

Sets Venice as the default inference provider when the plugin is enabled,
while respecting any user-set override. This is the multi-provider hook:
the plugin nudges Venice into place only when nothing else is configured.

The bootstrap touches process env only. It never writes to the user's
~/.hermes/.env file. Persistent configuration is the user's call.

v0.4 ships the full client adapter via `inference_client`. This module
remains the env-bootstrap entry point and a thin compatibility layer
for callers that want a stable shape (status tool, hook context).
"""
from __future__ import annotations

import logging
import os
from typing import Final

from . import inference_client

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER: Final[str] = inference_client.DEFAULT_PROVIDER

# Known providers a user can pin via HERMES_INFERENCE_PROVIDER. Sourced
# from the client module so the two stay in sync automatically.
KNOWN_PROVIDERS: Final[tuple[str, ...]] = tuple(inference_client.PROVIDERS.keys())

_ENV_PROVIDER_KEY: Final[str] = "HERMES_INFERENCE_PROVIDER"
# v0.4 migrated to Venice's canonical env var name. The legacy alias is
# still read so users who set HERMES_VENICE_API_KEY against v0.3 don't
# break silently; the canonical name wins if both are present.
_ENV_VENICE_API_KEY: Final[str] = "VENICE_API_KEY"
_ENV_VENICE_API_KEY_LEGACY: Final[str] = "HERMES_VENICE_API_KEY"
_ENV_VENICE_NO_LOG: Final[str] = "HERMES_VENICE_NO_LOG"


def bootstrap_inference_defaults() -> str:
    """Resolve the active inference provider, defaulting to Venice.

    If `HERMES_INFERENCE_PROVIDER` is unset, set it to "venice" in the
    process env. Otherwise leave the user's choice untouched. Also
    bridges the v0.3 legacy `HERMES_VENICE_API_KEY` env var into the
    canonical `VENICE_API_KEY` slot if only the legacy name is set, so
    existing users don't lose their key on upgrade.

    Returns the active provider name.
    """
    # Legacy-to-canonical bridge. We do not overwrite an explicit
    # VENICE_API_KEY if the user has set both.
    if not os.environ.get(_ENV_VENICE_API_KEY) and os.environ.get(
        _ENV_VENICE_API_KEY_LEGACY
    ):
        os.environ[_ENV_VENICE_API_KEY] = os.environ[_ENV_VENICE_API_KEY_LEGACY]
        logger.info(
            "bridged %s -> %s for v0.4 compatibility. "
            "Rename in ~/.hermes/.env when convenient.",
            _ENV_VENICE_API_KEY_LEGACY,
            _ENV_VENICE_API_KEY,
        )

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
    """True if a Venice API key is present under the canonical or legacy var."""
    return bool(
        os.environ.get(_ENV_VENICE_API_KEY)
        or os.environ.get(_ENV_VENICE_API_KEY_LEGACY)
    )


def provider_configured(name: str | None = None) -> bool:
    """True if the resolved provider has its API key set in env."""
    return inference_client.provider_configured(name)


def venice_no_log_enabled() -> bool:
    """True if HERMES_VENICE_NO_LOG is set to a truthy value.

    Note: Venice's no-log mode is platform-default, not a per-request
    header. This flag is retained as a documentation signal for users
    who want to surface their privacy posture in tool output.
    """
    raw = os.environ.get(_ENV_VENICE_NO_LOG, "").strip().lower()
    return raw in ("1", "true", "yes", "on")
