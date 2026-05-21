"""CLI entry points for hermes-mine-bean.

The `cron_entry` function is wired to the `hermes-minebean-deploy` console
script in pyproject.toml. Designed for headless cron use via Hermes Agent:

    hermes cron add --no-agent --script "hermes-minebean-deploy --profile sniper --quiet"

Single attempt per invocation (cron schedules the cadence). Reuses the same
plan-resolution path as the minebean_deploy tool handler so behaviour matches
between interactive and autonomous modes.

Live broadcast is gated until MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 is set
(post dev key-handling review). Until then, --dry-run is the hard default.
Pass --no-dry-run to intentionally fail with a clear blocked message.

Nonce + retry policy:
- Each invocation builds a fresh tx via contract.build_deploy_tx, which
  populates `nonce` from `eth_getTransactionCount(from, 'pending')`. No
  caching across invocations.
- We do NOT auto-retry a failed broadcast inside the cron entry. If the
  RPC times out or the tx reverts, we exit 1 and let cron schedule the
  next attempt. Reusing a nonce risks double-broadcast under reorg
  conditions; rebuilding is always safer.
- The daily ceiling counter increments ONLY when the signer returns
  ok=True (confirmed receipt with status=1). A revert does not burn quota.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hermes-minebean-deploy",
        description="Autonomous MineBean deploy. Reads strategy, builds plan, optionally broadcasts.",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Strategy preset. Defaults to saved profile from $HERMES_HOME/.minebean/profile.",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Build plan without broadcasting. Default true (live broadcast gated until Step 2c).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress JSON stdout. Only the exit code communicates outcome.",
    )
    p.add_argument(
        "--ignore-ceiling",
        action="store_true",
        help="Skip daily ceiling check. Use with caution.",
    )
    p.add_argument(
        "--per-block-wei",
        dest="per_block_wei",
        default=None,
        help="Override per-block deploy amount (wei). Default is contract MIN_DEPLOY.",
    )
    return p.parse_args(argv)


def _emit(payload: dict[str, Any], quiet: bool) -> None:
    if quiet:
        return
    print(json.dumps(payload, default=str))


def cron_entry(argv: list[str] | None = None) -> int:
    """Headless deploy entry. Returns 0 on success/skip/ceiling, 1 on error.

    Exit code semantics:
        0 = plan resolved cleanly OR daily ceiling hit (cron should not error)
        1 = recoverable error (RPC, missing config, invalid args)
        130 = SIGINT (user hit Ctrl-C)
    """
    args = _parse_args(argv)

    try:
        from . import signer as signer_module
        from . import state as state_module
        from .tools import HANDLERS

        # 1. Daily ceiling check first. Cheap, saves an RPC if we're capped.
        # Skipped on dry-run because dry-runs don't broadcast, so they never
        # burn quota and shouldn't be blocked by a full ceiling either.
        if not args.ignore_ceiling and not args.dry_run:
            ceiling = state_module.ceiling_status()
            if ceiling["remaining_today"] <= 0:
                _emit(
                    {
                        "ok": False,
                        "stage": "ceiling",
                        "tool": "hermes-minebean-deploy",
                        "error": (
                            f"daily ceiling reached "
                            f"({ceiling['today_count']}/{ceiling['daily_ceiling']})"
                        ),
                        "ceiling": ceiling,
                        "ts_utc": datetime.now(timezone.utc).isoformat(),
                    },
                    args.quiet,
                )
                return 0  # ceiling is not an error from cron's perspective

        # 2. Pre-flight: need at least readonly signer mode to build a plan.
        if signer_module.resolve_signer_mode() is None:
            _emit(
                {
                    "ok": False,
                    "stage": "no_signer",
                    "tool": "hermes-minebean-deploy",
                    "error": (
                        "no signer configured. set MINEBEAN_DEPLOYER_KEY (eoa), "
                        "BANKR_API_KEY (bankr), or MINEBEAN_MINER_ADDRESS (readonly)"
                    ),
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                },
                args.quiet,
            )
            return 1

        # 3. Delegate to the deploy tool handler. Same path as the interactive
        # minebean_deploy tool so behaviour is identical between modes.
        deploy = HANDLERS["minebean_deploy"]
        raw = deploy(
            profile=args.profile,
            per_block_wei=args.per_block_wei,
            dry_run=args.dry_run,
        )
        result = json.loads(raw)

        # 4. Counter increment is owned by the deploy handler in tools.py,
        # which gates on broadcast.ok (status == 1). The CLI no longer
        # double-counts. This block intentionally left empty so the cron
        # entry doesn't burn quota on sign_failed / broadcast_failed paths.

        # 5. Annotate result, emit, exit.
        result["ts_utc"] = datetime.now(timezone.utc).isoformat()
        result["cron_mode"] = True
        _emit(result, args.quiet)

        # Success or expected-skip both return 0 (cron should not error-spam).
        if result.get("ok"):
            return 0
        if result.get("stage") in ("ceiling", "skip", "live_broadcast_blocked"):
            return 0
        return 1

    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "stage": "uncaught_exception",
                "tool": "hermes-minebean-deploy",
                "error": f"{type(exc).__name__}: {exc}",
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            },
            args.quiet,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cron_entry())
