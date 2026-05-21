"""Persistent state for hermes-mine-bean.

Two storage surfaces live under `$HERMES_HOME/.minebean/`:

1. `profile` - the user's saved default strategy preset (single line).
2. `deploys-YYYY-MM-DD.count` - the daily deploy counter, used by the
   cron mode and the minebean_deploy tool to enforce
   MINEBEAN_MAX_DEPLOYS_PER_DAY.

HERMES_HOME defaults to `~/.hermes` if not set. Counter file naming uses
UTC so cron schedules stay consistent across timezone boundaries.
"""
from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path

from .schemas import STRATEGY_PRESETS


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def hermes_home() -> Path:
    """Return the Hermes home directory. HERMES_HOME env var overrides."""
    custom = (os.environ.get("HERMES_HOME") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return Path.home() / ".hermes"


def state_dir() -> Path:
    """Directory where minebean state files live. Created on demand."""
    d = hermes_home() / ".minebean"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_path() -> Path:
    return state_dir() / "profile"


def counter_path(date_utc: str | None = None) -> Path:
    """Path to the deploy counter for a given UTC date (defaults to today)."""
    if date_utc is None:
        date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state_dir() / f"deploys-{date_utc}.count"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def get_profile() -> str | None:
    """Return the saved default profile, or None if not set or invalid."""
    path = profile_path()
    if not path.exists():
        return None
    try:
        value = path.read_text().strip().lower()
        return value if value in STRATEGY_PRESETS else None
    except OSError:
        return None


def set_profile(profile: str) -> str:
    """Save the default strategy preset. Returns the normalised value.

    Raises ValueError if the profile isn't one of the known presets.
    """
    p = (profile or "").strip().lower()
    if p not in STRATEGY_PRESETS:
        raise ValueError(
            f"Unknown profile {profile!r}. Expected one of: {', '.join(STRATEGY_PRESETS)}"
        )
    profile_path().write_text(p + "\n")
    return p


def clear_profile() -> bool:
    """Remove the saved profile if it exists. Returns True if removed."""
    try:
        profile_path().unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Daily deploy counter
# ---------------------------------------------------------------------------
def get_daily_ceiling() -> int:
    """Return the daily deploy ceiling from env, defaulting to 100."""
    raw = (os.environ.get("MINEBEAN_MAX_DEPLOYS_PER_DAY") or "").strip()
    try:
        v = int(raw) if raw else 100
    except ValueError:
        v = 100
    return max(1, v)


def read_today_count() -> int:
    """Return today's deploy count from disk. Zero if no file exists."""
    path = counter_path()
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def increment_today_count() -> int:
    """Increment today's counter and return the new value.

    Read-modify-write is guarded by an exclusive flock on a sibling lock file
    so overlapping crons or a manual deploy fired alongside an autostart job
    cannot under-count.
    """
    path = counter_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    # Touch the lock file before opening for exclusive flock.
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            new_count = read_today_count() + 1
            path.write_text(str(new_count) + "\n")
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    return new_count


def reset_today_count() -> bool:
    """Wipe today's counter. Returns True if a file was removed."""
    try:
        counter_path().unlink()
        return True
    except FileNotFoundError:
        return False


def remaining_today() -> int:
    """Deploys still allowed today before hitting the ceiling."""
    return max(0, get_daily_ceiling() - read_today_count())


def ceiling_status() -> dict[str, int | str]:
    """Snapshot of today's ceiling state. Used by tools and the cron entry."""
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "date_utc": today_utc,
        "today_count": read_today_count(),
        "daily_ceiling": get_daily_ceiling(),
        "remaining_today": remaining_today(),
    }
