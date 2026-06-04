"""CLI entry points for hermes-mine-bean.

The `cron_entry` function is wired to the `hermes-minebean-deploy` console
script in pyproject.toml. Designed for headless cron use via Hermes Agent:

    hermes cron add --no-agent --script "hermes-minebean-deploy --profile sniper --quiet"

Single attempt per invocation (cron schedules the cadence). Reuses the same
plan-resolution path as the minebean_deploy tool handler so behaviour matches
between interactive and autonomous modes.

Live broadcast is gated behind MINEBEAN_LIVE_BROADCAST_UNLOCKED=1. Until
the operator sets that flag, --dry-run is the hard default and --no-dry-run
fails with a clear blocked message.

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
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _acquire_run_lock() -> Any | None:
    """Take a non-blocking, per-signer file lock for the duration of the run.

    Cron schedules a fixed cadence, but a single deploy can outlast its slot
    (receipt wait + RPC backoff). Without a lock the next tick fires while the
    prior one still holds an unconfirmed nonce, so both rebuild against the same
    `pending` count and collide. The lock makes an overlapping tick a clean
    no-op instead of a nonce clash.

    Returns the held file object on success, or None if another run holds it.
    The OS releases the lock when this process exits, so a crashed run never
    leaves a stale lock behind (unlike a plain lockfile-exists check).
    """
    import fcntl

    key = (
        os.environ.get("MINEBEAN_MINER_ADDRESS")
        or os.environ.get("MINEBEAN_DEPLOYER_KEY")
        or "default"
    ).lower()
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    lock_path = os.path.join(tempfile.gettempdir(), f"hermes-minebean-{digest}.lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


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
        help="Build plan without broadcasting. Default true (live broadcast gated behind MINEBEAN_LIVE_BROADCAST_UNLOCKED).",
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
    p.add_argument(
        "--claim",
        action="store_true",
        help="After the deploy fire, also claim pending ETH (eth_only). Same broadcast gate and dry-run default as the deploy.",
    )
    p.add_argument(
        "--min-claim-wei",
        dest="min_claim_wei",
        default=None,
        help="Pending-ETH floor (wei) below which the auto-claim is skipped. Defaults to MINEBEAN_MIN_CLAIM_WEI env.",
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

    # Overlap guard: if a previous tick is still in-flight (e.g. waiting on a
    # receipt), skip this one cleanly rather than colliding on the nonce. Dry
    # runs never broadcast, so they don't take the lock. Disable with
    # MINEBEAN_DISABLE_RUN_LOCK=1.
    run_lock = None
    if not args.dry_run and os.environ.get("MINEBEAN_DISABLE_RUN_LOCK") != "1":
        run_lock = _acquire_run_lock()
        if run_lock is None:
            _emit(
                {
                    "ok": True,
                    "stage": "overlap_skip",
                    "tool": "hermes-minebean-deploy",
                    "reason": "previous deploy still in-flight; skipping this tick",
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                },
                args.quiet,
            )
            return 0  # overlap is expected under fast cadence, not an error

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

        # 5. Annotate result, emit.
        result["ts_utc"] = datetime.now(timezone.utc).isoformat()
        result["cron_mode"] = True
        _emit(result, args.quiet)

        # Success or expected-skip both count as ok (cron should not error-spam).
        deploy_ok = bool(result.get("ok")) or result.get("stage") in (
            "ceiling",
            "skip",
            "live_broadcast_blocked",
        )

        # 6. Optional auto-claim of pending ETH. Runs AFTER the deploy so the
        # deploy's submit_tx(wait=True) has already settled its nonce before we
        # build the claim tx. eth_only keeps autonomous mode from ever moving
        # BEAN. Same broadcast gate and dry-run default as the deploy.
        claim_ok = True
        if args.claim:
            claim = HANDLERS["minebean_claim"]
            claim_raw = claim(
                dry_run=args.dry_run,
                eth_only=True,
                min_claim_wei=args.min_claim_wei,
            )
            claim_result = json.loads(claim_raw)
            claim_result["ts_utc"] = datetime.now(timezone.utc).isoformat()
            claim_result["cron_mode"] = True
            _emit(claim_result, args.quiet)
            claim_ok = bool(claim_result.get("ok")) or (
                claim_result.get("stage") == "live_broadcast_blocked"
            )

        return 0 if (deploy_ok and claim_ok) else 1

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
