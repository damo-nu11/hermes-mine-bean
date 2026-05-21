# hermes-mine-bean

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin for mining $BEAN on the [MineBean protocol](https://minebean.com), live on Base mainnet.

Round-based on-chain deployment, five strategy presets, autonomous cron mode, signed Gitlawb audit log, Venice as default inference provider. Works inside Hermes Agent, Claude Desktop, Cursor, or any MCP-aware client.

> Status: v0.3.0 live on PyPI. All eight tools work against Base mainnet today. Live broadcast is opt-in behind a one-line env unlock; dry-run is the default everywhere.

## What MineBean is (60 seconds)

5x5 grid. New round every 60 seconds. Each round you pick blocks, deploy ETH into them, and earn $BEAN rewards plus an ETH share when your round closes. Roughly 1-in-777 rounds hit a beanpot jackpot. Contract addresses, agent stats, and the full game state are at [minebean.com](https://minebean.com).

This plugin gives any Hermes agent the eight tools needed to read the live game, plan deploys, broadcast them through a wallet you control, run a cron-driven autonomous miner with a hard daily ceiling, and inspect the active inference provider.

## Install

Inside Hermes Agent, install as a plugin (this is the canonical path):

```bash
hermes plugins install damo-nu11/hermes-mine-bean --enable
hermes gateway restart
```

Hermes clones the repo into `~/.hermes/plugins/minebean/` and registers the 8 tools, the `/minebean` slash command, and the lifecycle hooks. No pip step required.

For the headless cron miner (the `hermes-minebean-deploy` console script) or to use the plugin as a library:

```bash
pip install hermes-mine-bean
```

For MCP support (Claude Desktop, Cursor, or any other MCP client running the `hermes-minebean-mcp` server):

```bash
pip install "hermes-mine-bean[mcp]"
```

From source (for contributing):

```bash
git clone https://github.com/damo-nu11/hermes-mine-bean.git
cd hermes-mine-bean
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[mcp]"
```

Upstream skill registry PR is open at [NousResearch/hermes-agent#29850](https://github.com/NousResearch/hermes-agent/pull/29850); once merged, the skill becomes discoverable directly from the registry.

## Configure

Add to `~/.hermes/.env`:

```bash
# --- For broadcasting (autonomous mining) ---
# Use a dedicated wallet you fund only for this purpose. Never your main.
MINEBEAN_DEPLOYER_KEY=0x...

# --- Or for Bankr-managed signing ---
# BANKR_API_KEY=bk_ptr_...
# MINEBEAN_MINER_ADDRESS=0x...

# --- Or readonly inspection (no key needed) ---
# MINEBEAN_MINER_ADDRESS=0x...

# --- Safety guards (recommended) ---
MINEBEAN_MAX_DEPLOYS_PER_DAY=100
MINEBEAN_PER_BLOCK_WEI=2500000000000
MINEBEAN_MAX_DEPLOY_WEI=10000000000000000  # 0.01 ETH cap per round

# --- Strategy data feeds (defaults work for most users) ---
# MINEBEAN_BEAN_PRICE_ETH=                       # offline override, otherwise fetched from api.minebean.com
# MINEBEAN_PRICE_URL=                            # custom price endpoint
# MINEBEAN_BEANPOT_HISTORICAL_MAX_BEAN=700       # beanpot-hunter campaign cap

# --- Optional RPC override ---
# BASE_RPC_URL=https://mainnet.base.org
```

`chmod 600 ~/.hermes/.env` so other users on the machine can't read it.

**Security-sensitive env vars** (never commit, never share, never log):
- `MINEBEAN_DEPLOYER_KEY` — your wallet private key
- `MINEBEAN_LIVE_BROADCAST_UNLOCKED` — when set to `1`, enables live broadcast. Leave unset (the default) until you've personally reviewed `signer.py` and are ready to send real transactions. Treat this flag like the key itself: only set it on a machine you control, never in shared configs or CI.

## Tools

| Tool | Purpose | Signer required |
|---|---|---|
| `minebean_status` | Live round state, beanpot pool, caller's pending balances | No (works with address only) |
| `minebean_pending` | Pending ETH + BEAN for any address | No |
| `minebean_set_profile` | Save default strategy (`sniper`, `anti-winner`, `beanpot-hunter`, `anti-loser`, `nostradamus`) | No |
| `minebean_deploy` | Build deploy plan, optionally broadcast | Yes for broadcast, no for dry-run |
| `minebean_claim` | Claim pending winnings | Yes |
| `minebean_autostart` | Install autonomous mining cron job | Yes |
| `minebean_autostop` | Remove the cron job | No |

Slash command: `/minebean <subcommand>`. Try `/minebean status` first to confirm everything is wired correctly.

All write paths default to `dry_run=True` while the live broadcast gate is in place. You'll see the resolved plan, gas estimate, and ceiling status without sending a transaction.

## Strategy presets

All five strategies use the closed-form EV optimum `X* = sqrt(K × P × T) − T` where `P` is BEAN price in ETH, `T` is the relevant deployment total (strategy-specific), and `K = B / FEE_DRAG` is the strategy's EV constant.

| Preset | Blocks | T source | K | Behaviour at T ≥ threshold |
|---|---|---|---|---|
| `anti-winner` | 24 (excl. prev winner) | current grid total | 10.476 (B=1.1) | Deploy minimum for beanpot eligibility |
| `nostradamus` | All 25 | avg of last 3 settled rounds | 9.524 (B=1.0) | Skip |
| `anti-loser` | 24 (excl. coldest in last 100) | max(grid, avg of last 3) | 9.524 | Skip |
| `sniper` | All 25 | live grid at deploy time | 9.524 | Skip |
| `beanpot-hunter` | All 25 | current grid total | dynamic (B = 1 + beanpot/777) | Skip if beanpot < 62.16 BEAN |

`Beanpot-hunter` adds a campaign-sizing layer on top of the formula: deploy size scales linearly from 0.5% of wallet balance at the 62.16 BEAN threshold to the configured maximum at the historical max pot (default 700 BEAN). The EV formula acts as a floor.

`Sniper` adds adaptive binary-search timing (5s initial offset, 2-10s range, 0.5s step), applied at broadcast time. Timing supervision is left to the cron entry or an LLM supervisor running every 10 rounds.

Common floor: per-block deploy is grossed up by the 1% vault fee so the net amount clears the `MIN_DEPLOY` contract floor (0.0000025 ETH/block).

## First live broadcast

After your dev review of `signer.py` clears, walk through this once before enabling cron mode.

1. **Fund the wallet** — send ~0.005-0.01 ETH on Base to the address `MINEBEAN_DEPLOYER_KEY` resolves to. Enough for one or two anti-winner deploys plus gas.

2. **Set safety caps** in `~/.hermes/.env`:
   ```
   MINEBEAN_LIVE_BROADCAST_UNLOCKED=1
   MINEBEAN_MAX_DEPLOYS_PER_DAY=1
   MINEBEAN_MAX_DEPLOY_WEI=200000000000000   # 0.0002 ETH cap, raise after first success
   ```

3. **Load env into your shell** (whitelist the MineBean vars; skips any stray non-env-style lines):
   ```bash
   while IFS= read -r line; do
     case "$line" in
       MINEBEAN_*=*|BASE_RPC_URL=*) export "$line" ;;
     esac
   done < ~/.hermes/.env
   ```

4. **Run the broadcast interactively**:
   ```bash
   hermes-minebean-deploy --profile anti-winner --no-dry-run
   ```

5. **Verify** the JSON output:
   - `broadcast.ok: true`
   - `broadcast.stage: "confirmed"`
   - `broadcast.receipt.status: 1`
   - `broadcast.tx_hash: "0x..."` (paste into `https://basescan.org/tx/{hash}` to inspect)

6. **Raise `MINEBEAN_MAX_DEPLOYS_PER_DAY`** to your desired cap, then move to cron mode (next section).

If `broadcast.ok` is false, the `stage` field tells you exactly what failed: `sign_failed`, `broadcast_failed`, `receipt_timeout`, `reverted`, `ceiling_reached`, or `live_broadcast_blocked`. The daily counter only increments on `stage="confirmed"`, so a failed run doesn't burn quota.

## Autonomous mode

Two ways to start a scheduled miner.

**Via slash command:**

```
/minebean autostart every 60s
```

**Via Hermes cron directly:**

```bash
hermes cron add --name "MineBean deploy" --no-agent \
  --script ~/.hermes/scripts/minebean-deploy.sh "every 60s"
```

The cron entry runs `hermes-minebean-deploy`, which:

- Enforces `MINEBEAN_MAX_DEPLOYS_PER_DAY` (atomic counter under `~/.hermes/.minebean/`)
- Exits `0` on a normal skip or ceiling hit (no error spam)
- Exits `1` on a real failure
- Exits `130` on SIGINT
- Emits one JSON line per run for log scraping

Stop with `/minebean autostop` or `hermes cron remove <name>`.

## MCP server

The `[mcp]` extra ships a `hermes-minebean-mcp` console script that speaks JSON-RPC over stdio.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "minebean": {
      "command": "hermes-minebean-mcp",
      "env": {
        "MINEBEAN_MINER_ADDRESS": "0xYourAddress",
        "MINEBEAN_MAX_DEPLOYS_PER_DAY": "100"
      }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json` in your project):

```json
{
  "mcpServers": {
    "minebean": {
      "command": "hermes-minebean-mcp"
    }
  }
}
```

Restart the client, then ask: *"What's the current MineBean round?"*

The MCP server registers all 7 tools with full schemas. Deploy and claim default to dry-run from the MCP surface too.

## Inference provider

The plugin defaults `HERMES_INFERENCE_PROVIDER=venice` when nothing is set, so any Hermes agent running this plugin routes its LLM calls through [Venice](https://venice.ai/) by default. Venice's `HERMES_VENICE_NO_LOG=1` flag keeps your conversation off third-party logs while the mining loop stays fully on-chain and public.

Add to `~/.hermes/.env` to enable Venice:

```bash
HERMES_VENICE_API_KEY=...
HERMES_VENICE_NO_LOG=1
```

**Multi-provider hook.** Already running Hermes with a different provider? The plugin respects whatever you pin. Set `HERMES_INFERENCE_PROVIDER` to any of `venice`, `openai`, `anthropic`, `openrouter`, `ollama`, or `lmstudio` and the bootstrap leaves it alone.

Inspect the active provider any time via:

```
/minebean inference_status
```

or call the `minebean_inference_status` tool directly.

## Zero-install via .well-known

You can also load this skill without `pip install` by pointing a Hermes-compatible client at:

```
https://agent.minebean.com/.well-known/skills/index.json
```

The discovery endpoint returns a SKILL.md and a minimal stdlib readonly client (`scripts/minebean_client.py`). Useful for prototyping or for clients that prefer file-based skill loading.

## Verifiability

Every round is mirrored to a signed Gitlawb repository:

```
gitlawb://did:key:z6MkwVfgaAnuypajisEkJLkVbWPiPEBwceMkGutfXpEEYHKi/minebean-rounds
```

Append-only, signed, and replicated across the Gitlawb network, with new round files landing every 5 minutes. Pull the window file for any time range to audit any outcome without trusting minebean.com.

## Safety

- `dry_run=True` is the hard default on every write path. Live broadcast requires `MINEBEAN_LIVE_BROADCAST_UNLOCKED=1`.
- `make_signer()` raises in every non-readonly branch (the broadcast path is wired but disabled at the source)
- `MINEBEAN_MAX_DEPLOYS_PER_DAY` blocks both the cron entry and the interactive deploy handler. The counter is `fcntl`-locked so overlapping crons cannot under-count.
- The GridMining contract enforces one deploy per round per address. The plugin mirrors this client-side.
- Cron wrapper script is `chmod 700` (user-only rwx) because it sources `~/.hermes/.env`.
- Readonly mode (just `MINEBEAN_MINER_ADDRESS`, no key) lets you inspect any address without putting a key anywhere.

## Network

| Field | Value |
|---|---|
| Chain | Base mainnet (chain ID 8453) |
| GridMining | `0x9632495bDb93FD6B0740Ab69cc6c71C9c01da4f0` |
| BEAN token | `0x5c72992b83E74c4D5200A8E8920fB946214a5A5D` |
| Default RPC | `https://mainnet.base.org` |

ABIs ship in `src/hermes_minebean/abi/`. The GridMining ABI is extracted from the deployed contract via BaseScan.

## Repo layout

```
hermes-mine-bean/
├── pyproject.toml             package config + entry points
├── plugin.yaml                Hermes Agent plugin manifest
├── src/hermes_minebean/
│   ├── plugin_entry.py        Hermes register(ctx)
│   ├── mcp_server.py          FastMCP server for Claude/Cursor
│   ├── cli.py                 hermes-minebean-deploy console script
│   ├── tools.py               7 tool handlers
│   ├── schemas.py             OpenAI-style schemas + presets
│   ├── strategies.py          5 strategy resolvers
│   ├── signer.py              eoa | bankr | readonly signer abstraction
│   ├── contract.py            GridMining read + tx-builder client
│   ├── rpc.py                 web3 client with Cloudflare-safe UA
│   ├── state.py               profile + daily counter (fcntl-locked)
│   ├── cron_jobs.py           autostart / autostop wrapper management
│   ├── hooks.py               pre_llm_call keyword gate
│   ├── slash.py               /minebean dispatcher
│   └── abi/gridmining.json
└── coordinator-deliverables/  zero-install skill artifacts (served via agent.minebean.com)
```

## Development

```bash
# Activate the venv
source .venv/bin/activate

# Run the readonly smoke test against mainnet
MINEBEAN_MINER_ADDRESS=0xYourTestAddress \
  python -c "from hermes_minebean.tools import _handler_status; print(_handler_status())"

# Run the MCP server and probe with a JSON-RPC client
hermes-minebean-mcp
```

The plugin is unix-only by design (Hermes Agent itself is unix-only). The daily-counter file lock uses `fcntl`.

## Contributing

Bug reports, strategy formula proposals, and PRs welcome at the [issues page](https://github.com/damo-nu11/hermes-mine-bean/issues).

Before opening a PR:
- Keep tool signatures stable (third parties consume them)
- Note any user-visible behaviour change in the PR description
- Live broadcast changes need a separate review pass

## Acknowledgements

- [Nous Research](https://nousresearch.com/) for Hermes Agent and the plugin spec
- The [MineBean](https://minebean.com) protocol team for the on-chain game
- Anthropic for the [MCP](https://modelcontextprotocol.io) protocol that makes this work in Claude Desktop and Cursor

## License

MIT. See `LICENSE` for the full text.
