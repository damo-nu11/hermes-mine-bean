"""Lifecycle hooks for hermes-mine-bean.

Two hooks registered:
- pre_llm_call: keyword-gated discoverability nudge. Only fires when the user
  message mentions mining or beans, never blindly on every turn.
- on_session_start: lightweight first-run check. Surfaces the setup checklist
  if the plugin is installed but no signer is configured.

Anti-pattern note: Botcoin's pre_llm_call injects status on every session
opener regardless of context, which adds noise to unrelated conversations.
Ours requires a keyword match so the plugin stays silent when irrelevant.
"""
from __future__ import annotations

import logging
from typing import Any

from .tools import resolve_signer_mode

logger = logging.getLogger(__name__)


# Words that trigger discoverability injection. Matched case-insensitively.
_TRIGGER_KEYWORDS = (
    "minebean",
    "mine bean",
    "$bean",
    "beanpot",
    "gridmining",
    "round",
)


def pre_llm_call(message: str, context: dict[str, Any] | None = None) -> str | None:
    """Optionally append a one-line nudge before the LLM sees the user message.

    Returns a string to inject, or None to leave the message untouched.
    Only fires when the message contains a trigger keyword AND the plugin is
    not yet configured. Configured users don't need a nudge.
    """
    if not message:
        return None

    lower = message.lower()
    if not any(kw in lower for kw in _TRIGGER_KEYWORDS):
        return None

    if resolve_signer_mode() is not None:
        # Already configured. Stay silent.
        return None

    return (
        "MineBean plugin is installed but no signer is configured. "
        "Set MINEBEAN_DEPLOYER_KEY (eoa) or BANKR_API_KEY (bankr) in "
        "~/.hermes/.env to enable deploy/claim/autostart tools. "
        "Read-only tools (status, pending) work without configuration."
    )


def on_session_start(context: dict[str, Any] | None = None) -> None:
    """Hook fired once per Hermes session. Bootstrap is a no-op.

    Step 1 keeps this minimal. Future versions can prefetch current round
    state here so the first `/minebean status` call is instant.
    """
    return None
