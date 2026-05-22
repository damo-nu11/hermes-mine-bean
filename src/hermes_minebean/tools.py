"""Tool handlers for hermes-mine-bean.

Seven tool handlers covering the full MineBean lifecycle: live read paths
(status, pending), profile persistence (set_profile), deploy planning and
broadcast, claim, and cron autostart/autostop.

Every handler returns a JSON string. On success: {"ok": true, ...payload}.
On failure: {"ok": false, "stage": "<category>", "error": "..."}. Hermes
expects string returns from tool calls.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signer detection. Delegates to signer.py, which is the single source of
# truth for mode and address resolution (includes readonly mode for dry-run
# testing with just MINEBEAN_MINER_ADDRESS).
# ---------------------------------------------------------------------------

def resolve_signer_mode(env: dict[str, str] | None = None) -> str | None:
    """Return 'eoa', 'bankr', 'readonly', or None. Delegates to signer module."""
    from . import signer as _signer
    return _signer.resolve_signer_mode(env)


def check_configured() -> bool:
    """Hermes gates write tools on this.

    Returns True if a broadcast-capable signer is resolvable. Readonly mode
    is excluded because Hermes uses this to decide whether to expose write
    tools (deploy/claim/autostart) to the LLM at all. Readonly users see
    only read tools (status/pending), which keeps the UX honest.
    """
    from . import signer as _signer
    return _signer.can_broadcast()


def configured_address() -> str | None:
    """Return the active signer's address if resolvable, else None.

    Covers all three signer modes (eoa, bankr, readonly). Used by the
    status handler to populate the caller block and by pending as the
    default address when none is passed.
    """
    from . import signer as _signer
    return _signer.resolve_address()


# ---------------------------------------------------------------------------
# Error helpers. Every handler wraps its body in try/except to keep Hermes
# happy on RPC blips, network outages, or contract reverts.
# ---------------------------------------------------------------------------

def _error(tool: str, stage: str, error: str, **extra: Any) -> str:
    body = {"ok": False, "stage": stage, "tool": tool, "error": error}
    body.update(extra)
    return json.dumps(body)


def _stub_response(tool_name: str, **extra: Any) -> str:
    body = {
        "ok": False,
        "stage": "not_implemented",
        "tool": tool_name,
        "note": "Tool not implemented in this build.",
    }
    body.update(extra)
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Read tools. Real implementations against on-chain GridMining.
# ---------------------------------------------------------------------------

def _handler_status(**_: Any) -> str:
    """Read the current round state from on-chain GridMining.

    Always works without a signer. If a signer IS configured, also includes
    that wallet's pending balances for convenience.
    """
    try:
        # Import lazily so handler import is cheap and dependency-less when
        # the user hasn't pip-installed web3 for some reason.
        from .contract import GridMiningClient

        client = GridMiningClient.from_env()
        info = client.current_round_info()
        beanpot = client.beanpot_status()

        # NOTE: constants() and health_check() were intentionally removed from
        # the default status path. The public Base RPC throttles after ~5
        # sequential calls, and constants don't change on a deployed contract.
        # Use the explicit minebean_constants tool if needed.
        payload: dict[str, Any] = {
            "ok": True,
            "tool": "minebean_status",
            "round": info,
            "beanpot": beanpot,
            "network": "base-mainnet",
            "grid_size": 25,
            "round_duration_seconds": 60,
        }

        # Caller-context block if a signer is configured.
        addr = configured_address()
        if addr is not None:
            try:
                pending = client.total_pending(addr)
                already = client.has_deployed_this_round(addr)
                payload["caller"] = {
                    "address": addr,
                    "signer_mode": resolve_signer_mode(),
                    "already_deployed_this_round": already,
                    "pending": pending,
                }
            except Exception as exc:
                payload["caller"] = {
                    "address": addr,
                    "signer_mode": resolve_signer_mode(),
                    "pending_error": f"{type(exc).__name__}: {exc}",
                }

        return json.dumps(payload)
    except Exception as exc:
        return _error("minebean_status", "rpc_error", f"{type(exc).__name__}: {exc}")


def _handler_pending(address: str | None = None, **_: Any) -> str:
    """Read pending winnings for an address.

    If no address is provided, uses the configured signer. If neither is
    available, returns a clear error rather than crashing.
    """
    try:
        target = (address or "").strip() or configured_address()
        if not target:
            return _error(
                "minebean_pending",
                "no_address",
                "No address provided and no signer configured.",
            )

        from .contract import GridMiningClient

        client = GridMiningClient.from_env()
        total = client.total_pending(target)
        bean = client.pending_bean(target)
        return json.dumps({
            "ok": True,
            "tool": "minebean_pending",
            "address": total["address"],
            "pending_eth_wei": total["pending_eth_wei"],
            "pending_unroasted_bean_wei": total["pending_unroasted_bean_wei"],
            "pending_roasted_bean_wei": total["pending_roasted_bean_wei"],
            "uncheckpointed_round": total["uncheckpointed_round"],
            "bean_breakdown": bean,
        })
    except Exception as exc:
        return _error("minebean_pending", "rpc_error", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Profile + write-path tool handlers.
# ---------------------------------------------------------------------------

def _handler_set_profile(profile: str | None = None, **_: Any) -> str:
    """Save the default strategy preset to $HERMES_HOME/.minebean/profile.

    No on-chain call. The saved profile is used by the cron entry and by
    minebean_deploy when called without an explicit profile argument.
    """
    try:
        from . import state

        if not profile:
            return _error(
                "minebean_set_profile",
                "missing_argument",
                "profile is required (one of: sniper, anti-winner, beanpot-hunter, anti-loser, nostradamus)",
            )
        saved = state.set_profile(profile)
        return json.dumps({
            "ok": True,
            "tool": "minebean_set_profile",
            "profile": saved,
            "saved_to": str(state.profile_path()),
        })
    except ValueError as exc:
        return _error("minebean_set_profile", "invalid_profile", str(exc))
    except Exception as exc:
        return _error(
            "minebean_set_profile", "io_error", f"{type(exc).__name__}: {exc}"
        )


def _handler_deploy(
    profile: str | None = None,
    blocks: list[int] | None = None,
    per_block_wei: str | int | None = None,
    dry_run: bool = True,
    **_: Any,
) -> str:
    """Build a deploy plan for the current round.

    dry_run=True (default) returns the resolved plan without broadcasting.
    dry_run=False is gated behind MINEBEAN_LIVE_BROADCAST_UNLOCKED=1.

    Resolution order for profile:
        1. Explicit profile arg
        2. Saved default from $HERMES_HOME/.minebean/profile
        3. Error (no profile)
    """
    try:
        from . import signer as signer_module
        from . import state as state_module
        from .contract import GridMiningClient
        from .strategies import StrategyContext, resolve_strategy

        # 1. Resolve profile.
        chosen = (profile or "").strip().lower() or state_module.get_profile()
        if not chosen:
            return _error(
                "minebean_deploy",
                "missing_profile",
                "No profile provided and none saved. Run /minebean profile <name> first.",
            )

        # 2. Resolve address (read-only path; signer constructed lazily later).
        address = signer_module.resolve_address()
        if not address:
            return _error(
                "minebean_deploy",
                "no_address",
                "No signer configured. Set MINEBEAN_DEPLOYER_KEY (eoa) or "
                "BANKR_API_KEY + MINEBEAN_MINER_ADDRESS (bankr).",
            )

        # 3. Live broadcast gate: only proceed if MINEBEAN_LIVE_BROADCAST_UNLOCKED=1.
        #    The actual signer construction happens at step 10 below. Bail early
        #    here so we don't burn RPC reads for a request that can't broadcast.
        if not dry_run and not signer_module._live_broadcast_unlocked():
            return _error(
                "minebean_deploy",
                "live_broadcast_blocked",
                "Live deploy is gated. Set MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 "
                "in ~/.hermes/.env when you are ready to send real transactions. "
                "Call with dry_run=true to see the resolved plan.",
            )

        # 4. Daily ceiling check. Informational in dry-run, blocking on live broadcast.
        ceiling = state_module.ceiling_status()
        if not dry_run and int(ceiling["remaining_today"]) <= 0:
            return _error(
                "minebean_deploy",
                "ceiling_reached",
                f"Daily deploy ceiling reached ({ceiling['today_count']}/"
                f"{ceiling['daily_ceiling']} for {ceiling['date_utc']}). "
                "Wait for UTC rollover or raise MINEBEAN_MAX_DEPLOYS_PER_DAY.",
            )

        # 5. Pull on-chain state needed by the strategy.
        client = GridMiningClient.from_env()
        round_info = client.current_round_info()
        beanpot = client.beanpot_status()
        grid_state = client.round_deployed(round_info["round_id"])
        prev_winner = client.prev_round_winning_block(round_info["round_id"])
        min_deploy = client.min_deploy_wei()

        # 6. Already-deployed-this-round check.
        miner = client.miner_info(round_info["round_id"], address)
        already = miner["deployed_mask"] != 0

        # 7. Resolve per-block wei override (caller > env > strategy default).
        per_block_override: int | None = None
        if per_block_wei is not None:
            try:
                per_block_override = int(per_block_wei)
            except (TypeError, ValueError):
                return _error(
                    "minebean_deploy",
                    "invalid_per_block_wei",
                    f"per_block_wei must parse to int, got {per_block_wei!r}",
                )
        elif (env_val := os.environ.get("MINEBEAN_PER_BLOCK_WEI", "").strip()):
            try:
                per_block_override = int(env_val)
            except ValueError:
                per_block_override = None

        # 8. Build strategy context and resolve plan.
        # Lazy reads dispatched on chosen profile to avoid paying for inputs
        # the strategy won't use. Coldest-block (anti-loser only) is the
        # expensive one (up to 100 sequential RPC calls, cached).
        if chosen == "beanpot-hunter":
            min_beanpot = client.min_beanpot_wei()
            max_beanpot = client.max_beanpot_wei()
        else:
            min_beanpot = 0
            max_beanpot = 0

        # BEAN price feed (every strategy needs it for the threshold).
        from . import pricing
        bean_price_eth = pricing.get_bean_price_eth()

        # Last 3 rounds avg (nostradamus, anti-loser).
        avg_last_3_wei = 0
        if chosen in ("nostradamus", "anti-loser"):
            avg_last_3_wei = client.avg_total_deployed_last_n(
                round_info["round_id"], n=3
            )

        # Coldest block over last 100 rounds (anti-loser only).
        coldest_block = None
        all_tied = False
        if chosen == "anti-loser":
            coldest_block, all_tied = client.coldest_block_last_n(
                round_info["round_id"], n=100
            )

        # Vault balance + max deploy (beanpot-hunter campaign sizing,
        # also fed to other strategies for the X* clamp).
        vault_balance_wei = 0
        if chosen == "beanpot-hunter":
            try:
                vault_balance_wei = client.eth_balance(address)
            except Exception:
                vault_balance_wei = 0

        max_deploy_env = (os.environ.get("MINEBEAN_MAX_DEPLOY_WEI") or "").strip()
        try:
            max_deploy_wei = int(max_deploy_env) if max_deploy_env else 0
        except ValueError:
            max_deploy_wei = 0

        # Historical max beanpot (env override, otherwise strategy default).
        max_bp_env = (os.environ.get("MINEBEAN_BEANPOT_HISTORICAL_MAX_BEAN") or "").strip()
        try:
            historical_max_bean = float(max_bp_env) if max_bp_env else 700.0
        except ValueError:
            historical_max_bean = 700.0

        ctx = StrategyContext(
            current_round_id=round_info["round_id"],
            grid_state_wei=tuple(grid_state),
            current_total_deployed_wei=int(round_info["total_deployed_wei"]),
            prev_round_winning_block=prev_winner,
            beanpot_accumulation_wei=int(beanpot["accumulation_wei"]),
            beanpot_pool_wei=int(beanpot["pool_wei"]),
            min_deploy_wei=min_deploy,
            min_beanpot_accumulation_wei=min_beanpot,
            max_beanpot_accumulation_wei=max_beanpot,
            per_block_wei_override=per_block_override,
            bean_price_eth=bean_price_eth,
            last_3_rounds_avg_total_wei=avg_last_3_wei,
            coldest_block_index=coldest_block,
            all_blocks_tied=all_tied,
            vault_balance_wei=vault_balance_wei,
            max_deploy_wei=max_deploy_wei,
            beanpot_historical_max_bean=historical_max_bean,
        )

        # Caller can also override blocks explicitly.
        if blocks is not None:
            try:
                explicit_blocks = tuple(int(b) for b in blocks)
            except (TypeError, ValueError):
                return _error(
                    "minebean_deploy",
                    "invalid_blocks",
                    "blocks must be a list of ints",
                )
            if any(b < 0 or b > 24 for b in explicit_blocks):
                return _error(
                    "minebean_deploy",
                    "invalid_blocks",
                    "block indices must be in 0..24",
                )
            per_block = per_block_override or min_deploy
            from .strategies import DeployPlan

            plan = DeployPlan(
                profile=f"{chosen}+explicit_blocks",
                should_skip=False,
                skip_reason=None,
                blocks=explicit_blocks,
                per_block_wei=per_block,
                total_wei=per_block * len(explicit_blocks),
                notes=("caller provided explicit blocks, bypassing strategy",),
            )
        else:
            plan = resolve_strategy(chosen, ctx)

        # 9. Estimate gas if the plan would broadcast (skip if already deployed).
        gas_estimate: int | None = None
        gas_error: str | None = None
        if not plan.should_skip and not already and plan.blocks:
            try:
                gas_estimate = client.estimate_deploy_gas(
                    address, list(plan.blocks), plan.per_block_wei
                )
            except Exception as exc:
                gas_error = f"{type(exc).__name__}: {exc}"

        # 10. Broadcast (live mode only). Skipped on dry-run, on plan-skip, on
        # already-deployed-this-round, on gas estimation failure (would revert).
        broadcast: dict[str, Any] | None = None
        if (
            not dry_run
            and not plan.should_skip
            and not already
            and plan.blocks
            and gas_error is None
        ):
            try:
                tx = client.build_deploy_tx(
                    address, list(plan.blocks), plan.per_block_wei
                )
                signer = signer_module.make_signer(w3=client._w3)
                broadcast = signer.submit_tx(tx, wait=True)
                if broadcast.get("ok"):
                    state_module.increment_today_count()
            except Exception as exc:
                broadcast = {
                    "ok": False,
                    "stage": "broadcast_exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "tx_hash": None,
                    "receipt": None,
                }

        return json.dumps({
            "ok": True,
            "tool": "minebean_deploy",
            "dry_run": dry_run,
            "address": address,
            "profile": plan.profile,
            "round_id": round_info["round_id"],
            "round_time_remaining_seconds": round_info["time_remaining_seconds"],
            "already_deployed_this_round": already,
            "plan": {
                "should_skip": plan.should_skip,
                "skip_reason": plan.skip_reason,
                "blocks": list(plan.blocks),
                "block_count": len(plan.blocks),
                "per_block_wei": str(plan.per_block_wei),
                "total_wei": str(plan.total_wei),
                "notes": list(plan.notes),
            },
            "gas": {
                "estimate": gas_estimate,
                "error": gas_error,
            },
            "ceiling": ceiling,
            "broadcast": broadcast,
        })
    except Exception as exc:
        return _error("minebean_deploy", "rpc_error", f"{type(exc).__name__}: {exc}")


def _handler_claim(dry_run: bool = True, **_: Any) -> str:
    """Build a claim plan for the configured signer's pending balances.

    Reads pending ETH and pending BEAN, identifies which claim function(s)
    to call, and (in dry-run) returns the plan. Live broadcast gated behind
    MINEBEAN_LIVE_BROADCAST_UNLOCKED.
    """
    try:
        from . import signer as signer_module
        from .contract import GridMiningClient

        address = signer_module.resolve_address()
        if not address:
            return _error(
                "minebean_claim",
                "no_address",
                "No signer configured. Set MINEBEAN_DEPLOYER_KEY (eoa) or "
                "BANKR_API_KEY + MINEBEAN_MINER_ADDRESS (bankr).",
            )

        if not dry_run:
            return _error(
                "minebean_claim",
                "live_broadcast_blocked",
                "Live claim is gated. Set MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 "
                "when you are ready to send real transactions. Call with "
                "dry_run=true to see what would be claimed.",
            )

        client = GridMiningClient.from_env()
        total = client.total_pending(address)
        pending_eth = int(total["pending_eth_wei"])
        pending_bean = int(total["pending_unroasted_bean_wei"]) + int(
            total["pending_roasted_bean_wei"]
        )

        actions: list[str] = []
        gas: dict[str, Any] = {}
        if pending_eth > 0:
            actions.append("claimETH")
            try:
                gas["claim_eth_gas"] = client.estimate_claim_eth_gas(address)
            except Exception as exc:
                gas["claim_eth_error"] = f"{type(exc).__name__}: {exc}"
        if pending_bean > 0:
            actions.append("claimBEAN")
            try:
                gas["claim_bean_gas"] = client.estimate_claim_bean_gas(address)
            except Exception as exc:
                gas["claim_bean_error"] = f"{type(exc).__name__}: {exc}"

        return json.dumps({
            "ok": True,
            "tool": "minebean_claim",
            "dry_run": True,
            "address": address,
            "pending": total,
            "actions": actions,
            "gas": gas,
            "would_claim": len(actions) > 0,
            "note": (
                "Dry-run only. Live broadcast is gated behind "
                "MINEBEAN_LIVE_BROADCAST_UNLOCKED=1."
            ),
        })
    except Exception as exc:
        return _error("minebean_claim", "rpc_error", f"{type(exc).__name__}: {exc}")


def _handler_autostart(
    schedule: str | None = None,
    daily_cap: int | None = None,
    **_: Any,
) -> str:
    """Install the autonomous mining cron job.

    Writes the wrapper script and invokes `hermes cron add` if Hermes is on
    PATH. Otherwise returns the suggested command for the user to run.

    Requires a broadcast-capable signer (eoa or bankr). Readonly mode is
    rejected since the cron would just spin forever in dry-run otherwise.
    """
    try:
        from . import cron_jobs as _cron
        from . import signer as _signer

        if not _signer.can_broadcast():
            return _error(
                "minebean_autostart",
                "no_signer",
                "Autonomous mining needs a broadcast-capable signer. "
                "Set MINEBEAN_DEPLOYER_KEY (eoa) or BANKR_API_KEY (bankr). "
                "Readonly mode is not enough for the cron loop.",
            )

        result = _cron.autostart(
            schedule=(schedule or "every 60s"),
            daily_cap=daily_cap,
        )
        return json.dumps({
            "ok": True,
            "tool": "minebean_autostart",
            **result,
        })
    except Exception as exc:
        return _error("minebean_autostart", "io_error", f"{type(exc).__name__}: {exc}")


def _handler_autostop(**_: Any) -> str:
    """Remove the autonomous mining cron job (best-effort)."""
    try:
        from . import cron_jobs as _cron

        result = _cron.autostop()
        return json.dumps({
            "ok": True,
            "tool": "minebean_autostop",
            **result,
        })
    except Exception as exc:
        return _error("minebean_autostop", "io_error", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# HANDLERS dict. Lookup table the plugin_entry registration loop walks.
# Keys must match the names in schemas.ALL_TOOLS.
# ---------------------------------------------------------------------------

def _handler_inference_status(**_: Any) -> str:
    """Return the active inference provider and per-provider configured state.

    v0.4 payload exposes resolved base_url, default model, and a configured-
    map across all six providers so the agent can introspect the inference
    posture before making a minebean_chat call.
    """
    from . import inference as _inference
    from . import inference_client as _client

    active = _inference.get_active_provider()
    try:
        active_cfg = _client.get_provider_config(active)
        active_base_url = _client._resolve_base_url(active_cfg)
        active_default_model = active_cfg.default_model
    except KeyError:
        active_base_url = None
        active_default_model = None

    configured_map: dict[str, bool] = {}
    for name in _inference.KNOWN_PROVIDERS:
        try:
            configured_map[name] = _client.provider_configured(name)
        except Exception:
            configured_map[name] = False

    payload = {
        "ok": True,
        "provider": active,
        "default_provider": _inference.DEFAULT_PROVIDER,
        "known_providers": list(_inference.KNOWN_PROVIDERS),
        "active_base_url": active_base_url,
        "active_default_model": active_default_model,
        "configured_providers": configured_map,
        "venice_api_key_configured": _inference.venice_configured(),
        "venice_no_log": _inference.venice_no_log_enabled(),
    }
    return json.dumps(payload)


def _handler_chat(**kwargs: Any) -> str:
    """Send a single prompt to the configured LLM inference provider.

    Read-only side effects: makes an HTTPS call to the provider's chat
    completion endpoint with the user's API key. Does not write env vars,
    files, or on-chain state. Errors surface as structured JSON; the
    agent should retry with a different provider or surface to the user.
    """
    from . import inference_client as _client

    prompt = (kwargs.get("prompt") or "").strip()
    if not prompt:
        return _error("minebean_chat", "missing_prompt", "prompt is required and cannot be empty.")
    # Server-side caps mirror schema maxLength. Defense in depth: a
    # schema-skipping caller still can't bill the user for a 5 MB prompt.
    if len(prompt) > 100000:
        return _error(
            "minebean_chat",
            "prompt_too_long",
            f"prompt is {len(prompt)} chars; max 100000.",
        )

    system = (kwargs.get("system") or "").strip()
    if len(system) > 20000:
        return _error(
            "minebean_chat",
            "system_too_long",
            f"system message is {len(system)} chars; max 20000.",
        )
    provider_override = kwargs.get("provider")
    model_override = kwargs.get("model")
    max_tokens = int(kwargs.get("max_tokens") or 1024)
    temperature = float(kwargs.get("temperature") if kwargs.get("temperature") is not None else 0.7)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        result = _client.chat(
            messages=messages,
            provider=provider_override,
            model=model_override,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except KeyError as exc:
        return _error("minebean_chat", "unknown_provider", str(exc))
    except ValueError as exc:
        # Missing API key for a remote provider.
        return _error("minebean_chat", "missing_api_key", str(exc))
    except RuntimeError as exc:
        # openai-python missing or other client-construction failure.
        return _error("minebean_chat", "client_unavailable", str(exc))
    except Exception as exc:
        # Network, auth, rate-limit, or provider-side error. Keep the
        # exception class name in the payload so the agent has a hint.
        return _error(
            "minebean_chat",
            "provider_error",
            f"{type(exc).__name__}: {exc}",
        )

    return json.dumps(result)


def _handler_vvv_status(**kwargs: Any) -> str:
    """Read VVV + sVVV balances for an address, defaulting to MINEBEAN_MINER_ADDRESS."""
    import os
    from . import staking

    address = (kwargs.get("address") or "").strip()
    if not address:
        address = (os.environ.get("MINEBEAN_MINER_ADDRESS") or "").strip()
    if not address:
        return _error(
            "minebean_vvv_status",
            "missing_address",
            "No address passed and MINEBEAN_MINER_ADDRESS not set in env.",
        )

    try:
        payload = staking.get_vvv_status(address)
    except Exception as exc:
        return _error(
            "minebean_vvv_status",
            "rpc_error",
            f"{type(exc).__name__}: {exc}",
        )
    return json.dumps(payload)


HANDLERS: dict[str, Callable[..., str]] = {
    "minebean_status": _handler_status,
    "minebean_pending": _handler_pending,
    "minebean_set_profile": _handler_set_profile,
    "minebean_deploy": _handler_deploy,
    "minebean_claim": _handler_claim,
    "minebean_autostart": _handler_autostart,
    "minebean_autostop": _handler_autostop,
    "minebean_inference_status": _handler_inference_status,
    "minebean_chat": _handler_chat,
    "minebean_vvv_status": _handler_vvv_status,
}
