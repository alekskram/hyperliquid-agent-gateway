# Real Trading Problems Solved

Four scenarios a trader or risk-watcher faces daily — solved with single MCP
calls. All outputs below are real captures (2026-09-22), lightly shortened.

## 1. "Where is the funding carry paying?"

**Pain:** Funding is paid hourly, but the obvious coins pay nothing. You want
the ranked board, without burning the API weight budget.

```
→ funding_carry_screener(topN=5, metric="premium")

  PURR  premium 0.2216%  funding +37.0%/yr (168h avg)   OI $115.2M
  BSV   premium 0.1993%  funding +45.3%/yr (168h avg)   OI $1.7M
  GMX   premium 0.1795%  funding +23.9%/yr (168h avg)   OI $1.0M

  ranked 234 perps from ONE metaAndAssetCtxs call;
  fundingHistory fetched only for the returned top-N
```

**Why it matters:** the screener ranks all 234 perps by live premium and only
then fetches 168h funding history for the top-N — you see whether the current
rate is a spike or a sustained regime, in one call, without hammering the
1200 weight/min budget.

## 2. "How close is this whale to liquidation?"

**Pain:** A 25x whale is underwater and you want the distance to the cascade —
not a screenshot, the numbers.

```
→ liquidation_risk(address="0xfc27…9d9d")

  account value      $21.65M   notional $209.7M
  BTC  SHORT -1117.8 @25x  entry 80652  mark 86379  ROE -177.5%  liq 101621 (-17.6% away)
  ETH  SHORT -28922  @25x  entry 2605   mark 2758   ROE -147.2%  liq 3343   (-21.2% away)
  SOL  SHORT -11935  @20x  ...           ROE -28.1%             liq 1528   (-1192% away)

  liq_px = venue's liquidationPx (estimated: false) — not a guess;
  mark resolved per coin from metaAndAssetCtxs (mark_px_source on every row)
```

**Why it matters:** when the venue publishes `liquidationPx` you get the real
distance (flagged `estimated: false`); when it doesn't, you get an explicitly
flagged estimate from the maintenance-margin ratio. Every mark price carries
its source — you always know which numbers are the exchange's and which are
derived.

## 3. "What does the whole board look like right now?"

**Pain:** You need the OI/volume/premium layout of all 233 perps before
deciding where to look closer.

```
→ market_overview(limit=5, sort="open_interest")

  BTC   mark 86374   OI $4.013B   24h vol $2.896B   prem +0.034%/h   maxlev 40
  ETH   mark 2758    OI $3.183B   24h vol $1.135B   prem +0.047%/h   maxlev 25
  HYPE  mark 96.05   OI $2.096B   24h vol $0.546B   prem -0.018%/h   maxlev 10
  ─ totals: 234 perps, OI $14.04B, 24h volume $7.18B
```

**Why it matters:** one call, the whole board with OI normalized to USD
(`open_interest_usd = openInterest × markPx` — the raw API gives you coin
units), totals included.

## 4. "Who is this address, actually?"

**Pain:** Before you trust a wallet's "track record" you want their real
fills, fees, win-rate and funding drag.

```
→ trader_activity(address="0xf3f4…744")

  fills PnL, fees (exchange vs builder), volume, win_rate_pct,
  net funding paid/received, open positions, per-coin breakdown
  — honest nulls when there is nothing to compute, never invented zeros
```

**Why it matters:** the win-rate note states exactly what is counted (share
of closed fills with closedPnl > 0), the fees split shows builder bribes, and
an empty account returns explicit zeros with `fills_analyzed: 0` rather than
a silent empty list.
