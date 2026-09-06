# hyperliquid-agent-gateway

An MCP (Model Context Protocol) server that gives AI agents read-only,
keyless access to **Hyperliquid** public data - the ~233-perp DEX
market, ~326 spot pairs, funding, per-account risk and HyperEVM (chain
999) token transfers. No API keys, no auth, no signing, no writes:
every tool reads public endpoints only (`api.hyperliquid.xyz/info` and
`rpc.hyperliquid.xyz/evm`), cached and rate-limited so an enthusiastic
agent cannot hammer the upstream.

## Use cases

- **Watch a wallet's risk** — per-account margin summary, leverage, liquidation distance on any address (`account risk` view)
- **Fund the carry, not the noise** — funding history + carry screener across 233 perps to find stable paid positions
- **Trace HyperEVM flows** — token transfers on chain 999 tied back to the perp markets (`token_transfers`)
- **Read the book before you enter** — order book + recent trades + all-mids in one pass
- **Trader scouting** — activity of any address: positions, volume, what they actually trade

Full walkthroughs: [examples/use-cases.md](examples/use-cases.md).

## Quickstart

stdio (default, for local agents):

```bash
uvx hyperliquid-agent-gateway
```

or from a checkout:

```bash
git clone https://github.com/alekskram/hyperliquid-agent-gateway
cd hyperliquid-agent-gateway
uv sync
uv run hyperliquid-agent-gateway
```

Claude Desktop / Cursor config:

```json
{
  "mcpServers": {
    "hyperliquid": {
      "command": "uvx",
      "args": ["--from",
               "git+https://github.com/alekskram/hyperliquid-agent-gateway",
               "hyperliquid-agent-gateway"]
    }
  }
}
```

Hosted form - streamable HTTP on port **8903**:

```bash
uv run hyperliquid-agent-gateway --http            # 127.0.0.1:8903
curl http://127.0.0.1:8903/health   # -> {"ok": true, "service": "hyperliquid-agent-gateway", ...}
```

<details>
<summary><b>Codex</b> (~/.codex/config.toml)</summary>

```toml
[mcp_servers.hyperliquid]
command = "uvx"
args = ["hyperliquid-agent-gateway"]
```
</details>

<details>
<summary><b>ZCode</b> — register the server (copy-paste)</summary>

```bash
# 1) start the gateway (keep it running)
uvx hyperliquid-agent-gateway --http --port 8903 &

# 2) register it (merges into ~/.zcode/cli/config.json)
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.zcode/cli/config.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg.setdefault("mcp", {}).setdefault("servers", {})["hyperliquid"] = {
    "type": "http", "url": "http://127.0.0.1:8903/mcp"}
json.dump(cfg, open(p, "w"), indent=2)
print("hyperliquid-agent-gateway registered:", p)
PY
```
</details>

Hosted form — streamable HTTP on port **8903**:

```bash
uvx hyperliquid-agent-gateway --http
```

## Tools

All 12 tools are read-only (annotated `readOnlyHint: true,
destructiveHint: false, openWorldHint: true`).

| # | Tool | Signature | What it does |
|---|------|-----------|--------------|
| 1 | `market_overview` | `market_overview(limit=20, sort="open_interest")` | Perp market snapshot from ONE `metaAndAssetCtxs` call: per-coin mark, open interest, day volume, premium, max leverage + totals. `sort` in {open_interest, volume, premium}. |
| 2 | `spot_overview` | `spot_overview(limit=20)` | Spot pairs from `spotMeta` + ctxs with HIP-1 to ERC-20 links; `@{index}` names resolved to readable token names. |
| 3 | `quote` | `quote(coin)` | Bid/ask/mid/spread + top-of-book sizes from `allMids` + `l2Book`. Unknown coin raises with 5 examples. |
| 4 | `order_book` | `order_book(coin, depth=10)` | Book levels per side with nSigFigs aggregation and per-side total liquidity. |
| 5 | `candles` | `candles(coin, interval="1h", limit=100)` | OHLCV rows newest-first; intervals 1m/15m/1h/4h/1d/1w/1M; `startTime` computed from `limit`. |
| 6 | `trades` | `trades(coin, limit=20)` | Recent public fills WITH both sides' addresses (`users: [maker, taker]`). |
| 7 | `funding_history` | `funding_history(coin, limit=100)` | Hourly funding rows + `premium_now` from the live asset ctx. |
| 8 | `liquidation_risk` | `liquidation_risk(address)` | Per-account risk: margin summary, cross maintenance margin, per-position leverage + `liquidationPx` when published; when null, an explicitly flagged ESTIMATED distance from the maintenance-margin ratio. Mark px is resolved per coin from `metaAndAssetCtxs` (fallback `allMids`) because live positions carry no `markPx` - see `mark_px_source` on each row. Includes funding drag. |
| 9 | `trader_activity` | `trader_activity(address, limit=50)` | Fills PnL/fees/volume/win-rate, funding net, open positions, per-coin breakdown. |
| 10 | `funding_carry_screener` | `funding_carry_screener(topN=10, metric="premium")` | Ranks ALL perps from ONE call; `fundingHistory` fetched only for the topN (weight economy). |
| 11 | `token_transfers` | `token_transfers(contract, limit=100, from_block=None)` | HyperEVM ERC-20 Transfer logs via adaptive-window `eth_getLogs`; rows carry from/to/value/txHash/blockNumber/ts with per-token `decimals` + `decimals_source` (static map or `assumed_18`). |
| 12 | `wallet_balance` | `wallet_balance(address)` | Native (eth_getBalance) + up to 20 ERC-20s (eth_call balanceOf, resolved from spotMeta) + Hyperliquid spot balances; every row carries `decimals`/`decimals_source`. |

## Rate limits

Two independent, locally enforced budgets protect the upstream:

**`/info` - 1200 weight per rolling 60s** (Hyperliquid's documented
weight pricing), tracked per request type:

| type | weight |
|------|--------|
| `allMids` | 2 |
| `l2Book` | 2 |
| `meta`, `metaAndAssetCtxs`, `spotMeta`, `spotMetaAndAssetCtxs` | 20 |
| `recentTrades`, `clearinghouseState`, `userFills`, `userFunding`, `spotClearinghouseState` | 20 |
| `fundingHistory` | 20 base + extra per 20 items beyond the first |
| `candleSnapshot` | 60 |

When the next request would exceed the budget the client waits once
(<=5s) for the window to roll, then raises a clear error naming the
limit - it never sleep-blocks forever.

**HyperEVM RPC - 100 requests per rolling 60s** (flat 1 per request),
enforced separately from /info. Over-budget calls raise immediately
(`rpc-limit`) - tools surface an honest error dict, and
`wallet_balance` stops its ERC-20 scan at the cap.

TTL caches additionally dedupe repeated calls per data type: allMids
15s, recentTrades 15s, l2Book 5s, metaAndAssetCtxs 60s, spotMeta 3600s,
spotMetaAndAssetCtxs 60s, candleSnapshot 300s, fundingHistory 300s,
per-address account types 60s.

## Honesty and degradation

- Every numeric from the API is a STRING upstream; the gateway parses
  them with a never-raising helper - `null` always means "not
  available", never zero.
- Every upstream failure returns an error dict
  `{"error": ..., "source": ..., "reason": ...}`, never a traceback;
  partial data degrades field-by-field with `warnings[]`.
- `liquidation_risk` never invents a liquidation price: when the venue
  publishes none, `liq_px` stays `null` and the distance is an
  explicitly flagged estimate (formula in the tool's note). Mark px is
  likewise never invented: live positions carry no `markPx`, so it is
  resolved from `metaAndAssetCtxs` (fallback `allMids`) and the row's
  `mark_px_source` says which; no source -> `null`.
- `funding_drag` / `funding_net`: the venue's `userFunding` returns
  only NON-ZERO funding events, so a live `null`/empty for a fresh or
  quiet address is expected behaviour, not a bug.
- ERC-20 amounts use a static decimals map for canonical HyperEVM
  tokens (6 for USDC/USDT-style, 18 for PURR/HYPE); unknown tokens
  assume 18 and every row says `decimals_source: "assumed_18"` - do
  not trust 6dp precision for unmapped tokens.
- Cached responses carry `age_seconds` / `fetched_at` freshness fields.


## License

MIT.
