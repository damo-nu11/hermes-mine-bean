"""Slash command dispatcher for /minebean.

Maps `/minebean <subcommand> [args...]` to the matching tool handler.
Subcommands mirror the tool names without the `minebean_` prefix.

Examples:
    /minebean status
    /minebean deploy sniper
    /minebean claim
    /minebean pending 0xabc...
    /minebean profile nostradamus
    /minebean autostart every 5m
    /minebean autostop
    /minebean inference_status
    /minebean chat What's the optimal sniper block this round?
    /minebean vvv_status 0xabc...
"""
from __future__ import annotations

import json
from typing import Any

from . import tools as tool_module

# Map slash subcommand to the underlying tool handler name.
# Kept explicit rather than auto-derived so help text stays predictable.
_SUBCOMMANDS: dict[str, str] = {
    "status": "minebean_status",
    "pending": "minebean_pending",
    "profile": "minebean_set_profile",
    "deploy": "minebean_deploy",
    "claim": "minebean_claim",
    "autostart": "minebean_autostart",
    "autostop": "minebean_autostop",
    "inference_status": "minebean_inference_status",
    "chat": "minebean_chat",
    "vvv_status": "minebean_vvv_status",
}

_HELP_TEXT = (
    "MineBean commands:\n"
    "  /minebean status                  read current round state\n"
    "  /minebean pending [address]       check pending winnings\n"
    "  /minebean profile <name>          set default strategy (sniper, anti-winner,\n"
    "                                    beanpot-hunter, anti-loser, nostradamus)\n"
    "  /minebean deploy <profile>        deploy into the current round\n"
    "  /minebean claim                   claim pending winnings\n"
    "  /minebean autostart [schedule]    start the autonomous cron job\n"
    "  /minebean autostop                stop the autonomous cron job\n"
    "  /minebean inference_status        show active LLM provider + configured map\n"
    "  /minebean chat <prompt>           send a prompt to the active LLM provider\n"
    "  /minebean vvv_status [address]    read VVV + sVVV balances on Base"
)


def handle_slash(args: str, context: dict[str, Any] | None = None) -> str:
    """Entry point Hermes calls when a user types /minebean ...

    `args` is everything after `/minebean ` (no leading slash, no command name).
    Returns a JSON string (consistent with tool handler return shape).
    """
    parts = (args or "").strip().split(maxsplit=1)
    if not parts or parts[0] in ("", "help", "-h", "--help"):
        return _HELP_TEXT

    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    handler_name = _SUBCOMMANDS.get(sub)
    if handler_name is None:
        return json.dumps({
            "ok": False,
            "stage": "unknown_subcommand",
            "subcommand": sub,
            "available": list(_SUBCOMMANDS.keys()),
        })

    handler = tool_module.HANDLERS.get(handler_name)
    if handler is None:
        # Should never happen if schemas + tools stay in sync.
        return json.dumps({
            "ok": False,
            "stage": "handler_missing",
            "tool": handler_name,
        })

    # Minimal positional-arg parsing per subcommand. Step 3 fleshes this out
    # with argparse for richer flags. Bootstrap supports the common case only.
    kwargs: dict[str, Any] = {}
    if sub == "pending" and rest:
        kwargs["address"] = rest.strip()
    elif sub == "profile" and rest:
        kwargs["profile"] = rest.strip()
    elif sub == "deploy" and rest:
        kwargs["profile"] = rest.strip()
    elif sub == "autostart" and rest:
        kwargs["schedule"] = rest.strip()
    elif sub == "chat" and rest:
        # Everything after `/minebean chat ` is the prompt verbatim.
        kwargs["prompt"] = rest
    elif sub == "vvv_status" and rest:
        kwargs["address"] = rest.strip()

    return handler(**kwargs)
