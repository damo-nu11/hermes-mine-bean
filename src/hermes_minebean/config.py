"""Configuration constants and environment variable helpers for hermes-mine-bean.

Centralised so contract addresses, RPC defaults, and env-var names live in
one place. Tools and the contract client read from here.
"""
from __future__ import annotations

import os
from typing import Final

# ---------------------------------------------------------------------------
# Chain constants. We default to Base mainnet. If a Sepolia GridMining
# deployment lands later, MINEBEAN_NETWORK=sepolia switches the targets.
# ---------------------------------------------------------------------------
BASE_MAINNET_CHAIN_ID: Final[int] = 8453
BASE_SEPOLIA_CHAIN_ID: Final[int] = 84532

DEFAULT_BASE_MAINNET_RPC: Final[str] = "https://mainnet.base.org"
DEFAULT_BASE_SEPOLIA_RPC: Final[str] = "https://sepolia.base.org"

# Public mainnet RPC sits behind Cloudflare and rejects empty UAs (error 1010).
# Always send this header on RPC calls.
DEFAULT_USER_AGENT: Final[str] = "hermes-mine-bean/0.3.0"

# ---------------------------------------------------------------------------
# Contract addresses on Base mainnet.
# When MINEBEAN_NETWORK=sepolia and we have Sepolia deployments, we can swap
# these out via a dict. Empty placeholders for Sepolia until the address lands.
# ---------------------------------------------------------------------------
GRIDMINING_MAINNET: Final[str] = "0x9632495bDb93FD6B0740Ab69cc6c71C9c01da4f0"
BEAN_MAINNET: Final[str] = "0x5c72992b83E74c4D5200A8E8920fB946214a5A5D"

GRIDMINING_SEPOLIA: Final[str] = ""  # filled in if/when dev confirms a deployment
BEAN_SEPOLIA: Final[str] = ""


def get_network() -> str:
    """Return 'mainnet' or 'sepolia'. Defaults to mainnet."""
    raw = (os.environ.get("MINEBEAN_NETWORK") or "mainnet").strip().lower()
    if raw not in ("mainnet", "sepolia"):
        return "mainnet"
    return raw


def get_chain_id() -> int:
    """Return the chain id for the active network."""
    return BASE_MAINNET_CHAIN_ID if get_network() == "mainnet" else BASE_SEPOLIA_CHAIN_ID


def get_rpc_url() -> str:
    """Return the RPC URL for the active network.

    Precedence: MINEBEAN_RPC_URL > BASE_RPC_URL > network default.
    """
    explicit = (os.environ.get("MINEBEAN_RPC_URL") or os.environ.get("BASE_RPC_URL") or "").strip()
    if explicit:
        return explicit
    if get_network() == "sepolia":
        return DEFAULT_BASE_SEPOLIA_RPC
    return DEFAULT_BASE_MAINNET_RPC


def get_gridmining_address() -> str:
    """Return the GridMining contract address for the active network."""
    if get_network() == "sepolia":
        if not GRIDMINING_SEPOLIA:
            raise RuntimeError(
                "MINEBEAN_NETWORK=sepolia but no Sepolia GridMining address is "
                "configured yet. Set MINEBEAN_GRIDMINING_ADDRESS to override."
            )
        return GRIDMINING_SEPOLIA
    override = (os.environ.get("MINEBEAN_GRIDMINING_ADDRESS") or "").strip()
    return override or GRIDMINING_MAINNET


def get_bean_address() -> str:
    """Return the BEAN token address for the active network."""
    if get_network() == "sepolia":
        return BEAN_SEPOLIA or ""
    override = (os.environ.get("MINEBEAN_BEAN_ADDRESS") or "").strip()
    return override or BEAN_MAINNET
