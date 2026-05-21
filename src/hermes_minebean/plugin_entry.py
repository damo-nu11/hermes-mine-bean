"""Plugin entry point for hermes-mine-bean.

Hermes Agent discovers this module via the `hermes_agent.plugins` entry
point in pyproject.toml. At plugin install or enable time, Hermes calls
`register_module.register(ctx)` with a context object that exposes the
runtime's registration API.

Registration sequence:
1. Tools (the 7 minebean_* tools from schemas.ALL_TOOLS)
2. Slash command (/minebean)
3. Hooks (pre_llm_call, on_session_start)

The CLI subcommand (hermes minebean <sub>) and the bundled SKILL.md land in
later steps. Tool handlers are stubs until Step 2 wires the real GridMining
contract calls.
"""
from __future__ import annotations

import logging
from typing import Any

from . import hooks as hook_module
from . import slash as slash_module
from . import tools as tool_module
from .schemas import ALL_TOOLS

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Wire the MineBean plugin into a running Hermes Agent.

    `ctx` is the registration context Hermes passes. We call:
      - ctx.register_tool(name, toolset, schema, handler, check_fn, emoji, description)
      - ctx.register_command(name, handler, description, args_hint)
      - ctx.register_hook(event_name, handler)

    Defensive shape: every ctx call is wrapped so a missing or evolving
    API surface on Hermes' side surfaces as a warning rather than a crash.
    """
    # 1. Tools.
    handler_map = tool_module.HANDLERS
    for name, schema, emoji, requires_signer in ALL_TOOLS:
        handler = handler_map.get(name)
        if handler is None:
            logger.warning("schema %s has no matching handler in HANDLERS, skipping", name)
            continue
        try:
            ctx.register_tool(
                name=name,
                toolset="minebean",
                schema=schema,
                handler=handler,
                check_fn=tool_module.check_configured if requires_signer else None,
                emoji=emoji,
                description=schema.get("description", "")[:280],
            )
        except Exception as exc:
            logger.warning("register_tool failed for %s: %s", name, exc)

    # 2. Slash command.
    try:
        ctx.register_command(
            "minebean",
            handler=slash_module.handle_slash,
            description=(
                "MineBean round-based mining: status, deploy, claim, pending, "
                "profile, autostart, autostop."
            ),
            args_hint="status|pending|profile|deploy|claim|autostart|autostop",
        )
    except Exception as exc:
        logger.warning("register_command(/minebean) failed: %s", exc)

    # 3. Hooks.
    try:
        ctx.register_hook("pre_llm_call", hook_module.pre_llm_call)
    except Exception as exc:
        logger.warning("register_hook(pre_llm_call) failed: %s", exc)
    try:
        ctx.register_hook("on_session_start", hook_module.on_session_start)
    except Exception as exc:
        logger.warning("register_hook(on_session_start) failed: %s", exc)


class _Module:
    """Tiny shim that satisfies Hermes' entry-point contract.

    Hermes calls `register_module.register(ctx)` at plugin enable time.
    The shim lets us keep `register` as a plain module-level function for
    easy testing while still exposing it under the attribute Hermes expects.
    """

    def __init__(self) -> None:
        self.register = register


register_module = _Module()
