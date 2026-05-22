"""Venice (VVV) read-only staking awareness.

v0.4 surfaces the user's VVV + sVVV balances on Base mainnet so the agent
can reason about "do I have free Venice inference allowance from staking?"
without leaving the Hermes session.

Read-only by design. Stake / unstake / mint-DIEM are deferred until v0.5
once we can audit a proper allowance proof flow with on-chain receipts.

Contract addresses (Base mainnet, chainId 8453):
    VVV  ERC-20  0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
    sVVV ERC-20  0x321b7ff75154472B18EDb199033fF4D116F340Ff

Both are standard ERC-20 surfaces (balanceOf, decimals, totalSupply, symbol,
name). We don't need a full ABI in package data — a four-method inline ABI
covers everything this module reads.

References:
- https://basescan.org/token/0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
- https://basescan.org/token/0x321b7ff75154472b18edb199033ff4d116f340ff
- https://venice.ai/lp/vvv
- https://venice.ai/lp/diem
"""
from __future__ import annotations

import logging
from typing import Any, Final

from . import rpc

logger = logging.getLogger(__name__)


# Contract addresses, Base mainnet (chainId 8453). Stored in checksum form
# directly — Web3.toChecksumAddress is applied at contract-instantiation
# time anyway, but storing in canonical mixed-case avoids confusing diffs.
VVV_ADDRESS: Final[str] = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
SVVV_ADDRESS: Final[str] = "0x321b7ff75154472B18EDb199033fF4D116F340Ff"


# Minimal ERC-20 ABI: only the read methods we need. Full token transfer /
# approve surface intentionally omitted so this module is structurally
# unable to issue writes against the VVV / sVVV contracts.
_ERC20_READ_ABI: Final[list[dict[str, Any]]] = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _contract(address: str) -> Any:
    """Return a Web3 contract bound to the read-only ERC-20 ABI at address."""
    w3 = rpc.get_web3()
    return w3.eth.contract(
        address=w3.to_checksum_address(address),
        abi=_ERC20_READ_ABI,
    )


def _balance_of(contract: Any, address: str) -> int:
    """Read balanceOf in raw wei. Returns 0 on RPC error."""
    w3 = rpc.get_web3()
    try:
        return int(contract.functions.balanceOf(w3.to_checksum_address(address)).call())
    except Exception as exc:
        logger.info("balanceOf failed for %s: %s", address, exc)
        return 0


def get_vvv_status(address: str) -> dict[str, Any]:
    """Return VVV + sVVV balance snapshot for the given address.

    All amounts returned in raw wei (uint256). Callers format for display.
    Schema:
        {
            "ok": bool,
            "address": str,
            "vvv": { "address": str, "balance_wei": int, "decimals": 18 },
            "svvv": { "address": str, "balance_wei": int, "decimals": 18 },
            "has_stake": bool,            # True iff svvv.balance_wei > 0
            "has_vvv_holding": bool,      # True iff vvv.balance_wei > 0
        }

    Errors return ok=False with an error field. Per-token balance reads
    silently degrade to 0 so a partial RPC failure still surfaces useful
    data (e.g. VVV balance fetched even if sVVV times out).
    """
    try:
        w3 = rpc.get_web3()
        checksum = w3.to_checksum_address(address)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"invalid_address: {exc}",
        }

    vvv = _contract(VVV_ADDRESS)
    svvv = _contract(SVVV_ADDRESS)

    vvv_wei = _balance_of(vvv, checksum)
    svvv_wei = _balance_of(svvv, checksum)

    return {
        "ok": True,
        "address": checksum,
        "vvv": {
            "address": VVV_ADDRESS,
            "balance_wei": vvv_wei,
            "decimals": 18,
        },
        "svvv": {
            "address": SVVV_ADDRESS,
            "balance_wei": svvv_wei,
            "decimals": 18,
        },
        "has_stake": svvv_wei > 0,
        "has_vvv_holding": vvv_wei > 0,
    }
