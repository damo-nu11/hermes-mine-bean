"""MCP server for hermes-mine-bean.

Exposes the same 7 tools to Claude Desktop, Cursor, and any MCP-compatible
client. Re-uses the HANDLERS dict from the main package so behaviour is
identical between Hermes plugin mode and MCP mode.

Install:
    pip install hermes-mine-bean[mcp]

Configure Claude Desktop / Cursor:

    {
      "mcpServers": {
        "minebean": {
          "command": "hermes-minebean-mcp",
          "env": {
            "MINEBEAN_DEPLOYER_KEY": "0x...",
            "BASE_RPC_URL": "https://mainnet.base.org"
          }
        }
      }
    }

Or for dry-run / readonly testing:

    {
      "mcpServers": {
        "minebean": {
          "command": "hermes-minebean-mcp",
          "env": {
            "MINEBEAN_MINER_ADDRESS": "0x..."
          }
        }
      }
    }

Live broadcast is gated behind MINEBEAN_LIVE_BROADCAST_UNLOCKED. dry_run
defaults to true on every deploy/claim call regardless of how the tool is
invoked.
"""
from __future__ import annotations

import json
from typing import Any

from .tools import HANDLERS


def _call(name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a HANDLERS function and parse its JSON return."""
    if name not in HANDLERS:
        return {"ok": False, "stage": "unknown_tool", "tool": name}
    raw = HANDLERS[name](**kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "stage": "parse_error", "tool": name, "raw": raw}


def main() -> None:
    """Start the FastMCP server over stdio.

    Imports FastMCP lazily so the rest of the package doesn't require the
    `mcp` dependency when users only need the Hermes plugin path.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit(
            "mcp package not installed. Install with: "
            "pip install hermes-mine-bean[mcp]"
        ) from exc

    app = FastMCP("minebean")

    @app.tool()
    def minebean_status() -> dict[str, Any]:
        """Read live MineBean state: current round id, time remaining, total deployed,
        beanpot pool, and the caller's pending balances if a signer is configured."""
        return _call("minebean_status")

    @app.tool()
    def minebean_pending(address: str | None = None) -> dict[str, Any]:
        """Read pending winnings (ETH + BEAN) for an address.

        Args:
            address: Optional 0x address to query. Defaults to the configured signer.
        """
        return _call("minebean_pending", address=address)

    @app.tool()
    def minebean_set_profile(profile: str) -> dict[str, Any]:
        """Save the default strategy preset to disk for future deploys.

        Args:
            profile: One of: sniper, anti-winner, beanpot-hunter, anti-loser, nostradamus.
        """
        return _call("minebean_set_profile", profile=profile)

    @app.tool()
    def minebean_deploy(
        profile: str | None = None,
        blocks: list[int] | None = None,
        per_block_wei: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Build a deploy plan for the current MineBean round.

        Args:
            profile: Strategy preset. Defaults to saved profile if omitted.
            blocks: Optional explicit block indices (0-24). Overrides strategy.
            per_block_wei: Optional wei amount per block as a string.
            dry_run: If true (default), returns the plan without broadcasting.
                Live broadcast is gated behind MINEBEAN_LIVE_BROADCAST_UNLOCKED.
        """
        return _call(
            "minebean_deploy",
            profile=profile,
            blocks=blocks,
            per_block_wei=per_block_wei,
            dry_run=dry_run,
        )

    @app.tool()
    def minebean_claim(dry_run: bool = True) -> dict[str, Any]:
        """Claim pending ETH + BEAN for the configured signer.

        Args:
            dry_run: If true (default), describes what would be claimed without broadcasting.
        """
        return _call("minebean_claim", dry_run=dry_run)

    @app.tool()
    def minebean_autostart(
        schedule: str = "every 60s",
        daily_cap: int | None = None,
    ) -> dict[str, Any]:
        """Install the autonomous mining cron job in Hermes.

        Args:
            schedule: Hermes cron schedule string (e.g. 'every 60s', 'every 5m').
            daily_cap: Optional override for MINEBEAN_MAX_DEPLOYS_PER_DAY.
        """
        return _call("minebean_autostart", schedule=schedule, daily_cap=daily_cap)

    @app.tool()
    def minebean_autostop() -> dict[str, Any]:
        """Return instructions to remove the autonomous mining cron job."""
        return _call("minebean_autostop")

    # FastMCP defaults to stdio transport, which is what Claude Desktop / Cursor expect.
    app.run()


if __name__ == "__main__":
    main()
