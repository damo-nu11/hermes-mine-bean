"""Signer abstraction for hermes-mine-bean.

Three signer modes:
- eoa: a local private key signs locally and broadcasts via JSON-RPC
- bankr: signing and broadcasting are delegated to api.bankr.bot
- readonly: address-only mode for dry-run testing. Cannot ever broadcast.

Use resolve_signer_mode() and resolve_address() before constructing a real
Signer. The factory make_signer() refuses readonly mode outright and gates
EOA mode behind MINEBEAN_LIVE_BROADCAST_UNLOCKED until the operator explicitly
flips it.

Key handling notes:
- The private key lives in the EOASigner instance for the lifetime of the
  signer object. We do not stuff it in a module-level variable, but we also
  do not pretend the instance attribute is somehow ephemeral. Consumers
  should construct a signer per broadcast and let it go out of scope.
- The derived address is cached at construction time. We never call
  Account.from_key on the hot path.
- We never log the key. On any exception path that might carry key bytes in
  its message, we log only type(exc).__name__ (not the message).
- We log the truncated address (first 6, last 4) at construction time so
  audit trails can verify the right wallet was used.
- Nonce semantics: the unsigned tx dict produced by contract.build_*_tx
  populates a nonce at build time. submit_tx does NOT auto-retry; callers
  that want retry semantics must rebuild the tx so the nonce is fresh.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode + address resolution. No keys touched in readonly mode.
# ---------------------------------------------------------------------------
def resolve_signer_mode(env: dict[str, str] | None = None) -> str | None:
    """Return 'eoa', 'bankr', 'readonly', or None.

    Precedence: explicit MINEBEAN_SIGNER, then auto-detect by:
      1. MINEBEAN_DEPLOYER_KEY set -> eoa
      2. BANKR_API_KEY set -> bankr
      3. MINEBEAN_MINER_ADDRESS set (without keys) -> readonly
      4. Nothing set -> None
    """
    env = env or os.environ
    forced = (env.get("MINEBEAN_SIGNER") or "").strip().lower()
    if forced in ("eoa", "bankr", "readonly"):
        return forced
    if forced:
        return forced  # surface unknowns instead of falling through silently
    if env.get("MINEBEAN_DEPLOYER_KEY"):
        return "eoa"
    if env.get("BANKR_API_KEY"):
        return "bankr"
    if env.get("MINEBEAN_MINER_ADDRESS"):
        return "readonly"
    return None


def resolve_address(env: dict[str, str] | None = None) -> str | None:
    """Return the wallet address tied to the active signer, if any.

    EOA mode derives the address from MINEBEAN_DEPLOYER_KEY locally.
    Bankr and readonly modes return the value of MINEBEAN_MINER_ADDRESS.

    On any failure we log only the exception type, never its message, to
    avoid the chance that eth_account ever embeds the key in an error string.
    """
    env = env or os.environ
    mode = resolve_signer_mode(env)
    if mode == "eoa":
        try:
            from eth_account import Account
            key = env["MINEBEAN_DEPLOYER_KEY"]
            return Account.from_key(key).address
        except Exception as exc:
            # Log only the type. Never the message (could contain key bytes
            # if the underlying library is ever buggy).
            logger.debug(
                "Could not derive address from MINEBEAN_DEPLOYER_KEY: %s",
                type(exc).__name__,
            )
            return None
    if mode in ("bankr", "readonly"):
        addr = (env.get("MINEBEAN_MINER_ADDRESS") or "").strip()
        return addr or None
    return None


def can_broadcast(env: dict[str, str] | None = None) -> bool:
    """True if the active mode could broadcast a transaction. Readonly returns False."""
    mode = resolve_signer_mode(env)
    return mode in ("eoa", "bankr")


def is_configured() -> bool:
    """True if a signer mode resolves AND an address is derivable."""
    return resolve_signer_mode() is not None and resolve_address() is not None


def _truncate_address(addr: str) -> str:
    """Return 0xABCD…WXYZ form of a checksum address for audit logging."""
    if not addr or len(addr) < 10:
        return addr or "<unknown>"
    return f"{addr[:6]}…{addr[-4:]}"


def _hex_str(v: Any) -> Any:
    """Format a HexBytes/bytes value as 0x-prefixed hex. None passthrough.

    Returns None for zero-byte values (web3.py occasionally returns
    all-zero block_hash when the receipt is fetched before the block
    hash is finalised at the node; surfacing it would be misleading).
    """
    if v is None:
        return None
    raw = bytes(v) if hasattr(v, "hex") else v
    if isinstance(raw, bytes):
        if not any(raw):  # all zero bytes
            return None
        s = raw.hex()
    else:
        s = str(raw)
    if not s.startswith("0x"):
        s = "0x" + s
    return s


def _serialize_receipt(receipt: Any) -> dict[str, Any]:
    """Convert a web3 TxReceipt (AttributeDict + HexBytes) into JSON-safe dict.

    Only includes fields we surface back to the caller. Drops logs / bloom
    filter / etc. since the caller can re-query by tx_hash if they want them.
    """
    if receipt is None:
        return {}
    return {
        "tx_hash": _hex_str(receipt.get("transactionHash")),
        "block_number": int(receipt.get("blockNumber", 0)) if receipt.get("blockNumber") is not None else None,
        "block_hash": _hex_str(receipt.get("blockHash")),
        "from": receipt.get("from"),
        "to": receipt.get("to"),
        "status": int(receipt.get("status", 0)) if receipt.get("status") is not None else None,
        "gas_used": int(receipt.get("gasUsed", 0)) if receipt.get("gasUsed") is not None else None,
        "effective_gas_price": int(receipt.get("effectiveGasPrice", 0)) if receipt.get("effectiveGasPrice") is not None else None,
        "contract_address": receipt.get("contractAddress"),
    }


# ---------------------------------------------------------------------------
# Abstract signer + EOA implementation.
# ---------------------------------------------------------------------------
class Signer:
    """Abstract base. EOASigner is the only concrete subclass shipped in v0.2."""

    mode: str = "abstract"

    def address(self) -> str:
        raise NotImplementedError

    def submit_tx(self, tx: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:  # noqa: ARG002
        raise NotImplementedError


class EOASigner(Signer):
    """Local EOA signer.

    Holds a key on the instance for its lifetime. The instance is intended
    to be short-lived: construct, broadcast one tx, let it go out of scope.

    Key safety properties:
    - Address cached at construction; submit paths never touch from_key.
    - No log line ever interpolates the key value. Exception logs only
      include type(exc).__name__.
    - Receipt serialization strips HexBytes / AttributeDict before return.
    - Status check: a receipt with status != 1 is treated as a failed
      broadcast and reported clearly. Caller can use this to decide whether
      to increment the daily ceiling counter.
    """

    mode = "eoa"

    def __init__(self, key: str, w3: Any) -> None:
        from eth_account import Account
        # Derive address once. Cache; never re-derive on hot path.
        try:
            account = Account.from_key(key)
        except Exception as exc:
            # See resolve_address: only type, never message.
            raise RuntimeError(
                f"EOASigner construction failed: {type(exc).__name__}"
            ) from None
        self._key = key  # lifetime: this instance only
        self._address = account.address
        self._w3 = w3
        logger.info(
            "EOASigner ready for %s on chain %s",
            _truncate_address(self._address),
            getattr(w3.eth, "chain_id", "?"),
        )

    def address(self) -> str:
        return self._address

    def submit_tx(self, tx: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
        """Sign and broadcast an unsigned tx dict. Optionally wait for receipt.

        The tx dict must already include `nonce` and gas fee fields (web3.py
        populates these inside Contract.functions.x.build_transaction()).
        We do NOT auto-retry. On RPC failure the caller must rebuild the tx
        so the nonce is fresh; reusing a nonce risks double-broadcast or
        revert under MEV/reorg conditions.

        Returns a JSON-safe dict:
            {
              "ok": bool,            # status == 1
              "stage": str | None,   # populated when ok is False
              "tx_hash": "0x...",
              "receipt": { ... } | None,
            }
        """
        from eth_account import Account

        # 1. Sign locally. Strip `from` because eth_account derives it from the
        #    key and rejects the field in the tx dict. Fetch nonce fresh at
        #    submit time if missing (web3.py's build_transaction doesn't always
        #    auto-populate it). Never log signed bytes.
        tx_to_sign = {k: v for k, v in tx.items() if k != "from"}
        if "nonce" not in tx_to_sign:
            try:
                tx_to_sign["nonce"] = self._w3.eth.get_transaction_count(
                    self._address, "pending"
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "stage": "nonce_fetch_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "tx_hash": None,
                    "receipt": None,
                }
        try:
            signed = Account.sign_transaction(tx_to_sign, self._key)
        except Exception as exc:
            logger.debug("sign_transaction failed: %s", type(exc).__name__)
            return {
                "ok": False,
                "stage": "sign_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "tx_hash": None,
                "receipt": None,
            }

        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        if raw is None:
            return {
                "ok": False,
                "stage": "sign_failed",
                "error": "signed transaction missing raw bytes",
                "tx_hash": None,
                "receipt": None,
            }

        # 2. Broadcast.
        try:
            tx_hash = self._w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            logger.debug("send_raw_transaction failed: %s", type(exc).__name__)
            return {
                "ok": False,
                "stage": "broadcast_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "tx_hash": None,
                "receipt": None,
            }

        tx_hash_hex = _hex_str(tx_hash) or str(tx_hash)
        logger.info(
            "EOASigner broadcast %s from %s",
            tx_hash_hex,
            _truncate_address(self._address),
        )

        if not wait:
            return {
                "ok": True,
                "stage": "broadcast_no_wait",
                "tx_hash": tx_hash_hex,
                "receipt": None,
            }

        # 3. Wait for receipt and verify status == 1.
        try:
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception as exc:
            logger.debug("wait_for_transaction_receipt failed: %s", type(exc).__name__)
            return {
                "ok": False,
                "stage": "receipt_timeout",
                "error": f"{type(exc).__name__}: {exc}",
                "tx_hash": tx_hash_hex,
                "receipt": None,
            }

        serialized = _serialize_receipt(receipt)
        status = serialized.get("status")
        if status != 1:
            logger.warning(
                "tx %s reverted (status=%s) for %s",
                tx_hash_hex,
                status,
                _truncate_address(self._address),
            )
            return {
                "ok": False,
                "stage": "reverted",
                "error": f"tx reverted on-chain (status={status})",
                "tx_hash": tx_hash_hex,
                "receipt": serialized,
            }

        return {
            "ok": True,
            "stage": "confirmed",
            "tx_hash": tx_hash_hex,
            "receipt": serialized,
        }


# ---------------------------------------------------------------------------
# Factory + unlock gate.
# ---------------------------------------------------------------------------
def _live_broadcast_unlocked(env: dict[str, str] | None = None) -> bool:
    """True if the operator has explicitly flipped the unlock env var.

    Defaults to False. Set MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 once you have
    reviewed signer.py and are ready to send real transactions.
    """
    env = env or os.environ
    flag = (env.get("MINEBEAN_LIVE_BROADCAST_UNLOCKED") or "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def make_signer(*, env: dict[str, str] | None = None, w3: Any = None) -> Signer:
    """Factory. Returns an EOASigner once the unlock flag is set.

    Refuses readonly mode outright since it can never broadcast.
    Refuses bankr mode until a separate review completes.
    Refuses EOA mode unless MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 is set.
    """
    env = env or os.environ
    mode = resolve_signer_mode(env)
    if mode is None:
        raise RuntimeError(
            "No signer configured. Set MINEBEAN_DEPLOYER_KEY (eoa) or "
            "BANKR_API_KEY (bankr) and try again."
        )
    if mode == "readonly":
        raise RuntimeError(
            "Readonly mode cannot broadcast. Set MINEBEAN_DEPLOYER_KEY (eoa) "
            "or BANKR_API_KEY (bankr) for live signing."
        )
    if mode == "bankr":
        raise NotImplementedError(
            "Bankr signer not yet wired. Use eoa mode for v0.2."
        )
    if mode == "eoa":
        if not _live_broadcast_unlocked(env):
            raise NotImplementedError(
                "EOA live broadcast is gated. Set "
                "MINEBEAN_LIVE_BROADCAST_UNLOCKED=1 after the dev key-handling "
                "review completes. Use dry_run=True (default) in the meantime."
            )
        # Lazy import to keep Web3 out of the readonly path.
        if w3 is None:
            from . import rpc
            w3 = rpc.get_web3()
        key = env["MINEBEAN_DEPLOYER_KEY"]
        return EOASigner(key=key, w3=w3)
    raise RuntimeError(f"Unknown signer mode: {mode!r}")
