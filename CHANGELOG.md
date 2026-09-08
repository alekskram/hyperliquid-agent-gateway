# Changelog

## 0.1.2 (2026-09-08)

Fix release for the `quote` mid source.

- `quote` (blocker): mid was taken from `allMids` (a mark-style
  oracle) while bid/ask came from `l2Book`, so on thin unit tokens
  mid systematically escaped the quoted spread (UBTC mid 78352.5 at
  bid 78330 / ask 78337), skewing spread-sensitive consumers. mid is
  now computed from the book — `(bid+ask)/2` whenever both sides are
  present — and `allMids` is only a labeled fallback when the book
  lacks a side. Every response carries `mid_source`
  ('book' | 'allMids' | null); with both sides present mid is always
  within [bid, ask]. `spread_bps` is computed from the returned mid
  as before. README tool table now also states the exact signature:
  `quote(coin)` — `coin` is the only parameter (no `size`/`limit`).
- Version bumped to 0.1.2 (pyproject == package == server == lock).

## 0.1.1 (2026-09-06)

Fix release from the independent v0.1.0 review.

- `liquidation_risk` (blocker): the LIVE `clearinghouseState` response
  carries no `markPx` in positions, so `mark_px` and
  `liq_distance_pct` were null in every branch live. Mark is now
  resolved per coin from `metaAndAssetCtxs` (60s cache) with an
  `allMids` fallback; each row reports `mark_px_source`
  ('position' | 'metaAndAssetCtxs' | 'allMids' | null) and distances
  stay null (honest) when no source knows the coin. `trader_activity`
  open-position rows get the same resolver. Note documents that
  `withdrawable` may be omitted by the venue while positions are open.
- `tests/fixtures/clearinghouseState.json` re-recorded from the live
  API (no `markPx` in positions, no `withdrawable` in marginSummary)
  so the offline suite exercises the live shape; regression tests
  cover mark from cache / allMids fallback / neither, liqPx present
  and null, cross and isolated.
- `wallet_balance` / `token_transfers`: per-token decimals via a
  static map of canonical HyperEVM tokens (6 for USDC/USDT-style,
  18 for PURR/HYPE) with honest `decimals_source`
  ('static_map' | 'assumed_18') on every row; unknown tokens assume
  18 and say so.
- `funding_history` / `userFunding` README note: the venue returns
  only non-zero funding events, so a live `funding_drag` of null for a
  fresh address is expected, not a bug.
- Version bumped to 0.1.1 (pyproject == package == server).

## 0.1.0 (2026-09-06)

Initial release.

- Read-only, keyless MCP gateway to Hyperliquid public data.
- `/info` client (`hyperliquid_mcp/info.py`): weight-based rate bucket
  (1200 weight/min), per-type TTL caches, retries with backoff on
  429/5xx, `User-Agent: hyperliquid-agent-gateway/0.1`.
- HyperEVM JSON-RPC client (`hyperliquid_mcp/evm.py`): separate 100
  req/min budget, `RpcError` taxonomy, adaptive `eth_getLogs` window
  (30-block start, halve on too-big, double on quiet, cap 60).
- 12 MCP tools (`hyperliquid_mcp/server.py`): market_overview,
  spot_overview, quote, order_book, candles, trades, funding_history,
  liquidation_risk, trader_activity, funding_carry_screener,
  token_transfers, wallet_balance - all annotated read-only with honest
  degradation (error dicts, never tracebacks) and freshness fields.
- Offline test suite on schema-realistic fixtures (well over a hundred
  checks; run `pytest -q` for the exact count); live online checks are
  opt-in via `-m online`.
- `scripts/recorder.py` live-fixture recorder + systemd units
  (6h schedule, disabled by default).
