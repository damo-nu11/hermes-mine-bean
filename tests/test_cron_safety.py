"""Cron-safety invariants: overlap lock and configurable receipt-wait.

These lock the two root-cause fixes for the cron timeout/collision so a future
change cannot reintroduce the 120s hard wait or drop the overlap guard.
"""
from __future__ import annotations

import fcntl

import pytest

from hermes_minebean import cli
from hermes_minebean.signer import DEFAULT_RECEIPT_TIMEOUT, _resolve_receipt_timeout


def _release(fh) -> None:
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    fh.close()


# ---------------------------------------------------------------------------
# Overlap lock
# ---------------------------------------------------------------------------
def test_second_acquire_is_blocked_while_first_held(monkeypatch):
    monkeypatch.setenv("MINEBEAN_MINER_ADDRESS", "0x" + "ab" * 20)
    first = cli._acquire_run_lock()
    try:
        assert first is not None
        assert cli._acquire_run_lock() is None  # overlap blocked
    finally:
        _release(first)


def test_lock_reacquires_after_release(monkeypatch):
    monkeypatch.setenv("MINEBEAN_MINER_ADDRESS", "0x" + "cd" * 20)
    first = cli._acquire_run_lock()
    assert first is not None
    _release(first)
    second = cli._acquire_run_lock()
    assert second is not None
    _release(second)


def test_distinct_signers_do_not_contend(monkeypatch):
    monkeypatch.setenv("MINEBEAN_MINER_ADDRESS", "0x" + "11" * 20)
    a = cli._acquire_run_lock()
    monkeypatch.setenv("MINEBEAN_MINER_ADDRESS", "0x" + "22" * 20)
    b = cli._acquire_run_lock()
    try:
        assert a is not None and b is not None
    finally:
        _release(a)
        _release(b)


def test_lock_filename_does_not_leak_the_key(monkeypatch):
    monkeypatch.delenv("MINEBEAN_MINER_ADDRESS", raising=False)
    secret = "0x" + "de" * 32
    monkeypatch.setenv("MINEBEAN_DEPLOYER_KEY", secret)
    fh = cli._acquire_run_lock()
    try:
        assert fh is not None
        assert secret[2:] not in fh.name  # raw key never appears in the path
    finally:
        _release(fh)


# ---------------------------------------------------------------------------
# Configurable receipt-wait timeout
# ---------------------------------------------------------------------------
def test_receipt_timeout_defaults_to_120():
    assert _resolve_receipt_timeout({}) == DEFAULT_RECEIPT_TIMEOUT == 120.0


def test_receipt_timeout_honours_override():
    assert _resolve_receipt_timeout({"MINEBEAN_RECEIPT_TIMEOUT": "20"}) == 20.0


def test_receipt_timeout_accepts_float():
    assert _resolve_receipt_timeout({"MINEBEAN_RECEIPT_TIMEOUT": "12.5"}) == 12.5


@pytest.mark.parametrize("bad", ["", "abc", "1,2"])
def test_receipt_timeout_falls_back_on_garbage(bad):
    assert _resolve_receipt_timeout({"MINEBEAN_RECEIPT_TIMEOUT": bad}) == 120.0
