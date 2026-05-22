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


_INFERENCE_STATUS_SCHEMA = {
    "name": "minebean_inference_status",
    "description": (
        "Return the active LLM inference provider Hermes is routing through. "
        "The MineBean plugin defaults to Venice when nothing is configured "
        "and respects HERMES_INFERENCE_PROVIDER overrides (openai, anthropic, "
        "openrouter, ollama, lmstudio). The v0.4 payload also exposes the "
        "resolved base URL, default model, and per-provider configured state."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


_VVV_STATUS_SCHEMA = {
    "name": "minebean_vvv_status",
    "description": (
        "Read the caller's VVV and sVVV (staked Venice token) balances on "
        "Base mainnet. Useful for checking whether the user qualifies for "
        "free Venice inference allowance via staking. Defaults the lookup "
        "address to MINEBEAN_MINER_ADDRESS when no address is passed. "
        "Read-only: this tool never broadcasts, signs, or modifies on-chain state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": (
                    "EOA address to look up balances for. Defaults to "
                    "MINEBEAN_MINER_ADDRESS when omitted."
                ),
            },
        },
        "required": [],
    },
}


_CHAT_SCHEMA = {
    "name": "minebean_chat",
    "description": (
        "Send a single prompt to the configured LLM inference provider "
        "(Venice by default) and return the response. Useful when the agent "
        "wants ad-hoc reasoning, a second opinion, or to route a specific "
        "call through Venice's privacy-preserving inference without leaving "
        "the Hermes session. Respects HERMES_INFERENCE_PROVIDER overrides; "
        "callers can also pin a provider or model per call. Does not modify "
        "on-chain state or trigger broadcasts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "User-side prompt to send to the model.",
                "maxLength": 100000,
            },
            "system": {
                "type": "string",
                "description": "Optional system message to prepend before the prompt.",
                "maxLength": 20000,
            },
            "provider": {
                "type": "string",
                "enum": ["venice", "openai", "anthropic", "openrouter", "ollama", "lmstudio"],
                "description": (
                    "Override the active inference provider for this call only. "
                    "Defaults to whatever HERMES_INFERENCE_PROVIDER resolves to "
                    "(Venice unless the user has pinned a different provider)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Override the model id for this call. Defaults to the "
                    "provider's configured default (e.g. venice-uncensored for Venice)."
                ),
            },
            "max_tokens": {
                "type": "integer",
                "description": "Max completion tokens. Defaults to 1024.",
                "minimum": 1,
                "maximum": 8192,
            },
            "temperature": {
                "type": "number",
                "description": "Sampling temperature. Defaults to 0.7.",
                "minimum": 0,
                "maximum": 2,
            },
        },
        "required": ["prompt"],
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
    ("minebean_inference_status", _INFERENCE_STATUS_SCHEMA, "", False),
    ("minebean_chat", _CHAT_SCHEMA, "", False),
    ("minebean_vvv_status", _VVV_STATUS_SCHEMA, "", False),
]

# Just the tool names, in declared order. Convenience for tests and slash.py.
TOOL_NAMES = tuple(t[0] for t in ALL_TOOLS)
