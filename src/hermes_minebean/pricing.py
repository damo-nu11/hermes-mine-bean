"""BEAN price feed for hermes-mine-bean strategies.

Source priority:
    1. MINEBEAN_BEAN_PRICE_ETH env var (offline override, useful for tests)
    2. MINEBEAN_PRICE_URL env var (custom endpoint)
    3. https://api.minebean.com/api/price (default)

Endpoint contract: returns JSON with a `priceNative` field, BEAN price quoted
in ETH (BEAN/ETH). On any failure (network, parse, missing key) we return 0.0,
which signals strategies to skip rather than compute on bad data.

Caching: process-level dict with a 30s TTL. Strategies call this once per
deploy plan, so a 60s cron interval gets at most one network call per cycle.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.error import URLError

_DEFAULT_PRICE_URL = "https://api.minebean.com/api/price"
_TTL_SECONDS = 30.0

_cache: dict[str, tuple[float, float]] = {}  # url -> (expires_at, price_eth)


def get_bean_price_eth(timeout: float = 5.0) -> float:
    """Return BEAN price in ETH. Returns 0.0 on any failure.

    Order of resolution:
        1. MINEBEAN_BEAN_PRICE_ETH override (parsed as float)
        2. Cached value if still fresh
        3. HTTP GET against price URL, then cache for TTL
    """
    override = (os.environ.get("MINEBEAN_BEAN_PRICE_ETH") or "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            return 0.0

    url = (os.environ.get("MINEBEAN_PRICE_URL") or "").strip() or _DEFAULT_PRICE_URL

    now = time.monotonic()
    cached = _cache.get(url)
    if cached and cached[0] > now:
        return cached[1]

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "hermes-mine-bean/0.2"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        # Real endpoint shape: {"bean": {"priceNative": "0.005870", ...}, ...}
        # Fall back to a top-level priceNative for any future endpoint that
        # decides to flatten the response, but never raise on missing keys.
        raw = (payload.get("bean") or {}).get("priceNative")
        if raw is None:
            raw = payload.get("priceNative")
        if raw is None:
            return 0.0
        price = float(raw)
    except (URLError, ValueError, KeyError, TimeoutError, OSError):
        return 0.0

    if price <= 0 or price > 1.0:  # sanity check: BEAN/ETH should never approach 1
        return 0.0

    _cache[url] = (now + _TTL_SECONDS, price)
    return price


def clear_cache() -> None:
    """Drop the cached price. Useful for tests."""
    _cache.clear()
