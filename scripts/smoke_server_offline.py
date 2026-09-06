"""Offline smoke test for hyperliquid_mcp.server - zero HTTP, fixtures only.

Monkeypatches the info/evm module surfaces (the server only ever calls
info.<fn>() / evm.<fn>(), by design), spins the FastMCP in-process and
calls all 12 tools, printing a PASS/FAIL table. Run:

    .venv/bin/python scripts/smoke_server_offline.py
"""
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO))

import hyperliquid_mcp.evm as evm        # noqa: E402
import hyperliquid_mcp.info as info      # noqa: E402
import hyperliquid_mcp.server as srv     # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))


def load(name):
    return json.loads((FIXTURES / name).read_text())


META_PAIR = load("metaAndAssetCtxs.json")
META, CTXS = META_PAIR
SPOT_PAIR = load("spotMetaAndAssetCtxs.json")
SPOT_META, SPOT_CTXS = SPOT_PAIR

# ------------------------------------------------------------ monkeypatch


def _no_http(*a, **k):
    raise RuntimeError("live HTTP attempted in offline smoke!")


info.post = _no_http
info.meta_and_asset_ctxs = lambda: [META, CTXS]
info.meta = lambda: META
info.spot_meta = lambda: SPOT_META
info.spot_meta_and_asset_ctxs = lambda: [SPOT_META, SPOT_CTXS]
info.all_mids = lambda: load("allMids.json")
info.l2_book = lambda coin: load("l2Book.json")
info.candle_snapshot = lambda coin, iv, st: load("candleSnapshot.json")
info.funding_history = lambda coin, **kw: load("fundingHistory.json")
info.recent_trades = lambda coin: load("recentTrades.json")
info.clearinghouse_state = lambda a: load("clearinghouseState.json")
info.user_fills = lambda a: load("userFills.json")
info.user_funding = lambda a: load("userFunding.json")
info.spot_clearinghouse_state = lambda a: load("spotClearinghouseState.json")
info.cache_age = lambda t, b=None: 0.0

evm.post = _no_http
evm.block_number = lambda: 123582
evm.native_balance = lambda a: 2 * 10**18
evm.balance_of = lambda c, a: 10**18
evm.get_logs = lambda c, f, t: {"logs": load("getLogs.json"), "window": {
    "from_block": f, "to_block": t, "requests": 3,
    "widths_used": [30, 60], "final_width": 60, "oldest_covered": f,
    "newest_covered": t, "stopped_reason": "complete", "note": "covered"}}

ADDR = "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"
CONTRACT = "0x9b498c3c8a0b8cd8ba1d9851d40d186f1872b44e"

# ------------------------------------------------------------- 12 tools

mo = srv.market_overview(limit=5)
check("market_overview: rows + totals",
      mo["returned"] == 5 and mo["totals"]["perps"] == len(META["universe"])
      and mo["totals"]["day_volume_usd"] > 0,
      f"perps={mo['totals']['perps']}")
check("market_overview: sorted by OI",
      [r["open_interest_usd"] for r in mo["perps"]] ==
      sorted([r["open_interest_usd"] for r in mo["perps"]], reverse=True))

so = srv.spot_overview(limit=2)
check("spot_overview: pair rows + token links",
      so["returned"] == 2 and so["pairs"][0]["display_name"]
      and isinstance(so["pairs"][0]["tokens"], list))

q = srv.quote("BTC")
check("quote: bid/ask/mid/spread + sizes",
      q["bid"] < q["ask"] and q["mid"] and q["spread"] >= 0
      and q["top_bid_size"] is not None)

ob = srv.order_book("BTC", depth=5)
check("order_book: levels + liquidity totals",
      len(ob["levels"]["bids"]) == 5 and ob["total_bid_liquidity"] > 0
      and ob["n_sig_figs"] is not None)

ca = srv.candles("BTC", interval="1h", limit=10)
check("candles: newest-first OHLCV",
      ca["count"] == 10 and
      [c["t"] for c in ca["candles"]] ==
      sorted([c["t"] for c in ca["candles"]], reverse=True))

tr = srv.trades("BTC", limit=10)
check("trades: rows with users[] both sides",
      tr["count"] == 10 and len(tr["trades"][0]["users"]) == 2)

fh = srv.funding_history("BTC", limit=48)
check("funding_history: rows + premium_now",
      fh["count"] == 48 and fh["premium_now"] is not None)

lr = srv.liquidation_risk(ADDR)
with_liq = next(p for p in lr["positions"] if p["liq_px"] is not None)
no_liq = next(p for p in lr["positions"] if p["liq_px"] is None)
check("liquidation_risk: liqPx present -> not estimated",
      with_liq["estimated"] is False and with_liq["liq_distance_pct"] is not None)
check("liquidation_risk: null liqPx -> estimated flag",
      no_liq["estimated"] is True and no_liq["liq_distance_pct"] is not None)
check("liquidation_risk: LIVE form - mark resolved (no markPx in position)",
      all(p["mark_px"] is not None for p in lr["positions"])
      and {p["mark_px_source"] for p in lr["positions"]} <=
      {"metaAndAssetCtxs", "allMids"})
check("liquidation_risk: funding drag present",
      lr["funding_drag"] and lr["funding_drag"]["events"] > 0)

ta = srv.trader_activity(ADDR, limit=40)
check("trader_activity: totals + win rate + per-coin",
      ta["fills_analyzed"] == 40 and ta["win_rate_pct"] is not None
      and len(ta["per_coin"]) > 0)

sc = srv.funding_carry_screener(topN=3)
check("funding_carry_screener: ranked + funding_history on top rows",
      sc["returned"] == 3 and sc["funding_history_available"] == 3
      and all("funding_history" in r for r in sc["perps"]))

tt = srv.token_transfers(CONTRACT)
check("token_transfers: rows + window metadata",
      tt["count"] > 0 and tt["transfers"][0]["value"] is not None
      and tt["window"]["stopped_reason"] == "complete")

wb = srv.wallet_balance(ADDR)
check("wallet_balance: native + erc20 + spot rows",
      any(t["token"] == "NATIVE" for t in wb["tokens"])
      and any(t["source"] == "eth_call balanceOf" for t in wb["tokens"])
      and any(t["source"] == "spotClearinghouseState" for t in wb["tokens"]))

# ------------------------------------------------------- honesty / errors


def _boom(*a, **k):
    raise RuntimeError("upstream down")


info.meta_and_asset_ctxs = _boom
err = srv.market_overview()
check("honest degradation: error dict, no traceback",
      err.get("error") and err.get("source") == "info"
      and "reason" in err)
info.meta_and_asset_ctxs = lambda: [META, CTXS]  # restore

try:
    srv.quote("NOTACOIN")
    raised = False
except ValueError as e:
    raised = "market_overview" in str(e)
check("unknown coin: helpful ValueError with examples", raised)

evm_get = evm.get_logs


def _limit(*a, **k):
    raise evm.RpcError("budget", kind="rpc-limit")


evm.get_logs = _limit
srv._TRANSFERS_CACHE.clear()  # bypass the 60s tool cache
err2 = srv.token_transfers(CONTRACT)
check("rpc-limit -> honest narrative", "100 req/min" in err2["reason"])
evm.get_logs = evm_get
srv._TRANSFERS_CACHE.clear()

# ---------------------------------------------------------- MCP wire

mcp = srv.build_server()


async def _meta():
    ts = await mcp.list_tools()
    return {t.name: t.annotations for t in ts}


tool_meta = asyncio.run(_meta())
EXPECTED = {"market_overview", "spot_overview", "quote", "order_book",
            "candles", "trades", "funding_history", "liquidation_risk",
            "trader_activity", "funding_carry_screener",
            "token_transfers", "wallet_balance"}


def _ann(ann, attr):
    return (ann.get(attr) if isinstance(ann, dict)
            else getattr(ann, attr, None))


check("build_server: exactly the 12 spec tools",
      set(tool_meta) == EXPECTED, f"got {sorted(tool_meta)}")
check("build_server: all read-only + open-world annotated",
      all(_ann(a, "read_only_hint") is True
          and _ann(a, "destructive_hint") is False
          and _ann(a, "open_world_hint") is True
          for a in tool_meta.values()))


async def _call():
    res = await mcp.call_tool("market_overview", {"limit": 2})
    text = res.content[0].text if res.content else "{}"
    return json.loads(text)


info.meta_and_asset_ctxs = lambda: [META, CTXS]
wire = asyncio.run(_call())
check("MCP wire: call_tool round-trips",
      wire.get("returned") == 2 and wire.get("perps"))

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:", *[f"  - {n}" for n, _, _ in failed], sep="\n")
sys.exit(1 if failed else 0)
