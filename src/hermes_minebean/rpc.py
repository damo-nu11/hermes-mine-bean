"""Web3 client setup for hermes-mine-bean.

Single source of truth for RPC connection. Two load-bearing tweaks:

1. User-Agent header on every request — public Base mainnet RPC sits behind
   Cloudflare and rejects empty UAs with error 1010.
2. Retry-on-429 wrapper — public Base RPC throttles aggressively (~10 req/s
   anonymous). We retry with exponential backoff so a deploy plan that fires
   ~10 RPC calls doesn't trip on the first burst. Users with a premium RPC
   (Alchemy, QuickNode) should set BASE_RPC_URL and won't see this kick in.
"""
from __future__ import annotations

import json
import logging
import random
import time
from functools import lru_cache
from importlib import resources
from typing import Any

import requests
from web3 import HTTPProvider, Web3

from . import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry-on-429 provider wrapper
# ---------------------------------------------------------------------------
class _RetryingHTTPProvider(HTTPProvider):
    """HTTPProvider that retries on HTTP 429 with exponential backoff + jitter.

    Configuration via env vars:
      MINEBEAN_RPC_MAX_RETRIES   default 4 (so up to 5 attempts total)
      MINEBEAN_RPC_BASE_BACKOFF  default 0.5 (seconds, base)
      MINEBEAN_RPC_MAX_BACKOFF   default 8.0 (seconds, cap per retry)

    Only retries on 429, and ONLY for read methods. Write methods
    (eth_sendRawTransaction, eth_sendTransaction) propagate 429s immediately
    so the cron entry can fail and retry on the next tick with a fresh tx.
    Retrying a write risks double-broadcast: the first call may have landed
    server-side before returning 429, and a retry would put a second signed
    tx in the mempool.

    Any non-429 error and any error on a non-read method propagates straight
    to the web3.py caller without retry.
    """

    # Read methods that are safe to retry. Anything not in this set
    # propagates 429s without retry.
    _RETRYABLE_METHODS = frozenset({
        "eth_call",
        "eth_estimateGas",
        "eth_getBalance",
        "eth_getBlockByHash",
        "eth_getBlockByNumber",
        "eth_blockNumber",
        "eth_chainId",
        "eth_getCode",
        "eth_getLogs",
        "eth_getStorageAt",
        "eth_getTransactionByHash",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
        "eth_gasPrice",
        "eth_feeHistory",
        "eth_maxPriorityFeePerGas",
        "net_version",
        "web3_clientVersion",
    })

    def make_request(self, method: str, params: Any) -> Any:
        import os

        max_retries = int(os.environ.get("MINEBEAN_RPC_MAX_RETRIES") or 4)
        base_backoff = float(os.environ.get("MINEBEAN_RPC_BASE_BACKOFF") or 0.5)
        max_backoff = float(os.environ.get("MINEBEAN_RPC_MAX_BACKOFF") or 8.0)

        is_retryable = method in self._RETRYABLE_METHODS

        attempt = 0
        while True:
            try:
                return super().make_request(method, params)
            except requests.exceptions.HTTPError as exc:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None)
                if status != 429 or not is_retryable or attempt >= max_retries:
                    if status == 429 and not is_retryable:
                        logger.warning(
                            "RPC 429 on write method %s; NOT retrying "
                            "(double-broadcast risk). Caller should rebuild "
                            "and retry on next cron tick.",
                            method,
                        )
                    raise
                sleep_s = min(max_backoff, base_backoff * (2 ** attempt))
                sleep_s += random.uniform(0, sleep_s * 0.25)  # jitter
                logger.warning(
                    "RPC 429 on %s (attempt %d/%d), sleeping %.2fs",
                    method, attempt + 1, max_retries + 1, sleep_s,
                )
                time.sleep(sleep_s)
                attempt += 1


def _build_provider(rpc_url: str) -> HTTPProvider:
    """RetryingHTTPProvider with the Cloudflare-safe User-Agent header attached."""
    request_kwargs = {
        "timeout": 30,
        "headers": {
            "User-Agent": config.DEFAULT_USER_AGENT,
            "Content-Type": "application/json",
        },
    }
    return _RetryingHTTPProvider(rpc_url, request_kwargs=request_kwargs)


@lru_cache(maxsize=4)
def get_web3(rpc_url: str | None = None) -> Web3:
    """Return a cached Web3 instance for the given RPC URL.

    Defaults to whatever config.get_rpc_url() resolves to.
    """
    url = rpc_url or config.get_rpc_url()
    return Web3(_build_provider(url))


@lru_cache(maxsize=1)
def load_gridmining_abi() -> list[dict[str, Any]]:
    """Load the GridMining ABI from the package data ABI directory."""
    abi_text = resources.files("hermes_minebean.abi").joinpath("gridmining.json").read_text()
    return json.loads(abi_text)


def get_gridmining_contract(w3: Web3 | None = None) -> Any:
    """Return a web3 Contract instance bound to GridMining on the active network."""
    w3 = w3 or get_web3()
    address = w3.to_checksum_address(config.get_gridmining_address())
    abi = load_gridmining_abi()
    return w3.eth.contract(address=address, abi=abi)


def health_check() -> dict[str, Any]:
    """Quick sanity check: confirm the RPC is reachable and chain id matches.

    Returns a dict with status info. Used by tools.minebean_status to add a
    diagnostic block when the user runs it without a configured signer.
    """
    try:
        w3 = get_web3()
        connected = w3.is_connected()
        chain_id = w3.eth.chain_id if connected else None
        expected = config.get_chain_id()
        return {
            "ok": connected and chain_id == expected,
            "connected": connected,
            "chain_id": chain_id,
            "expected_chain_id": expected,
            "rpc_url": config.get_rpc_url(),
            "network": config.get_network(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "rpc_url": config.get_rpc_url(),
            "network": config.get_network(),
        }
