# Changelog

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
