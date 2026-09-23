# Real Trading Problems Solved

Four scenarios a risk-watcher hits daily; each closes with one call. Outputs are real captures from 2026-09-22, lightly shortened.

## 1. "Where is the funding carry paying?"

The obvious coins pay nothing, funding arrives hourly, and the weight budget
(1200/min) punishes the naive. The screener exists for exactly this.

```
→ funding_carry_screener(topN=5, metric="premium")

  PURR  premium 0.2216%  funding +37.0%/yr (168h avg)   OI $115.2M
  BSV   premium 0.1993%  funding +45.3%/yr (168h avg)   OI $1.7M
  GMX   premium 0.1795%  funding +23.9%/yr (168h avg)   OI $1.0M

  ranked 234 perps from ONE metaAndAssetCtxs call;
  fundingHistory fetched only for the returned top-N
```

Ranking all 234 perps costs one call; history is fetched only for the top-N,
so you stay well inside the weight budget. The 168h average next to the live
rate is what tells you spike from regime.

## 2. "How close is this whale to liquidation?"

A 25x whale is underwater. You want the distance to the cascade in numbers,
not a screenshot.

```
→ liquidation_risk(address="0xfc27…9d9d")

  account value      $21.65M   notional $209.7M
  BTC  SHORT -1117.8 @25x  entry 80652  mark 86379  ROE -177.5%  liq 101621 (-17.6% away)
  ETH  SHORT -28922  @25x  entry 2605   mark 2758   ROE -147.2%  liq 3343   (-21.2% away)
  SOL  SHORT -11935  @20x  ...           ROE -28.1%             liq 1528   (-1192% away)

  liq_px = venue's liquidationPx (estimated: false) — not a guess;
  mark resolved per coin from metaAndAssetCtxs (mark_px_source on every row)
```

Where the venue publishes `liquidationPx`, the distance is the exchange's own
number, flagged `estimated: false`. Where it doesn't, the estimate says so
explicitly and comes from the maintenance-margin ratio. Every mark price
carries its source tag, so the exchange's numbers and the derived ones never
mix silently.

## 3. "What does the whole board look like right now?"

Before drilling anywhere you want the whole board's layout: OI, volume,
premium.

```
→ market_overview(limit=5, sort="open_interest")

  BTC   mark 86374   OI $4.013B   24h vol $2.896B   prem +0.034%/h   maxlev 40
  ETH   mark 2758    OI $3.183B   24h vol $1.135B   prem +0.047%/h   maxlev 25
  HYPE  mark 96.05   OI $2.096B   24h vol $0.546B   prem -0.018%/h   maxlev 10
  ─ totals: 234 perps, OI $14.04B, 24h volume $7.18B
```

One call returns the full board with OI already in USD (`open_interest_usd =
openInterest × markPx`; the raw API speaks coin units) and totals attached.

## 4. "Who is this address, actually?"

Before trusting a wallet's "track record" you want the fills, the fees, the
win-rate and the funding drag.

```
→ trader_activity(address="0xf3f4…744")

  fills PnL, fees (exchange vs builder), volume, win_rate_pct,
  net funding paid/received, open positions, per-coin breakdown
  — honest nulls when there is nothing to compute, never invented zeros
```

The win-rate note states exactly what is counted (closed fills with
closedPnl > 0, partials per fill). Fees split into exchange and builder. An
empty account returns explicit zeros with `fills_analyzed: 0`, never a silent
empty list.
