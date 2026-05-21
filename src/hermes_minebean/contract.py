"""GridMining contract client for hermes-mine-bean.

Wraps the ABI's view functions with typed Python returns. The write-path
tx builders are inert until the EOASigner submits them.

All addresses are accepted as either lowercase hex or checksum; the client
normalises to checksum before calling.
"""
from __future__ import annotations

from typing import Any

from web3 import Web3

from . import rpc


def _assert_eip1559_tx(tx: dict[str, Any]) -> None:
    """Sanity check: confirm a built tx uses EIP-1559 fee fields, not legacy.

    Base is EIP-1559 native. If web3.py ever emits a legacy `gasPrice` here
    we want to fail loudly rather than silently broadcast with the wrong
    fee semantics (which on Base can result in stuck or under-priced txs).

    Raises RuntimeError if the tx looks legacy. No-op otherwise.
    """
    if "gasPrice" in tx and "maxFeePerGas" not in tx:
        raise RuntimeError(
            "Built tx is legacy (gasPrice) but Base is EIP-1559 native. "
            "Refusing to broadcast. Upgrade web3.py or set maxFeePerGas "
            "and maxPriorityFeePerGas explicitly."
        )


class GridMiningClient:
    """Thin wrapper around the GridMining contract.

    Stateless apart from the Web3 + contract handles. Safe to instantiate per
    request, or to cache and reuse. Reads always work; writes route through
    the EOASigner in `signer.py`.
    """

    def __init__(self, w3: Web3 | None = None) -> None:
        self._w3: Web3 = w3 or rpc.get_web3()
        self._c = rpc.get_gridmining_contract(self._w3)

    @classmethod
    def from_env(cls) -> "GridMiningClient":
        """Build a client from environment variables (the standard path)."""
        return cls()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _checksum(self, address: str) -> str:
        return self._w3.to_checksum_address(address)

    # ------------------------------------------------------------------
    # Round state
    # ------------------------------------------------------------------
    def current_round_info(self) -> dict[str, Any]:
        """Current round id, timing, total deployed wei, and active flag."""
        round_id, start, end, total, remaining, active = (
            self._c.functions.getCurrentRoundInfo().call()
        )
        return {
            "round_id": int(round_id),
            "start_time": int(start),
            "end_time": int(end),
            "total_deployed_wei": str(total),
            "time_remaining_seconds": int(remaining),
            "is_active": bool(active),
        }

    def round_view(self, round_id: int) -> dict[str, Any]:
        """Read the rich `rounds(uint64)` struct for a settled round."""
        (
            start,
            end,
            total_deployed,
            total_winnings,
            winners_deployed,
            winning_block,
            top_miner,
            top_miner_reward,
            beanpot_amount,
            vrf_request_id,
            top_miner_seed,
            settled,
            miner_count,
        ) = self._c.functions.rounds(round_id).call()
        return {
            "round_id": int(round_id),
            "start_time": int(start),
            "end_time": int(end),
            "total_deployed_wei": str(total_deployed),
            "total_winnings_wei": str(total_winnings),
            "winners_deployed_wei": str(winners_deployed),
            "winning_block": int(winning_block),
            "top_miner": top_miner,
            "top_miner_reward_wei": str(top_miner_reward),
            "beanpot_amount_wei": str(beanpot_amount),
            "vrf_request_id": str(vrf_request_id),
            "top_miner_seed": str(top_miner_seed),
            "settled": bool(settled),
            "miner_count": int(miner_count),
        }

    def round_deployed(self, round_id: int) -> list[int]:
        """Per-block deployed total for the round. Returns 25 ints (one per block)."""
        result = self._c.functions.getRoundDeployed(round_id).call()
        return [int(x) for x in result]

    # ------------------------------------------------------------------
    # User state
    # ------------------------------------------------------------------
    def total_pending(self, address: str) -> dict[str, Any]:
        """One-call pending rewards summary for an address."""
        pending_eth, pending_unroasted, pending_roasted, uncheckpointed = (
            self._c.functions.getTotalPendingRewards(self._checksum(address)).call()
        )
        return {
            "address": self._checksum(address),
            "pending_eth_wei": str(pending_eth),
            "pending_unroasted_bean_wei": str(pending_unroasted),
            "pending_roasted_bean_wei": str(pending_roasted),
            "uncheckpointed_round": int(uncheckpointed),
        }

    def pending_eth(self, address: str) -> int:
        """ETH owed to an address in wei."""
        return int(self._c.functions.getPendingETH(self._checksum(address)).call())

    def pending_bean(self, address: str) -> dict[str, Any]:
        """BEAN owed to an address. Returns gross / fee / net split."""
        gross, fee, net = self._c.functions.getPendingBEAN(self._checksum(address)).call()
        return {
            "gross_wei": str(gross),
            "fee_wei": str(fee),
            "net_wei": str(net),
        }

    def miner_info(self, round_id: int, address: str) -> dict[str, Any]:
        """User's deploy mask, per-block amount, and checkpoint flag for a round."""
        mask, amount, checkpointed = self._c.functions.getMinerInfo(
            round_id, self._checksum(address)
        ).call()
        return {
            "round_id": int(round_id),
            "address": self._checksum(address),
            "deployed_mask": int(mask),
            "amount_per_block_wei": str(amount),
            "checkpointed": bool(checkpointed),
        }

    def has_deployed_this_round(self, address: str) -> bool:
        """True if the address already deployed in the current round."""
        info = self.current_round_info()
        if not info["is_active"]:
            return False
        m = self.miner_info(info["round_id"], address)
        return m["deployed_mask"] != 0

    # ------------------------------------------------------------------
    # Beanpot state
    # ------------------------------------------------------------------
    def beanpot_status(self) -> dict[str, Any]:
        """Current beanpot accumulation and pool."""
        accumulation = int(self._c.functions.beanpotAccumulation().call())
        pool = int(self._c.functions.beanpotPool().call())
        return {
            "accumulation_wei": str(accumulation),
            "pool_wei": str(pool),
        }

    # ------------------------------------------------------------------
    # Static constants. Cached on the client instance after first read.
    # Reading all of these at once is expensive on the public RPC
    # (~7 sequential calls), so each individual constant has its own
    # cached accessor for callers that only need one.
    # ------------------------------------------------------------------
    _CONSTANTS_CACHE: dict[str, Any] | None = None

    def constants(self) -> dict[str, Any]:
        """Read and cache static contract constants. Use sparingly on public RPC."""
        if self._CONSTANTS_CACHE is None:
            self._CONSTANTS_CACHE = {
                "grid_size": int(self._c.functions.GRID_SIZE().call()),
                "round_duration_seconds": int(self._c.functions.ROUND_DURATION().call()),
                "min_deploy_wei": str(self._c.functions.MIN_DEPLOY().call()),
                "beanpot_chance": int(self._c.functions.BEANPOT_CHANCE().call()),
                "min_beanpot_accumulation_wei": str(
                    self._c.functions.MIN_BEANPOT_ACCUMULATION().call()
                ),
                "max_beanpot_accumulation_wei": str(
                    self._c.functions.MAX_BEANPOT_ACCUMULATION().call()
                ),
                "max_supply_wei": str(self._c.functions.MAX_SUPPLY().call()),
                "one_bean_wei": str(self._c.functions.ONE_BEAN().call()),
            }
        return self._CONSTANTS_CACHE

    _MIN_DEPLOY_CACHE: int | None = None

    def min_deploy_wei(self) -> int:
        """MIN_DEPLOY constant, cached after first read."""
        if self._MIN_DEPLOY_CACHE is None:
            self._MIN_DEPLOY_CACHE = int(self._c.functions.MIN_DEPLOY().call())
        return self._MIN_DEPLOY_CACHE

    _MIN_BEANPOT_CACHE: int | None = None

    def min_beanpot_wei(self) -> int:
        """MIN_BEANPOT_ACCUMULATION constant, cached."""
        if self._MIN_BEANPOT_CACHE is None:
            self._MIN_BEANPOT_CACHE = int(self._c.functions.MIN_BEANPOT_ACCUMULATION().call())
        return self._MIN_BEANPOT_CACHE

    _MAX_BEANPOT_CACHE: int | None = None

    def max_beanpot_wei(self) -> int:
        """MAX_BEANPOT_ACCUMULATION constant, cached."""
        if self._MAX_BEANPOT_CACHE is None:
            self._MAX_BEANPOT_CACHE = int(self._c.functions.MAX_BEANPOT_ACCUMULATION().call())
        return self._MAX_BEANPOT_CACHE

    def game_started(self) -> bool:
        return bool(self._c.functions.gameStarted().call())

    def prev_round_winning_block(self, current_round_id: int) -> int | None:
        """Return the winning block of the most recently settled round, or None.

        Reads `rounds(currentRoundId - 1)` from the contract. If the previous
        round isn't settled (rare race during round transitions), returns None.
        """
        if current_round_id <= 0:
            return None
        try:
            data = self._c.functions.rounds(current_round_id - 1).call()
            # rounds() returns: (startTime, endTime, totalDeployed, totalWinnings,
            #                    winnersDeployed, winningBlock, topMiner,
            #                    topMinerReward, beanpotAmount, vrfRequestId,
            #                    topMinerSeed, settled, minerCount)
            settled = bool(data[11])
            if not settled:
                return None
            return int(data[5])  # winningBlock
        except Exception:
            return None

    def avg_total_deployed_last_n(self, current_round_id: int, n: int = 3) -> int:
        """Arithmetic mean of last N settled rounds' totalDeployed (wei).

        Used by nostradamus (n=3) and anti-loser (n=3) for T prediction.
        Skips unsettled rounds. Returns 0 if no settled history is available.
        """
        if current_round_id <= 0 or n <= 0:
            return 0
        totals: list[int] = []
        for offset in range(1, n + 1):
            rid = current_round_id - offset
            if rid < 0:
                break
            try:
                data = self._c.functions.rounds(rid).call()
                if not bool(data[11]):  # settled flag
                    continue
                totals.append(int(data[2]))  # totalDeployed
            except Exception:
                continue
        if not totals:
            return 0
        return sum(totals) // len(totals)

    # Cache the coldest-block lookup since it's O(N) RPC calls.
    # Key: (current_round_id, n). Value: (coldest_block_or_None, all_tied_bool).
    # Invalidates per-round automatically because the key includes round id.
    _COLDEST_CACHE: dict[tuple[int, int], tuple[int | None, bool]] = {}

    def coldest_block_last_n(
        self,
        current_round_id: int,
        n: int = 100,
    ) -> tuple[int | None, bool]:
        """Return (coldest_block, all_blocks_tied) over last N settled rounds.

        Coldest = block with the fewest wins. Tiebreaker among coldest blocks:
        the one that won most recently (i.e., skip the most recently-cold block).

        Returns (None, True) if all 25 blocks are tied on win count.
        Returns (None, False) if no settled history is available.

        Cached per (current_round_id, n) tuple. The cache is process-local and
        invalidates automatically when the round id advances.
        """
        if current_round_id <= 0 or n <= 0:
            return (None, False)
        key = (current_round_id, n)
        cached = self._COLDEST_CACHE.get(key)
        if cached is not None:
            return cached

        win_counts = [0] * 25
        last_win_round: dict[int, int] = {}
        rounds_read = 0
        for offset in range(1, n + 1):
            rid = current_round_id - offset
            if rid < 0:
                break
            try:
                data = self._c.functions.rounds(rid).call()
                if not bool(data[11]):  # settled
                    continue
                wb = int(data[5])
                if 0 <= wb < 25:
                    win_counts[wb] += 1
                    last_win_round[wb] = max(last_win_round.get(wb, -1), rid)
                    rounds_read += 1
            except Exception:
                continue

        if rounds_read == 0:
            result: tuple[int | None, bool] = (None, False)
        elif all(c == win_counts[0] for c in win_counts):
            result = (None, True)
        else:
            min_count = min(win_counts)
            coldest_blocks = [i for i, c in enumerate(win_counts) if c == min_count]
            # Tiebreaker: most recent winner among coldest blocks.
            coldest = max(coldest_blocks, key=lambda b: last_win_round.get(b, -1))
            result = (coldest, False)

        self._COLDEST_CACHE[key] = result
        # Bound cache size: keep only the most recent 5 keys.
        if len(self._COLDEST_CACHE) > 5:
            oldest_key = min(self._COLDEST_CACHE.keys(), key=lambda k: k[0])
            self._COLDEST_CACHE.pop(oldest_key, None)
        return result

    def eth_balance(self, address: str) -> int:
        """Return the ETH balance (wei) of an address."""
        return int(self._w3.eth.get_balance(self._checksum(address)))

    # ------------------------------------------------------------------
    # Write-path tx builders. These produce UNSIGNED transaction dicts
    # and never broadcast. The EOASigner in signer.py submits them.
    # ------------------------------------------------------------------
    def build_deploy_tx(
        self,
        from_address: str,
        blocks: list[int] | tuple[int, ...],
        per_block_wei: int,
    ) -> dict[str, Any]:
        """Build an unsigned `deploy(uint8[])` transaction dict.

        Caller passes the resolved blocks and per-block amount from a
        strategy plan. Total ETH value = per_block_wei * len(blocks).

        Gas + fee semantics:
        - `web3.py >= 6` populates EIP-1559 fee fields (`maxFeePerGas`,
          `maxPriorityFeePerGas`) when the chain advertises EIP-1559 support.
          Base mainnet is EIP-1559 native (chain id 8453, post-Bedrock), so
          legacy `gasPrice` should not appear. We assert this in
          _assert_eip1559_tx after build to catch any regression.
        - `nonce` is populated by web3.py via `eth_getTransactionCount(from,
          'pending')`. Callers MUST NOT cache or reuse this dict across
          retries; the signer treats nonce as authoritative at submit time.
        """
        if not blocks:
            raise ValueError("blocks must be non-empty")
        if any(b < 0 or b > 24 for b in blocks):
            raise ValueError("blocks must be in range 0..24")
        if per_block_wei <= 0:
            raise ValueError("per_block_wei must be positive")

        total_wei = per_block_wei * len(blocks)
        fn = self._c.functions.deploy(list(blocks))
        tx = fn.build_transaction({
            "from": self._checksum(from_address),
            "value": total_wei,
            "chainId": self._w3.eth.chain_id,
        })
        _assert_eip1559_tx(tx)
        return tx

    def estimate_deploy_gas(
        self,
        from_address: str,
        blocks: list[int] | tuple[int, ...],
        per_block_wei: int,
    ) -> int:
        """eth_estimateGas for a deploy call. Does NOT broadcast."""
        total_wei = per_block_wei * len(blocks)
        return int(
            self._c.functions.deploy(list(blocks)).estimate_gas({
                "from": self._checksum(from_address),
                "value": total_wei,
            })
        )

    def build_claim_bean_tx(self, from_address: str) -> dict[str, Any]:
        """Build an unsigned `claimBEAN()` transaction dict (EIP-1559)."""
        tx = self._c.functions.claimBEAN().build_transaction({
            "from": self._checksum(from_address),
            "value": 0,
            "chainId": self._w3.eth.chain_id,
        })
        _assert_eip1559_tx(tx)
        return tx

    def build_claim_eth_tx(self, from_address: str) -> dict[str, Any]:
        """Build an unsigned `claimETH()` transaction dict (EIP-1559)."""
        tx = self._c.functions.claimETH().build_transaction({
            "from": self._checksum(from_address),
            "value": 0,
            "chainId": self._w3.eth.chain_id,
        })
        _assert_eip1559_tx(tx)
        return tx

    def estimate_claim_bean_gas(self, from_address: str) -> int:
        """eth_estimateGas for claimBEAN. Does NOT broadcast."""
        return int(
            self._c.functions.claimBEAN().estimate_gas({
                "from": self._checksum(from_address),
            })
        )

    def estimate_claim_eth_gas(self, from_address: str) -> int:
        """eth_estimateGas for claimETH. Does NOT broadcast."""
        return int(
            self._c.functions.claimETH().estimate_gas({
                "from": self._checksum(from_address),
            })
        )
