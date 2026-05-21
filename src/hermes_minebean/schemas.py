"""Tool schemas for hermes-mine-bean.

Each entry in ALL_TOOLS is a tuple of:
    (tool_name, schema_dict, emoji, requires_signer)

- tool_name: snake_case identifier Hermes uses to call the tool
- schema_dict: OpenAI function-calling style schema (name, description, parameters)
- emoji: intentionally empty string per the no-emojis-in-UI rule. Hermes accepts ""
  and renders the tool without a leading glyph.
- requires_signer: True if the tool writes on-chain. Hermes won't expose these
  tools to the LLM until a signer is configured (see tools.check_configured).

Read-only tools have requires_signer=False so the LLM can always answer
"what's the current round" without any setup.
"""
from __future__ import annotations

# Strategy presets exposed to minebean_deploy and minebean_set_profile.
STRATEGY_PRESETS = ("sniper", "anti-winner", "beanpot-hunter", "anti-loser", "nostradamus")


_STATUS_SCHEMA = {
    "name": "minebean_status",
    "description": (
        "Read the live state of the MineBean GridMining game on Base. Returns "
        "current round id, time remaining, active miners, total deployed in "
        "the current round, the beanpot reserve, and the caller's pending "
        "winnings if a signer is configured."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


_DEPLOY_SCHEMA = {
    "name": "minebean_deploy",
    "description": (
        "Deploy $BEAN into the current MineBean round using a strategy preset. "
        "Strategies: sniper (highest-EV block), anti-winner (avoid last winner), "
        "beanpot-hunter (aggressive when reserve is high), anti-loser (avoid last "
        "loser's pick), nostradamus (predicted next winner). Requires a signer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": list(STRATEGY_PRESETS),
                "description": "Strategy preset that decides which blocks to deploy on.",
            },
            "blocks": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "description": (
                    "Optional explicit block indices. Overrides the profile if "
                    "provided. Validated against the contract's grid size at "
                    "broadcast time."
                ),
            },
            "per_block_wei": {
                "type": "string",
                "description": (
                    "Optional $BEAN amount per block as a wei-denominated "
                    "string. Defaults to MINEBEAN_PER_BLOCK_WEI env var."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "If true, returns the resolved plan without broadcasting. "
                    "Defaults to true while live broadcast is gated by the dev "
                    "key-handling review."
                ),
                "default": True,
            },
        },
        "required": ["profile"],
    },
}


_CLAIM_SCHEMA = {
    "name": "minebean_claim",
    "description": (
        "Claim any pending $BEAN winnings for the configured signer wallet. "
        "Reads the pending balance first and short-circuits if zero. Requires "
        "a signer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "description": (
                    "If true, returns pending balance without broadcasting. "
                    "Defaults to true while live broadcast is gated."
                ),
                "default": True,
            },
        },
        "required": [],
    },
}


_PENDING_SCHEMA = {
    "name": "minebean_pending",
    "description": (
        "Read pending $BEAN winnings for a wallet. Defaults to the configured "
        "signer wallet if none is provided. No signer required for explicit "
        "address lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "Optional 0x address. Defaults to the configured signer.",
            },
        },
        "required": [],
    },
}


_SET_PROFILE_SCHEMA = {
    "name": "minebean_set_profile",
    "description": (
        "Save the default strategy preset for this session and future cron "
        "runs. Persists to $HERMES_HOME/.minebean/profile. No on-chain call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": list(STRATEGY_PRESETS),
                "description": "Strategy preset to save.",
            },
        },
        "required": ["profile"],
    },
}


_AUTOSTART_SCHEMA = {
    "name": "minebean_autostart",
    "description": (
        "Start an autonomous mining cron job that calls hermes-minebean-deploy "
        "on a schedule. Enforces MINEBEAN_MAX_DEPLOYS_PER_DAY. Requires a signer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "schedule": {
                "type": "string",
                "description": "Cron schedule string. Defaults to 'every 5m'.",
                "default": "every 5m",
            },
            "daily_cap": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Maximum deploys per UTC day. Defaults to 100 or the value "
                    "of MINEBEAN_MAX_DEPLOYS_PER_DAY."
                ),
            },
        },
        "required": [],
    },
}


_AUTOSTOP_SCHEMA = {
    "name": "minebean_autostop",
    "description": "Stop and remove the autonomous mining cron job created by minebean_autostart.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# Order matters for slash command help output and for the LLM's tool list view.
ALL_TOOLS: list[tuple[str, dict, str, bool]] = [
    # (name, schema, emoji, requires_signer)
    ("minebean_status", _STATUS_SCHEMA, "", False),
    ("minebean_pending", _PENDING_SCHEMA, "", False),
    ("minebean_set_profile", _SET_PROFILE_SCHEMA, "", False),
    ("minebean_deploy", _DEPLOY_SCHEMA, "", True),
    ("minebean_claim", _CLAIM_SCHEMA, "", True),
    ("minebean_autostart", _AUTOSTART_SCHEMA, "", True),
    ("minebean_autostop", _AUTOSTOP_SCHEMA, "", False),
]

# Just the tool names, in declared order. Convenience for tests and slash.py.
TOOL_NAMES = tuple(t[0] for t in ALL_TOOLS)
