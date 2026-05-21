"""Strategy resolvers for hermes-mine-bean.

Each strategy takes a StrategyContext (current round state + history + market
data) and returns a DeployPlan (which blocks, how much per block, whether to
skip).

CANONICAL FORMULAS as of v0.2, supplied by the MineBean dev team. Each
strategy uses the closed-form EV optimum:

    X* = sqrt(K * P * T) - T

where:
    X = total ETH the miner deploys for this round
    T = ETH already deployed by the field (definition varies per strategy)
    P = BEAN price in ETH (ETH per BEAN)
    K = B / FEE_DRAG (BEAN units), strategy-specific

Anti-Winner uses B = 1.1 (emission + avg beanpot contribution), so K = 10.476.
Sniper / Nostradamus / Anti-Loser use B = 1.0 (emission only), so K = 9.5238.
Beanpot Hunter uses dynamic B = 1.0 + beanpotSize / 777.

All amounts inside this module are normalised to ETH/BEAN floats for the
arithmetic, and converted back to wei integers in the DeployPlan. Callers
should treat the plan as authoritative.

DATA INPUTS the canonical formulas need beyond what's already wired:
    - bean_price_eth (ETH per BEAN)
    - last_3_rounds_avg_total_wei (avg of last 3 settled rounds' totalDeployed)
    - coldest_block_index + all_blocks_tied (anti-loser block selection)
    - vault_balance_wei + max_deploy_wei (beanpot-hunter campaign sizing)
    - beanpot_pool_bean (current beanpot in BEAN units)
    - beanpot_historical_max_pot_bean (default 700 BEAN if not provided)

When a required input is missing or zero, the strategy degrades gracefully
to a clearly-noted skip rather than computing on bad data. Wire the data
sources in tools.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .schemas import STRATEGY_PRESETS

# ---------------------------------------------------------------------------
# Canonical constants (dev spec)
# ---------------------------------------------------------------------------
GRID_SIZE = 25
ALL_BLOCKS: tuple[int, ...] = tuple(range(GRID_SIZE))

# Fee drag the EV formula corrects for (10.5% of deployed ETH lost to fees).
FEE_DRAG = 0.105

# Contract floor: every block must receive at least this much ETH (net of vault fee).
BLOCK_MIN_DEPLOY_ETH = 0.0000025

# Vault fee used to gross up the per-block minimum so the net amount clears the
# contract floor. EOA mode has no vault, but matching the dev's "min deploy"
# formula keeps the plugin and the vault agents on the same page.
VAULT_FEE_FRACTION = 0.01

# Beanpot probability denominator (1-in-N odds per round).
BEANPOT_PROBABILITY_DENOM = 777.0

# Per-strategy K constants. K = B / FEE_DRAG, derived from dev spec.
K_ANTI_WINNER = 1.1 / FEE_DRAG  # 10.4762
K_BASE = 1.0 / FEE_DRAG  # 9.5238

# Beanpot-hunter campaign params.
BEANPOT_DEPLOY_THRESHOLD_BEAN = 62.16
BEANPOT_HISTORICAL_MAX_BEAN_DEFAULT = 700.0
MIN_CAMPAIGN_DEPLOY_FRACTION = 0.005  # 0.5% of vault balance

# Sniper timing params (adaptive binary search, applied at broadcast time, not here).
SNIPER_INITIAL_OFFSET_S = 5.0
SNIPER_MIN_OFFSET_S = 2.0
SNIPER_MAX_OFFSET_S = 10.0
SNIPER_STEP_SIZE_S = 0.5

# Unit conversions.
WEI_PER_ETH = 10**18
BEAN_DECIMALS = 18


# ---------------------------------------------------------------------------
# Context + plan dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyContext:
    """Inputs every strategy may need. Built by the deploy handler."""

    # Existing inputs.
    current_round_id: int
    grid_state_wei: tuple[int, ...]  # per-block cumulative wei in current round
    current_total_deployed_wei: int
    prev_round_winning_block: int | None  # 0-24 or None if previous not settled
    beanpot_accumulation_wei: int
    beanpot_pool_wei: int
    min_deploy_wei: int  # contract MIN_DEPLOY per block, raw
    min_beanpot_accumulation_wei: int
    max_beanpot_accumulation_wei: int
    per_block_wei_override: int | None = None  # caller override

    # Canonical formula inputs. Default to safe values when not wired yet.
    bean_price_eth: float = 0.0  # ETH per BEAN. 0 means price feed missing.
    last_3_rounds_avg_total_wei: int = 0  # avg of last 3 settled rounds' totals
    coldest_block_index: int | None = None  # last-100-round coldest
    all_blocks_tied: bool = False  # anti-loser hard-skip condition
    vault_balance_wei: int = 0  # wallet balance for campaign sizing
    max_deploy_wei: int = 0  # vault cap, 0 means no cap
    beanpot_historical_max_bean: float = BEANPOT_HISTORICAL_MAX_BEAN_DEFAULT


@dataclass(frozen=True)
class DeployPlan:
    """The resolved plan a deploy handler should broadcast (or describe in dry-run)."""

    profile: str
    should_skip: bool
    skip_reason: str | None
    blocks: tuple[int, ...]
    per_block_wei: int
    total_wei: int
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _wei_to_eth(w: int) -> float:
    return w / WEI_PER_ETH


def _eth_to_wei(e: float) -> int:
    return int(e * WEI_PER_ETH)


def _bean_wei_to_bean(w: int) -> float:
    return w / (10**BEAN_DECIMALS)


def _ceil_to(value: float, decimals: int = 7) -> float:
    """Round up to N decimal places. Prevents truncation below the floor."""
    factor = 10**decimals
    return math.ceil(value * factor) / factor


def _min_total_deploy_eth(n_blocks: int) -> float:
    """Grossed-up minimum total deploy for N blocks.

    Per dev spec: (BLOCK_MIN_DEPLOY × N) / (1 − VAULT_FEE_FRACTION).
    """
    return (BLOCK_MIN_DEPLOY_ETH * n_blocks) / (1.0 - VAULT_FEE_FRACTION)


def _optimal_x_eth(k: float, price_eth: float, t_eth: float) -> float:
    """Closed-form EV optimum: X* = sqrt(K × P × T) − T. Returns ETH total."""
    if k <= 0 or price_eth <= 0 or t_eth <= 0:
        return 0.0
    return math.sqrt(k * price_eth * t_eth) - t_eth


def _threshold_eth(k: float, price_eth: float) -> float:
    """THRESHOLD = K × P. Compared against T (also in ETH)."""
    return k * price_eth


def _clamp_total_deploy(
    raw_total_eth: float,
    n_blocks: int,
    max_deploy_wei: int,
) -> float:
    """Clamp X* into [minTotalDeploy, maxTotalDeploy] and ceil to 7 dp."""
    min_total = _min_total_deploy_eth(n_blocks)
    clamped = max(raw_total_eth, min_total)
    if max_deploy_wei > 0:
        clamped = min(clamped, _wei_to_eth(max_deploy_wei))
    return _ceil_to(clamped, decimals=7)


def _per_block_from_total(total_eth: float, n_blocks: int) -> int:
    """Split a total deploy across N blocks evenly. Returns per-block wei."""
    if n_blocks <= 0:
        return 0
    per_block_eth = total_eth / n_blocks
    return _eth_to_wei(per_block_eth)


def _skip(profile: str, reason: str, **extras: object) -> DeployPlan:
    notes = tuple(f"{k}={v}" for k, v in extras.items())
    return DeployPlan(
        profile=profile,
        should_skip=True,
        skip_reason=reason,
        blocks=(),
        per_block_wei=0,
        total_wei=0,
        notes=notes,
    )


def _plan(
    profile: str,
    blocks: tuple[int, ...],
    per_block_wei: int,
    *notes: str,
) -> DeployPlan:
    return DeployPlan(
        profile=profile,
        should_skip=False,
        skip_reason=None,
        blocks=blocks,
        per_block_wei=per_block_wei,
        total_wei=per_block_wei * len(blocks),
        notes=notes,
    )


def _override_or_minimum(
    ctx: StrategyContext,
    profile: str,
    blocks: tuple[int, ...],
    reason: str,
) -> DeployPlan:
    """Build a minimum-deploy plan (used when T=0 or override is set)."""
    n = len(blocks)
    if ctx.per_block_wei_override:
        per_block = ctx.per_block_wei_override
    else:
        per_block = _eth_to_wei(_min_total_deploy_eth(n) / n)
    return _plan(profile, blocks, per_block, reason)


def _resolve_per_block(
    ctx: StrategyContext,
    profile: str,
    blocks: tuple[int, ...],
    t_eth: float,
    k: float,
) -> DeployPlan:
    """Run the K/P/T decision tree common to anti-winner/nostradamus/anti-loser/sniper.

    Returns the per-block plan for the live-X* branch only. Callers handle
    the T=0 and T≥THRESHOLD branches themselves so they can apply different
    behaviour (skip vs. minimum).
    """
    threshold = _threshold_eth(k, ctx.bean_price_eth)
    raw_total = _optimal_x_eth(k, ctx.bean_price_eth, t_eth)
    total = _clamp_total_deploy(raw_total, len(blocks), ctx.max_deploy_wei)
    per_block_wei = _per_block_from_total(total, len(blocks))
    return _plan(
        profile,
        blocks,
        per_block_wei,
        f"T={t_eth:.7f} ETH",
        f"THRESHOLD={threshold:.7f} ETH",
        f"K={k:.4f}",
        f"P={ctx.bean_price_eth:.10f} ETH/BEAN",
        f"X*={raw_total:.7f} ETH (clamped to {total:.7f})",
    )


# ---------------------------------------------------------------------------
# Strategies (canonical math, dev spec v0.2)
# ---------------------------------------------------------------------------
def _anti_winner(ctx: StrategyContext) -> DeployPlan:
    """Deploy to 24 blocks excluding the previous round's winning block.

    Decision tree:
        T == 0                     → deploy minimum
        0 < T < THRESHOLD          → deploy X*
        T >= THRESHOLD             → deploy minimum (for beanpot eligibility)

    Anti-Winner never skips when prev winning block is known. It floors to
    minimum when the grid is over-saturated, keeping beanpot eligibility alive.
    """
    if ctx.prev_round_winning_block is None:
        return _skip(
            "anti-winner",
            "previous round not settled, cannot exclude winner",
        )
    excluded = ctx.prev_round_winning_block
    if excluded < 0 or excluded >= GRID_SIZE:
        return _skip("anti-winner", f"invalid prev winning block {excluded}")
    blocks = tuple(b for b in ALL_BLOCKS if b != excluded)

    if ctx.bean_price_eth <= 0:
        return _override_or_minimum(
            ctx, "anti-winner", blocks,
            "bean_price_eth missing, defaulting to minimum (per-block override applied if set)",
        )

    t_eth = _wei_to_eth(ctx.current_total_deployed_wei)
    threshold = _threshold_eth(K_ANTI_WINNER, ctx.bean_price_eth)

    if t_eth <= 0:
        return _override_or_minimum(
            ctx, "anti-winner", blocks,
            f"empty grid (T=0), deploying minimum across {len(blocks)} blocks",
        )

    if t_eth >= threshold:
        # Negative-EV zone. Keep beanpot eligibility with minimum deploy.
        return _override_or_minimum(
            ctx, "anti-winner", blocks,
            f"T={t_eth:.7f} ETH >= THRESHOLD={threshold:.7f}, "
            "deploying minimum for beanpot eligibility",
        )

    return _resolve_per_block(ctx, "anti-winner", blocks, t_eth, K_ANTI_WINNER)


def _nostradamus(ctx: StrategyContext) -> DeployPlan:
    """Predict T from last 3 rounds, deploy across all 25 blocks or skip.

    T = arithmetic mean of last 3 settled rounds' totalDeployed.
    No live grid read.

    Decision tree:
        T == 0                     → deploy minimum
        0 < T < THRESHOLD          → deploy X*
        T >= THRESHOLD             → SKIP (hard gate)
    """
    if ctx.bean_price_eth <= 0:
        return _skip(
            "nostradamus",
            "bean_price_eth missing, cannot compute THRESHOLD",
        )

    t_eth = _wei_to_eth(ctx.last_3_rounds_avg_total_wei)
    threshold = _threshold_eth(K_BASE, ctx.bean_price_eth)

    if t_eth <= 0:
        return _override_or_minimum(
            ctx, "nostradamus", ALL_BLOCKS,
            "no round history available (T=0), deploying minimum across all 25 blocks",
        )

    if t_eth >= threshold:
        return _skip(
            "nostradamus",
            f"predicted T={t_eth:.7f} ETH >= THRESHOLD={threshold:.7f}, "
            "negative EV, skipping",
            t_eth=f"{t_eth:.7f}",
            threshold_eth=f"{threshold:.7f}",
        )

    return _resolve_per_block(ctx, "nostradamus", ALL_BLOCKS, t_eth, K_BASE)


def _anti_loser(ctx: StrategyContext) -> DeployPlan:
    """Deploy to 24 blocks, skip the coldest in last 100 rounds.

    T = max(currentGridTotal, avgOfLast3) to avoid under-estimating early in
    a round when the grid hasn't filled yet.

    Decision tree:
        all 25 blocks tied         → SKIP (no informative coldest signal)
        T == 0                     → deploy minimum
        0 < T < THRESHOLD          → deploy X*
        T >= THRESHOLD             → SKIP (hard gate)

    Block selection: exclude the coldest block in last 100 rounds. Tiebreaker
    among coldest blocks: skip the one that won most recently (resolved by
    the caller before building the context).
    """
    if ctx.all_blocks_tied:
        return _skip(
            "anti-loser",
            "all 25 blocks tied at same win count, no coldest signal",
        )

    if ctx.coldest_block_index is None:
        return _skip(
            "anti-loser",
            "coldest_block_index missing, cannot exclude",
        )

    coldest = ctx.coldest_block_index
    if coldest < 0 or coldest >= GRID_SIZE:
        return _skip("anti-loser", f"invalid coldest block {coldest}")

    if ctx.bean_price_eth <= 0:
        return _skip("anti-loser", "bean_price_eth missing")

    blocks = tuple(b for b in ALL_BLOCKS if b != coldest)
    grid_total = _wei_to_eth(ctx.current_total_deployed_wei)
    avg_last_3 = _wei_to_eth(ctx.last_3_rounds_avg_total_wei)
    t_eth = max(grid_total, avg_last_3)
    threshold = _threshold_eth(K_BASE, ctx.bean_price_eth)

    if t_eth <= 0:
        return _override_or_minimum(
            ctx, "anti-loser", blocks,
            f"T=0, deploying minimum across {len(blocks)} blocks "
            f"(excluded coldest block {coldest})",
        )

    if t_eth >= threshold:
        return _skip(
            "anti-loser",
            f"T={t_eth:.7f} ETH >= THRESHOLD={threshold:.7f}, "
            "negative EV, skipping",
            coldest_block=coldest,
            t_eth=f"{t_eth:.7f}",
            threshold_eth=f"{threshold:.7f}",
        )

    return _resolve_per_block(ctx, "anti-loser", blocks, t_eth, K_BASE)


def _sniper(ctx: StrategyContext) -> DeployPlan:
    """Deploy across all 25 blocks using live grid T, adaptive timing.

    Timing (adaptive binary search) is handled at broadcast time, not here.
    The strategy returns the per-block plan; the cron entry or supervisor
    decides when in the round window to actually fire.

    Decision tree:
        T == 0                     → deploy minimum
        0 < T < THRESHOLD          → deploy X*
        T >= THRESHOLD             → SKIP (hard gate)
    """
    if ctx.bean_price_eth <= 0:
        return _skip("sniper", "bean_price_eth missing")

    t_eth = _wei_to_eth(ctx.current_total_deployed_wei)
    threshold = _threshold_eth(K_BASE, ctx.bean_price_eth)

    if t_eth <= 0:
        return _override_or_minimum(
            ctx, "sniper", ALL_BLOCKS,
            "empty grid (T=0), deploying minimum across all 25 blocks",
        )

    if t_eth >= threshold:
        return _skip(
            "sniper",
            f"T={t_eth:.7f} ETH >= THRESHOLD={threshold:.7f}, "
            "negative EV, skipping",
            t_eth=f"{t_eth:.7f}",
            threshold_eth=f"{threshold:.7f}",
        )

    plan = _resolve_per_block(ctx, "sniper", ALL_BLOCKS, t_eth, K_BASE)
    timing_note = (
        f"timing: adaptive binary search "
        f"(initial={SNIPER_INITIAL_OFFSET_S}s, "
        f"min={SNIPER_MIN_OFFSET_S}s, max={SNIPER_MAX_OFFSET_S}s, "
        f"step={SNIPER_STEP_SIZE_S}s) handled at broadcast time"
    )
    return DeployPlan(
        profile=plan.profile,
        should_skip=plan.should_skip,
        skip_reason=plan.skip_reason,
        blocks=plan.blocks,
        per_block_wei=plan.per_block_wei,
        total_wei=plan.total_wei,
        notes=plan.notes + (timing_note,),
    )


def _beanpot_hunter(ctx: StrategyContext) -> DeployPlan:
    """Deploy across all 25 blocks with dynamic K and campaign sizing.

    EV constants scale with current beanpot:
        B_beanpot = beanpotSize / 777
        effectiveB = 1.0 + B_beanpot
        K = effectiveB / FEE_DRAG

    Campaign sizing (the real driver):
        deployThreshold = 62.16 BEAN
        historicalMaxPot = beanpot_historical_max_bean (default 700)
        minCampaignDeploy = 0.5% of vault_balance
        campaignProgress = (beanpotSize - 62.16) / (700 - 62.16), capped at 1.0
        campaignSize = minCampaignDeploy + campaignProgress × (maxDeploy - minCampaignDeploy)

    Decision tree:
        beanpotSize < 62.16        → SKIP entirely
        T == 0                     → deploy minimum
        beanpotSize >= 62.16       → deploy max(campaignSize, formulaSize)
                                     (formula acts as a floor)
    """
    if ctx.bean_price_eth <= 0:
        return _skip("beanpot-hunter", "bean_price_eth missing")

    beanpot_bean = _bean_wei_to_bean(ctx.beanpot_pool_wei)

    if beanpot_bean < BEANPOT_DEPLOY_THRESHOLD_BEAN:
        return _skip(
            "beanpot-hunter",
            f"beanpot {beanpot_bean:.2f} BEAN below deploy threshold "
            f"{BEANPOT_DEPLOY_THRESHOLD_BEAN} BEAN",
        )

    if ctx.vault_balance_wei <= 0 or ctx.max_deploy_wei <= 0:
        return _skip(
            "beanpot-hunter",
            "vault_balance_wei or max_deploy_wei missing, "
            "cannot compute campaign sizing",
        )

    t_eth = _wei_to_eth(ctx.current_total_deployed_wei)
    if t_eth <= 0:
        return _override_or_minimum(
            ctx, "beanpot-hunter", ALL_BLOCKS,
            "empty grid (T=0), deploying minimum across all 25 blocks",
        )

    # Dynamic K based on current beanpot.
    b_beanpot = beanpot_bean / BEANPOT_PROBABILITY_DENOM
    effective_b = 1.0 + b_beanpot
    k = effective_b / FEE_DRAG

    # Formula size (the EV optimum).
    raw_total = _optimal_x_eth(k, ctx.bean_price_eth, t_eth)
    formula_total = _clamp_total_deploy(raw_total, GRID_SIZE, ctx.max_deploy_wei)

    # Campaign size (linear interpolation between min and max).
    historical_max = ctx.beanpot_historical_max_bean or BEANPOT_HISTORICAL_MAX_BEAN_DEFAULT
    progress_range = historical_max - BEANPOT_DEPLOY_THRESHOLD_BEAN
    if progress_range <= 0:
        campaign_progress = 1.0
    else:
        campaign_progress = max(
            0.0,
            min(1.0, (beanpot_bean - BEANPOT_DEPLOY_THRESHOLD_BEAN) / progress_range),
        )
    vault_balance_eth = _wei_to_eth(ctx.vault_balance_wei)
    min_campaign_eth = MIN_CAMPAIGN_DEPLOY_FRACTION * vault_balance_eth
    max_deploy_eth = _wei_to_eth(ctx.max_deploy_wei)
    campaign_total = min_campaign_eth + campaign_progress * (max_deploy_eth - min_campaign_eth)

    # Formula acts as a floor.
    total_eth = max(campaign_total, formula_total)
    total_eth = _ceil_to(total_eth, decimals=7)
    per_block_wei = _per_block_from_total(total_eth, GRID_SIZE)

    threshold = _threshold_eth(k, ctx.bean_price_eth)
    return _plan(
        "beanpot-hunter",
        ALL_BLOCKS,
        per_block_wei,
        f"beanpot={beanpot_bean:.2f} BEAN",
        f"K={k:.4f} (dynamic, B={effective_b:.4f})",
        f"P={ctx.bean_price_eth:.10f} ETH/BEAN",
        f"T={t_eth:.7f} ETH",
        f"THRESHOLD={threshold:.7f} ETH",
        f"X*={raw_total:.7f} ETH (clamped to {formula_total:.7f})",
        f"campaign_progress={campaign_progress:.3f}",
        f"campaign_total={campaign_total:.7f} ETH",
        f"total={total_eth:.7f} ETH (max of campaign, formula)",
    )


_HANDLERS = {
    "sniper": _sniper,
    "anti-winner": _anti_winner,
    "anti-loser": _anti_loser,
    "beanpot-hunter": _beanpot_hunter,
    "nostradamus": _nostradamus,
}


def resolve_strategy(profile: str, ctx: StrategyContext) -> DeployPlan:
    """Public entry point. Look up the strategy handler and produce a plan."""
    key = (profile or "").strip().lower()
    if key not in STRATEGY_PRESETS:
        raise ValueError(
            f"Unknown profile {profile!r}. Expected one of: {', '.join(STRATEGY_PRESETS)}"
        )
    handler = _HANDLERS[key]
    return handler(ctx)
