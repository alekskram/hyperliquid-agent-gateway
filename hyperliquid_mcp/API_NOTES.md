# API notes - Hyperliquid public surfaces (as verified 2026-09-05/06)

Facts the gateway depends on. Every claim below was checked against the
live endpoints before v0.1.0 was coded; fixtures in `tests/fixtures/`
mirror these shapes.

## /info (POST https://api.hyperliquid.xyz/info)

- Single POST endpoint; body is `{"type": "<Type>", ...}`. Keyless,
  read-only. Action types (signing) are NOT used by this gateway.
- **ALL numerics are STRINGS** (`"42350.1"`, `"-0.00012"`). Parse with
  a never-raising `_f()`; empty/absent optionals become `None`.
- Weight-based rate limit: **1200 weight / 60s**. Per-request weights
  implemented in `hyperliquid_mcp/info.py`:

  | type | weight |
  |------|--------|
  | allMids | 2 |
  | l2Book | 2 |
  | meta, metaAndAssetCtxs, spotMeta, spotMetaAndAssetCtxs | 20 |
  | recentTrades, clearinghouseState, userFills, userFunding, spotClearinghouseState | 20 |
  | fundingHistory | 20 base + extra per 20 items beyond the first |
  | candleSnapshot | 60 |

- `metaAndAssetCtxs` returns a **pair** `[meta, assetCtxs]`: meta =
  `{"universe": [{"name", "szDecimals", "maxLeverage",
  "onlyIsolated"}]}`; assetCtxs rows align by index with universe and
  carry `dayNtlVlm`, `openInterest` (coin units), `premium`, `markPx`,
  `midPx`, `oraclePx`, `impactPxs {bidPx, askPx}`, `funding`
  (hourly). ~233 perps at capture time.
- `spotMeta` = `{"universe": [{"name": "@1/PURR", "tokens": [int
  indices], "isCanonical", "index"}], "tokens": [{"name", "index",
  "token"}], "deployAuctionStatus", "registeredContracts": [{"name",
  "builder"}]}`. `token` is the ERC-20 contract (0x...) for HIP-1
  deployed coins, otherwise the coin name. ~326 pairs.
- `spotMetaAndAssetCtxs` likewise returns `[spotMeta, spotCtxs]`;
  spotCtxs rows carry `coin`, `markPx`, `midPx`, `dayNtlVlm`,
  `prevDayPx`, `circulating`, `funding`.
- `l2Book` body `{"coin"}` -> `{"coin", "time", "levels": {"bids":
  [{"px","sz","nSigFigs"}], "asks": [...]}}` - already aggregated to
  significant figures by the venue.
- `candleSnapshot` body `{"coin", "interval", "startTime"}`; intervals
  `1m,15m,1h,4h,1d,1w,1M`; rows oldest-first with `t/T` (ms), `o/c/h/l/v`
  (STRINGS), `n` (int trade count).
- `recentTrades` body `{"coin"}` -> rows with `side` ('B'/'A'), `px`,
  `sz`, `time` (ms), `hash`, **`users: [maker, taker]`** (both sides).
- `fundingHistory` body `{"coin", "startTime?", "endTime?"}` -> flat
  `[{coin, fundingRate, premium, time}]`, hourly, newest-last.
- `clearinghouseState` body `{"user": address}` ->
  `marginSummary {accountValue, totalNtlPos, totalRawUsd,
  totalMarginUsed, withdrawable}`, `crossMaintenanceMarginUsed`,
  `assetPositions [{type, position {coin, szi, leverage {type, value},
  entryPx, positionValue, unrealizedPnl, returnOnEquity,
  liquidationPx | null, marginUsed, maintMarginUsed, isolatedMargin}}]`.
  **`liquidationPx` can be null** - the estimate path in
  `liquidation_risk` exists for exactly that case.
- `userFills` -> `[{coin, dir, px, sz, time, closedPnl, fee, feeToken,
  builderFee, hash, oid}]`.
- `userFunding` -> non-zero funding payments `[{coin, fundingRate,
  premium, time, delta}]`; `delta` is the USD payment, negative when
  the trader pays.
- `spotClearinghouseState` -> `{"balances": [{"coin": "@/PURR",
  "hold", "total"}]}`.

## HyperEVM RPC (POST https://rpc.hyperliquid.xyz/evm)

- Chain id **999** (0x3e7). Keyless public node, separate from /info
  budgets: this gateway enforces its own **100 requests/min** cap
  locally.
- `eth_getLogs` responses are capped (~1MB); active tokens need
  roughly **30-60 block windows**. `evm.get_logs` adapts: start 30
  blocks, halve on failure/too-big (floor 2), double on quiet success
  (cap 60), max 14 slices.
- Transfer topic
  `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`;
  `balanceOf(address)` selector `0x70a08231`; native balance via
  `eth_getBalance`.
- Env override: `HL_EVM_RPC_URL`.

## Endpoints NOT used, and why

- **info type `leaderboard`** - returns 403 behind Cloudflare bot
  protection for non-browser clients; unreliable for a gateway.
- **Blockscout-style explorers** (`explorer.hyperliquid.xyz` API) - the
  free tier was dead/unstable at verification time; all on-chain reads
  go through the raw RPC instead.
- **WebSocket subscriptions** (`wss://api.hyperliquid.xyz/ws`) -
  deferred to v0.2; the MCP request/response model gains little from
  push feeds and the TTL caches already cover freshness.
- **Exchange/exchangeAction (signing) endpoints** - out of scope by
  design: this gateway is strictly read-only and keyless; no private
  keys ever enter the process.
