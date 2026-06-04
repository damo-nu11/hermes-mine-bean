"""Cap invariant: no resolved plan may ever outspend MINEBEAN_MAX_DEPLOY_WEI.

These tests lock the financial safety guarantee so a future strategy refactor
cannot silently reintroduce an overspend. They cover:

1. enforce_max_deploy_cap as a pure unit (clamp, skip-on-too-low, no-cap passthrough).
2. The beanpot-hunter overspend reproduction: resolve_strategy ALONE can exceed
   the cap (its campaign-sizing branch is unclamped), and the rail closes it.
3. Every strategy preset: resolve_strategy -> enforce_max_deploy_cap never exceeds
   the cap, across a low cap and a generous cap.
"""
from __future__ import annotations

import pytest

from hermes_minebean.strategies import (
    ALL_BLOCKS,
    DeployPlan,
    StrategyContext,
    enforce_max_deploy_cap,
    resolve_strategy,
)

MIN_DEPLOY = 2_500_000_000_000  # 2.5e12 wei, ~contract per-block floor
WEI = 10**18


def _plan(per_block: int, n_blocks: int, *, skip: bool = False) -> DeployPlan:
    blocks = tuple(range(n_blocks))
    return DeployPlan(
        profile="test",
        should_skip=skip,
        skip_reason="pre-skip" if skip else None,
        blocks=() if skip else blocks,
        per_block_wei=0 if skip else per_block,
        total_wei=0 if skip else per_block * n_blocks,
        notes=(),
    )


# ---------------------------------------------------------------------------
# 1. enforce_max_deploy_cap as a pure unit
# ---------------------------------------------------------------------------
def test_clamp_reduces_per_block_to_honour_cap():
    # Shiek's overspend shape: 5e13/block x 25 = 1.25e15, cap 1e14.
    cap = 10**14
    out = enforce_max_deploy_cap(_plan(5 * 10**13, 25), cap, MIN_DEPLOY)
    assert out.per_block_wei == cap // 25
    assert out.per_block_wei * len(out.blocks) <= cap
    assert not out.should_skip


def test_under_cap_is_untouched():
    plan = _plan(2 * 10**12, 25)
    out = enforce_max_deploy_cap(plan, 10**14, MIN_DEPLOY)
    assert out is plan  # returned unchanged


def test_no_cap_is_untouched():
    plan = _plan(5 * 10**13, 25)
    out = enforce_max_deploy_cap(plan, 0, MIN_DEPLOY)
    assert out is plan


def test_exact_cap_boundary_is_untouched():
    # per_block * blocks == cap exactly; must NOT clamp (strict >).
    plan = _plan(4 * 10**12, 25)  # == 1e14
    out = enforce_max_deploy_cap(plan, 10**14, MIN_DEPLOY)
    assert out is plan


def test_cap_too_low_converts_to_skip():
    # cap 1e13 over 25 blocks = 4e11/block < MIN_DEPLOY -> skip, no broadcast.
    out = enforce_max_deploy_cap(_plan(5 * 10**13, 25), 10**13, MIN_DEPLOY)
    assert out.should_skip
    assert out.blocks == ()
    assert out.per_block_wei == 0
    assert "minimum" in out.skip_reason


def test_pre_skipped_plan_is_untouched():
    plan = _plan(0, 25, skip=True)
    out = enforce_max_deploy_cap(plan, 10**14, MIN_DEPLOY)
    assert out is plan


def test_24_block_boundary():
    out = enforce_max_deploy_cap(_plan(5 * 10**13, 24), 10**14, MIN_DEPLOY)
    assert out.per_block_wei == 10**14 // 24
    assert out.per_block_wei * len(out.blocks) <= 10**14
    assert len(out.blocks) == 24


# ---------------------------------------------------------------------------
# 2. beanpot-hunter overspend reproduction (proves the rail is load-bearing)
# ---------------------------------------------------------------------------
def _beanpot_overspend_ctx() -> tuple[StrategyContext, int]:
    """Context where 0.5%-of-vault (campaign floor) exceeds the cap, so the
    campaign-sizing branch lands above max_deploy and bypasses the clamp."""
    cap = 10**16  # 0.01 ETH cap
    ctx = StrategyContext(
        current_round_id=1,
        grid_state_wei=tuple([0] * 25),
        current_total_deployed_wei=2 * 10**16,  # T = 0.02 ETH (>0)
        prev_round_winning_block=None,
        beanpot_accumulation_wei=0,
        beanpot_pool_wei=100 * WEI,  # 100 BEAN, above 62.16 threshold
        min_deploy_wei=MIN_DEPLOY,
        min_beanpot_accumulation_wei=0,
        max_beanpot_accumulation_wei=0,
        bean_price_eth=0.005,
        vault_balance_wei=10 * WEI,  # 10 ETH -> 0.5% = 0.05 ETH > cap
        max_deploy_wei=cap,
    )
    return ctx, cap


def test_beanpot_hunter_overspends_without_the_rail():
    ctx, cap = _beanpot_overspend_ctx()
    raw = resolve_strategy("beanpot-hunter", ctx)
    assert not raw.should_skip, "expected a live deploy for this context"
    # The bug: the resolved plan alone outspends the cap.
    assert raw.per_block_wei * len(raw.blocks) > cap


def test_rail_closes_the_beanpot_hunter_overspend():
    ctx, cap = _beanpot_overspend_ctx()
    raw = resolve_strategy("beanpot-hunter", ctx)
    capped = enforce_max_deploy_cap(raw, cap, ctx.min_deploy_wei)
    assert capped.per_block_wei * len(capped.blocks) <= cap
    assert not capped.should_skip
    assert capped.blocks == raw.blocks  # same block selection, just smaller


# ---------------------------------------------------------------------------
# 3. Every strategy: resolved + capped plan never exceeds the cap
# ---------------------------------------------------------------------------
def _ctx_for(profile: str, cap: int) -> StrategyContext:
    """A context that yields a live (non-skip) deploy for each strategy."""
    base = dict(
        current_round_id=10,
        grid_state_wei=tuple([10**14] * 25),
        current_total_deployed_wei=2 * 10**16,  # T = 0.02 ETH, below THRESHOLD
        prev_round_winning_block=7,
        beanpot_accumulation_wei=0,
        beanpot_pool_wei=100 * WEI,
        min_deploy_wei=MIN_DEPLOY,
        min_beanpot_accumulation_wei=0,
        max_beanpot_accumulation_wei=0,
        bean_price_eth=0.005,
        last_3_rounds_avg_total_wei=2 * 10**16,
        coldest_block_index=3,
        all_blocks_tied=False,
        vault_balance_wei=10 * WEI,
        max_deploy_wei=cap,
    )
    return StrategyContext(**base)


@pytest.mark.parametrize(
    "profile",
    ["sniper", "anti-winner", "anti-loser", "nostradamus", "beanpot-hunter"],
)
@pytest.mark.parametrize("cap", [10**15, 10**16, 5 * 10**17])
def test_no_strategy_exceeds_cap(profile: str, cap: int):
    ctx = _ctx_for(profile, cap)
    plan = enforce_max_deploy_cap(
        resolve_strategy(profile, ctx), cap, ctx.min_deploy_wei
    )
    if plan.should_skip:
        assert not plan.blocks  # a skip never broadcasts
    else:
        assert plan.per_block_wei * len(plan.blocks) <= cap
        assert plan.per_block_wei >= ctx.min_deploy_wei
