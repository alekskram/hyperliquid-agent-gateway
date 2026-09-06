"""hyperliquid-agent-gateway - MCP server (Hyperliquid public data).

Read-only, keyless tools over the /info REST surface (info.py) and the
HyperEVM JSON-RPC (evm.py): market_overview, spot_overview, quote,
order_book, candles, trades, funding_history, liquidation_risk,
trader_activity, funding_carry_screener, token_transfers,
wallet_balance.

Style follows the proven gateway pattern: plain functions registered
via build_server() -> FastMCP, all annotated read-only. All upstream
access goes through the `info` / `evm` module namespaces (info.meta()
etc.) so tests monkeypatch them - never `from .info import x`.

Honesty rules (spec):
  * EVERY numeric from the API is a STRING - _f() parses, never raises,
    None on non-numeric.
  * Every upstream failure returns an error dict
    {"error": ..., "source": ..., "reason": ...}, never a traceback.
  * Cached responses carry a freshness field (age_seconds).
"""
from __future__ import annotations

import re
import time

from . import evm
from . import info

DEFAULT_PORT = 8903
_VERSION = "0.1.0"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# candles: MCP interval -> /info candleSnapshot interval strings
_INTERVALS = {
    "1m": "1m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
    "1M": "1M",
}
_INTERVAL_SECONDS = {
    "1m": 60, "15m": 900, "1h": 3600, "4h": 14400,
    "1d": 86400, "1w": 604800, "1M": 2592000,
}

# token_transfers / wallet_balance tool-level caches
_TRANSFERS_TTL = 60.0
_TRANSFERS_CACHE: dict[str, tuple[float, dict]] = {}
_TOOL_CACHE_LOCK = __import__("threading").Lock()


# ------------------------------------------------------------------ helpers


def _f(s) -> float | None:
    """float(s) for numeric strings ("42350.1", "" -> None); None on
    garbage. Every Hyperliquid numeric is a STRING (API_NOTES.md #1);
    empty/absent optionals must become None, never a crash."""
    if s is None:
        return None
    if isinstance(s, str) and not s.strip():
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _i(s) -> int | None:
    """int(s) for numeric strings; None on garbage. Never raises."""
    v = _f(s)
    return int(v) if v is not None else None


def _round(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def _err(source: str, error: str, reason: str, **extra) -> dict:
    """Honest-degradation error dict (never a traceback)."""
    out = {"error": error, "source": source, "reason": reason,
           "detail": error}
    out.update(extra)
    return out


def _age(req_type: str, body: dict | None = None) -> dict:
    """Freshness block for a cached /info payload."""
    age = info.cache_age(req_type, body)
    if age is None:
        return {"fetched_at": _now_iso()}
    return {"age_seconds": round(age, 1), "fetched_at": _now_iso()}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_coin(coin: str) -> str:
    """Validate a perp coin against the meta universe (helpful error with
    5 example coins, arcus _require_symbol pattern)."""
    want = (coin or "").strip()
    if want.startswith("@"):
        return want  # spot pair names pass through (quote handles spot)
    want = want.upper()
    try:
        universe = _perp_universe()
    except (ValueError, RuntimeError) as e:
        raise ValueError(
            f"cannot validate coin {coin!r}: universe unavailable ({e})")
    names = {u.get("name", "") for u in universe}
    if want not in names:
        examples = ", ".join(sorted(n for n in names if n)[:5])
        raise ValueError(
            f"unknown coin {coin!r}. Call market_overview() for the full "
            f"perp set; examples: {examples}")
    return want


def _perp_universe() -> list[dict]:
    """meta.universe list (via the cached metaAndAssetCtxs pair)."""
    pair = info.meta_and_asset_ctxs()
    meta = pair[0] if isinstance(pair, list) and pair else {}
    return (meta or {}).get("universe") or []


def _perp_ctx(coin: str) -> dict | None:
    """AssetCtx row for `coin` (index join meta.universe[i] == ctxs[i])."""
    want = (coin or "").strip().upper()
    pair = info.meta_and_asset_ctxs()
    if not (isinstance(pair, list) and len(pair) == 2):
        return None
    meta, ctxs = pair
    for u, c in zip((meta or {}).get("universe") or [], ctxs or []):
        if (u or {}).get("name", "").upper() == want:
            return c
    return None


def _wei_to_units(wei: int | None) -> float | None:
    return None if wei is None else wei / 10**18


def _hex_units(data: str | None) -> int | None:
    """Transfer log data word -> int units; None on garbage."""
    if not isinstance(data, str):
        return None
    try:
        return int(data, 16) if data.startswith("0x") else int(data)
    except ValueError:
        return None


def _topic_address(topic: str | None) -> str | None:
    """32-byte topic word -> 0x + 40 hex chars."""
    if not isinstance(topic, str) or len(topic) < 42:
        return None
    return "0x" + topic[-40:]


def _normalize_addr(address: str) -> str:
    """0x + 40 hex validation; raises ValueError with a clear message."""
    addr = (address or "").strip()
    if not _ADDRESS_RE.match(addr):
        raise ValueError(
            f"not a wallet address: {address!r} - expected 0x + 40 hex "
            f"chars")
    return addr.lower()


# ------------------------------------------------------------------- tools


def market_overview(limit: int = 20, sort: str = "open_interest") -> dict:
    """Perp market overview from ONE metaAndAssetCtxs call (~233 perps):
    per-coin mark, open interest, day volume, premium (funding basis),
    max leverage, plus totals (sum OI, sum day volume). Rows are sorted
    by `sort` in {open_interest (default), volume, premium} descending
    and capped at `limit` (max 100). Example: market_overview(limit=10)
    """
    sort_keys = {
        "open_interest": lambda r: r.get("open_interest_usd") or 0.0,
        "volume": lambda r: r.get("day_volume") or 0.0,
        "premium": lambda r: abs(r.get("premium") or 0.0),
    }
    key = sort_keys.get((sort or "").strip().lower(), sort_keys["open_interest"])
    try:
        pair = info.meta_and_asset_ctxs()
    except (ValueError, RuntimeError) as e:
        return _err("info", f"metaAndAssetCtxs unavailable: {e}",
                    "upstream /info failure")
    if not (isinstance(pair, list) and len(pair) == 2):
        return _err("info", "metaAndAssetCtxs returned a malformed payload",
                    "expected [meta, assetCtxs] pair")
    meta, ctxs = pair
    rows: list[dict] = []
    total_oi = total_vol = 0.0
    for u, c in zip((meta or {}).get("universe") or [], ctxs or []):
        oi_coin = _f(c.get("openInterest")) or 0.0
        mark = _f(c.get("markPx"))
        row = {
            "coin": u.get("name"),
            "mark_px": mark,
            "open_interest": _round(_f(c.get("openInterest")), 3),
            "open_interest_usd": _round(oi_coin * mark, 2)
                                 if mark else None,
            "day_volume": _round(_f(c.get("dayNtlVlm")), 2),
            "premium": _round(_f(c.get("premium")), 6),
            "funding": _round(_f(c.get("funding")), 8),
            "max_leverage": u.get("maxLeverage"),
            "only_isolated": bool(u.get("onlyIsolated")),
        }
        rows.append(row)
        total_oi += oi_coin * (mark or 0.0)
        total_vol += _f(c.get("dayNtlVlm")) or 0.0
    rows.sort(key=key, reverse=True)
    n = max(1, min(100, int(limit)))
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "sort": (sort or "open_interest").strip().lower(),
        "perps": rows[:n],
        "totals": {
            "perps": len(rows),
            "open_interest_usd": _round(total_oi, 2),
            "day_volume_usd": _round(total_vol, 2),
        },
        **_age("metaAndAssetCtxs", {}),
        "note": "open_interest is in coin units; open_interest_usd = "
                "openInterest * markPx. premium is the hourly funding "
                "basis (perp mid vs oracle).",
    }


def spot_overview(limit: int = 20) -> dict:
    """Spot market overview (~326 pairs) from spotMeta +
    spotMetaAndAssetCtxs: pair rows (name, mark, day volume, OI where
    published) with HIP-1 <-> ERC-20 links resolved - '@{index}' spot
    coin names are mapped to token names via spotMeta.tokens. Sorted by
    day volume descending, capped at `limit` (max 100).
    Example: spot_overview(limit=10)
    """
    try:
        sm = info.spot_meta()
        pair = info.spot_meta_and_asset_ctxs()
    except (ValueError, RuntimeError) as e:
        return _err("info", f"spot meta unavailable: {e}",
                    "upstream /info failure")
    # spotMetaAndAssetCtxs ALSO returns [spotMeta, ctxs]
    if isinstance(pair, list) and len(pair) == 2:
        ctxs = pair[1] or []
    else:
        return _err("info", "spotMetaAndAssetCtxs returned a malformed "
                            "payload", "expected [spotMeta, ctxs] pair")
    tokens = {t.get("index"): t for t in (sm.get("tokens") or [])
              if isinstance(t.get("index"), int)}
    ctx_by_coin = {c.get("coin"): c for c in ctxs
                   if isinstance(c, dict) and c.get("coin")}
    rows: list[dict] = []
    for u in (sm.get("universe") or []):
        name = u.get("name") or ""
        ctx = ctx_by_coin.get(name, {})
        tok_ids = u.get("tokens") or []
        token_rows = []
        for tid in tok_ids:
            t = tokens.get(tid)
            if t:
                token_rows.append({
                    "name": t.get("name"),
                    "index": t.get("index"),
                    "token": t.get("token"),  # 0x ERC-20 address or name
                    "is_canonical_pair": bool(u.get("isCanonical")),
                })
        rows.append({
            "pair": name,
            "display_name": _spot_display_name(name, tokens),
            "mark_px": _round(_f(ctx.get("markPx"))),
            "day_volume": _round(_f(ctx.get("dayNtlVlm")), 2),
            "open_interest": _round(_f(ctx.get("dayNtlVlm")), 2),  # spot OI
            # spot publishes notional volume; OI is null where absent
            "tokens": token_rows,
            "is_canonical": bool(u.get("isCanonical")),
        })
    # prefer explicit OI when present, else fall back to volume sort key
    rows.sort(key=lambda r: r.get("day_volume") or 0.0, reverse=True)
    n = max(1, min(100, int(limit)))
    return {
        "count": len(rows),
        "returned": min(n, len(rows)),
        "pairs": rows[:n],
        **_age("spotMetaAndAssetCtxs", {}),
        "note": "spot pairs are '@{index}/BASE' - display_name resolves "
                "the index token via spotMeta.tokens; token.token is the "
                "ERC-20 contract where the token is an HIP-1 deployed "
                "coin. Spot OI is not published per-pair; "
                "open_interest mirrors notional volume as the closest "
                "activity measure.",
    }


def _spot_display_name(pair: str, tokens: dict) -> str:
    """'@1/PURR' -> 'PURR/HYPE'-style readable name."""
    if not pair.startswith("@"):
        return pair
    bits = pair.split("/", 1)
    idx = bits[0][1:]
    base = bits[1] if len(bits) > 1 else ""
    if idx.isdigit():
        t = tokens.get(int(idx))
        if t:
            return f"{t.get('name')}/{base}" if base else str(t.get("name"))
    return pair


def quote(coin: str) -> dict:
    """One coin's live quote from allMids + l2Book: bid/ask/mid/spread
    and top-of-book sizes. The coin is validated against the perp
    universe (spot '@{index}/NAME' pairs pass through to the book).
    Example: quote(coin="BTC")
    """
    raw = (coin or "").strip()
    if not raw:
        raise ValueError("coin required")
    if not raw.startswith("@"):
        try:
            raw = _require_coin(raw)
        except ValueError as e:
            raise ValueError(str(e))
    try:
        mids = info.all_mids()
        book = info.l2_book(raw)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"quote data unavailable for {coin!r}: {e}",
                    "upstream /info failure")
    mid = _f(mids.get(raw)) if isinstance(mids, dict) else None
    levels = (book or {}).get("levels") or {}
    bids = levels.get("bids") or []
    asks = levels.get("asks") or []
    bid = _f(bids[0].get("px")) if bids else None
    ask = _f(asks[0].get("px")) if asks else None
    bid_sz = _f(bids[0].get("sz")) if bids else None
    ask_sz = _f(asks[0].get("sz")) if asks else None
    spread = round(ask - bid, 8) if bid is not None and ask is not None \
        else None
    mid = mid if mid is not None else (
        (bid + ask) / 2 if bid is not None and ask is not None else None)
    return {
        "coin": raw,
        "bid": bid, "ask": ask, "mid": mid,
        "spread": spread,
        "spread_bps": _round(spread / mid * 10000, 2)
                      if spread is not None and mid else None,
        "top_bid_size": bid_sz,
        "top_ask_size": ask_sz,
        "book_time": _i((book or {}).get("time")),
        **_age("l2Book", {"coin": raw}),
    }


def order_book(coin: str, depth: int = 10) -> dict:
    """Aggregated order book for one coin from l2Book: `depth` levels per
    side (default 10, max 100), aggregated by the book's nSigFigs
    precision (default taken from the response levels), with total
    liquidity (sum px*sz) per side. Example: order_book(coin="ETH",
    depth=20)
    """
    raw = (coin or "").strip()
    if not raw:
        raise ValueError("coin required")
    if not raw.startswith("@"):
        raw = _require_coin(raw)
    try:
        book = info.l2_book(raw)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"l2Book unavailable for {coin!r}: {e}",
                    "upstream /info failure")
    levels = (book or {}).get("levels") or {}
    n = max(1, min(100, int(depth)))
    sig = None
    for side in ("bids", "asks"):
        for lvl in levels.get(side) or []:
            if lvl.get("nSigFigs") is not None:
                sig = _i(lvl.get("nSigFigs"))
                break
        if sig is not None:
            break
    out_levels: dict[str, list] = {}
    totals: dict[str, float] = {}
    for side in ("bids", "asks"):
        rows = []
        for lvl in (levels.get(side) or [])[:n]:
            px, sz = _f(lvl.get("px")), _f(lvl.get("sz"))
            rows.append({"px": px, "sz": sz, "n_sig_figs": sig,
                         "notional": _round(px * sz, 2)
                                     if px is not None and sz is not None
                                     else None})
            if px is not None and sz is not None:
                totals[side] = totals.get(side, 0.0) + px * sz
        out_levels[side] = rows
    return {
        "coin": raw,
        "depth": n,
        "levels": out_levels,
        "total_bid_liquidity": _round(totals.get("bids"), 2),
        "total_ask_liquidity": _round(totals.get("asks"), 2),
        "n_sig_figs": sig,
        "book_time": _i((book or {}).get("time")),
        **_age("l2Book", {"coin": raw}),
        "note": "levels arrive aggregated to nSigFigs significant "
                "figures by the venue; total liquidity = sum(px*sz) over "
                "the returned depth only.",
    }


def candles(coin: str, interval: str = "1h", limit: int = 100) -> dict:
    """OHLCV candles from candleSnapshot for `coin`: rows newest-first,
    capped at `limit` (max 500). startTime is computed so roughly
    `limit` candles of `interval` are requested. Valid intervals: 1m,
    15m, 1h, 4h, 1d, 1w, 1M. Example: candles(coin="BTC",
    interval="4h", limit=50)
    """
    raw = (coin or "").strip()
    if not raw:
        raise ValueError("coin required")
    iv = (interval or "").strip()
    if iv not in _INTERVALS:
        raise ValueError(
            f"unknown interval {interval!r}: use one of "
            f"{', '.join(_INTERVALS)}")
    api_iv = _INTERVALS[iv]
    n = max(1, min(500, int(limit)))
    step = _INTERVAL_SECONDS[iv]
    start_ms = int((time.time() - (n + 1) * step) * 1000)
    if not raw.startswith("@"):
        raw = _require_coin(raw)
    try:
        rows_raw = info.candle_snapshot(raw, api_iv, start_ms)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"candleSnapshot unavailable for {coin!r}: {e}",
                    "upstream /info failure")
    rows = [{
        "t": _i(c.get("t")),
        "open": _f(c.get("o")), "high": _f(c.get("h")),
        "low": _f(c.get("l")), "close": _f(c.get("c")),
        "volume": _f(c.get("v")), "trades": _i(c.get("n")),
    } for c in rows_raw or []]
    rows.sort(key=lambda r: r.get("t") or 0, reverse=True)  # newest-first
    return {
        "coin": raw,
        "interval": iv,
        "count": len(rows[:n]),
        "candles": rows[:n],
        **_age("candleSnapshot",
               {"coin": raw, "interval": api_iv, "startTime": start_ms}),
    }


def trades(coin: str, limit: int = 20) -> dict:
    """Recent public fills from recentTrades(coin) WITH both sides'
    addresses: rows (newest-first, max 500) carry side, px, sz, time,
    coin, hash and users [maker, taker]. Example: trades(coin="BTC",
    limit=10)
    """
    raw = (coin or "").strip()
    if not raw:
        raise ValueError("coin required")
    if not raw.startswith("@"):
        raw = _require_coin(raw)
    try:
        raws = info.recent_trades(raw)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"recentTrades unavailable for {coin!r}: {e}",
                    "upstream /info failure")
    n = max(1, min(500, int(limit)))
    rows = [{
        "coin": t.get("coin"),
        "side": t.get("side"),  # 'B' | 'A'
        "px": _f(t.get("px")),
        "sz": _f(t.get("sz")),
        "time": _i(t.get("time")),
        "hash": t.get("hash"),
        "users": t.get("users") or [],  # [maker, taker] addresses
    } for t in raws or []]
    rows.sort(key=lambda r: r.get("time") or 0, reverse=True)
    return {
        "coin": raw,
        "count": len(rows[:n]),
        "trades": rows[:n],
        **_age("recentTrades", {"coin": raw}),
    }


def funding_history(coin: str, limit: int = 100) -> dict:
    """Hourly funding history for `coin` (fundingHistory) plus the
    CURRENT premium from metaAndAssetCtxs (premium_now): rows carry
    fundingRate (hourly), premium, time (ms). Newest-first, capped at
    `limit` (max 500). Example: funding_history(coin="BTC", limit=48)
    """
    raw = (coin or "").strip()
    if not raw:
        raise ValueError("coin required")
    if not raw.startswith("@"):
        raw = _require_coin(raw)
    n = max(1, min(500, int(limit)))
    errors: list[dict] = []
    try:
        hist = info.funding_history(raw)
    except (ValueError, RuntimeError) as e:
        hist = []
        errors.append(_err("info", f"fundingHistory unavailable: {e}",
                           "upstream /info failure"))
    try:
        ctx = _perp_ctx(raw)
        prem_now = _f((ctx or {}).get("premium")) if ctx else None
        fund_now = _f((ctx or {}).get("funding")) if ctx else None
    except (ValueError, RuntimeError) as e:
        prem_now = fund_now = None
        errors.append(_err("info", f"current premium unavailable: {e}",
                           "metaAndAssetCtxs failure"))
    rows = [{
        "time": _i(h.get("time")),
        "funding_rate": _f(h.get("fundingRate")),
        "premium": _f(h.get("premium")),
    } for h in hist or []]
    rows.sort(key=lambda r: r.get("time") or 0, reverse=True)
    out = {
        "coin": raw,
        "count": len(rows[:n]),
        "funding": rows[:n],
        "premium_now": prem_now,
        "funding_now": fund_now,
        **_age("fundingHistory", {"coin": raw}),
    }
    if errors:
        out["warnings"] = errors
    return out


def liquidation_risk(address: str) -> dict:
    """KEY TOOL. Liquidation risk for one perp account
    (clearinghouseState + userFunding): margin summary (accountValue,
    totalRawUsd, totalMarginUsed, withdrawable), cross maintenance
    margin, per-position leverage/entry/mark/unrealizedPnl and
    liquidationPx when the venue provides it. When liquidationPx is
    null, liq_distance_pct is an ESTIMATE from the maintenance-margin
    ratio (crossMaintenanceMarginUsed / marginSummary.accountValue;
    isolated positions use marginUsed / positionValue) - flagged
    'estimated': true, liqPx stays null (honest). funding_drag totals
    negative funding payments from userFunding. Address must be 0x +
    40 hex. Example: liquidation_risk(address="0x...")
    """
    addr = _normalize_addr(address)
    errors: list[dict] = []
    try:
        state = info.clearinghouse_state(addr)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"clearinghouseState unavailable: {e}",
                    "upstream /info failure")
    ms = (state or {}).get("marginSummary") or {}
    cross_mm = _f(state.get("crossMaintenanceMarginUsed"))
    acct_value = _f(ms.get("accountValue"))
    positions = []
    for ap in (state or {}).get("assetPositions") or []:
        p = (ap or {}).get("position") or {}
        coin = p.get("coin", "")
        entry = _f(p.get("entryPx"))
        mark = _f(p.get("markPx"))
        szi = _f(p.get("szi"))
        lev = p.get("leverage") or {}
        pos_value = _f(p.get("positionValue"))
        margin_used = _f(p.get("marginUsed"))
        liq_px = _f(p.get("liquidationPx"))  # None when venue omits it
        row: dict = {
            "coin": coin,
            "side": "long" if (szi or 0) > 0 else "short",
            "size": szi,
            "leverage": _f(lev.get("value")),
            "leverage_type": lev.get("type"),  # cross | isolated
            "entry_px": entry,
            "mark_px": mark,
            "position_value": pos_value,
            "margin_used": margin_used,
            "unrealized_pnl": _f(p.get("unrealizedPnl")),
            "roe_pct": None,
            "liq_px": liq_px,
        }
        roe = _f(p.get("returnOnEquity"))
        if roe is not None:
            row["roe_pct"] = _round(roe * 100, 4)
        if liq_px is not None and mark:
            # distance from mark to liquidation, signed toward liq side
            dist = (liq_px - mark) / mark * 100
            row["liq_distance_pct"] = _round(dist, 3)
            row["estimated"] = False
        else:
            # ESTIMATE: maintenance-margin ratio heuristic. Cross:
            # crossMaintenanceMarginUsed / accountValue; isolated:
            # marginUsed / positionValue. Distance in % of mark.
            if (lev.get("type") == "isolated" and pos_value
                    and margin_used):
                mm_ratio = 1.0 / (pos_value / margin_used)
            elif acct_value and cross_mm:
                mm_ratio = cross_mm / acct_value
            else:
                mm_ratio = None
            if mm_ratio is not None and mark:
                # ~1/leverage worth of headroom scaled by mm ratio
                lev_v = _f(lev.get("value")) or 1.0
                dist = 100.0 * mm_ratio / lev_v
                row["liq_distance_pct"] = _round(dist, 3)
                row["estimated"] = True
            else:
                row["liq_distance_pct"] = None
                row["estimated"] = None
        positions.append(row)
    # funding drag from userFunding
    funding_drag = None
    try:
        funding = info.user_funding(addr)
        neg = [ _f(f.get("delta")) for f in funding or [] ]
        neg = [x for x in neg if x is not None]
        if neg:
            funding_drag = {
                "paid_usd": _round(-sum(x for x in neg if x < 0), 2),
                "received_usd": _round(sum(x for x in neg if x > 0), 2),
                "net_usd": _round(sum(neg), 2),
                "events": len(neg),
            }
    except (ValueError, RuntimeError) as e:
        errors.append(_err("info", f"userFunding unavailable: {e}",
                           "funding drag omitted"))
    out: dict = {
        "address": addr,
        "margin_summary": {
            "account_value": acct_value,
            "total_raw_usd": _f(ms.get("totalRawUsd")),
            "total_margin_used": _f(ms.get("totalMarginUsed")),
            "withdrawable": _f(ms.get("withdrawable")),
            "total_ntl_pos": _f(ms.get("totalNtlPos")),
        },
        "cross_maintenance_margin_used": cross_mm,
        "positions": positions,
        "open_positions": len(positions),
        "funding_drag": funding_drag,
        **_age("clearinghouseState", {"user": addr}),
        "note": "liq_px is the venue's liquidationPx when present "
                "(estimated=false); when null, liq_distance_pct is an "
                "ESTIMATE from the maintenance-margin ratio "
                "(crossMaintenanceMarginUsed/accountValue for cross, "
                "marginUsed/positionValue for isolated) divided by "
                "leverage - treat as a rough risk ranking only.",
    }
    if errors:
        out["warnings"] = errors
    return out


def trader_activity(address: str, limit: int = 50) -> dict:
    """Trading profile for one address (userFills + userFunding +
    clearinghouseState): total closed PnL, fees (fee + builderFee),
    volume, fill count, win-rate-ish share of fills with closedPnl > 0,
    per-coin breakdown, net funding and open positions. `limit` caps
    fills analyzed (max 500, newest-first).
    Example: trader_activity(address="0x...", limit=100)
    """
    addr = _normalize_addr(address)
    errors: list[dict] = []
    try:
        fills = info.user_fills(addr)
    except (ValueError, RuntimeError) as e:
        return _err("info", f"userFills unavailable: {e}",
                    "upstream /info failure")
    fills = fills or []
    n = max(1, min(500, int(limit)))
    rows = sorted(fills, key=lambda f: _i(f.get("time")) or 0, reverse=True)
    analyzed = rows[:n]
    total_pnl = total_fee = total_builder = total_vol = 0.0
    wins = 0
    decided = 0
    per_coin: dict[str, dict] = {}
    for f in analyzed:
        coin = f.get("coin") or "?"
        pnl = _f(f.get("closedPnl")) or 0.0
        fee = _f(f.get("fee")) or 0.0
        bfee = _f(f.get("builderFee")) or 0.0
        px, sz = _f(f.get("px")), _f(f.get("sz"))
        vol = px * sz if px is not None and sz is not None else 0.0
        total_pnl += pnl
        total_fee += fee
        total_builder += bfee
        total_vol += vol
        if _f(f.get("closedPnl")) is not None:
            decided += 1
            wins += 1 if pnl > 0 else 0
        c = per_coin.setdefault(coin, {"fills": 0, "closed_pnl": 0.0,
                                       "volume": 0.0, "fees": 0.0})
        c["fills"] += 1
        c["closed_pnl"] += pnl
        c["volume"] += vol
        c["fees"] += fee + bfee
    # funding totals
    funding_net = None
    try:
        funding = info.user_funding(addr)
        deltas = [x for x in (_f(f.get("delta")) for f in funding or [])
                  if x is not None]
        if deltas:
            funding_net = _round(sum(deltas), 2)
    except (ValueError, RuntimeError) as e:
        errors.append(_err("info", f"userFunding unavailable: {e}",
                           "funding totals omitted"))
    # open positions
    open_position_rows: list[dict] = []
    try:
        state = info.clearinghouse_state(addr)
        for ap in (state or {}).get("assetPositions") or []:
            p = (ap or {}).get("position") or {}
            szi = _f(p.get("szi"))
            if szi:
                open_position_rows.append({
                    "coin": p.get("coin"),
                    "side": "long" if szi > 0 else "short",
                    "size": szi,
                    "entry_px": _f(p.get("entryPx")),
                    "unrealized_pnl": _f(p.get("unrealizedPnl")),
                    "leverage": _f((p.get("leverage") or {}).get("value")),
                })
    except (ValueError, RuntimeError) as e:
        errors.append(_err("info", f"clearinghouseState unavailable: {e}",
                           "open positions omitted"))
    coins = [{"coin": k, **{kk: (vv if isinstance(vv, int) else
                                 _round(vv, 4))
                             for kk, vv in v.items()}}
             for k, v in sorted(per_coin.items(),
                                key=lambda kv: -kv[1]["volume"])]
    out: dict = {
        "address": addr,
        "fills_analyzed": len(analyzed),
        "fills_total": len(fills),
        "total_closed_pnl": _round(total_pnl, 4),
        "total_fees": _round(total_fee + total_builder, 4),
        "fees_breakdown": {"exchange": _round(total_fee, 4),
                           "builder": _round(total_builder, 4)},
        "total_volume": _round(total_vol, 2),
        "win_rate_pct": _round(wins / decided * 100, 2) if decided else None,
        "win_rate_note": "share of fills with closedPnl > 0 among "
                         "closed fills (partial closes counted per fill)",
        "funding_net": funding_net,
        "open_positions": len(open_position_rows),
        "open_position_rows": open_position_rows,
        "per_coin": coins,
        **_age("userFills", {"user": addr}),
    }
    if errors:
        out["warnings"] = errors
    return out


def funding_carry_screener(topN: int = 10, metric: str = "premium") -> dict:
    """Rank ALL perps from ONE metaAndAssetCtxs call by `metric`
    (premium default; oi / volume options) and return the topN rows
    with premium, oi, dayVolume, markPx. fundingHistory rows are fetched
    ONLY for the topN coins (weight economy) and appear as
    'funding_history' on rows where that call succeeded.
    Example: funding_carry_screener(topN=5, metric="premium")
    """
    m = (metric or "premium").strip().lower()
    if m not in ("premium", "oi", "volume"):
        raise ValueError(
            f"unknown metric {metric!r}: use 'premium', 'oi' or 'volume'")
    try:
        pair = info.meta_and_asset_ctxs()
    except (ValueError, RuntimeError) as e:
        return _err("info", f"metaAndAssetCtxs unavailable: {e}",
                    "upstream /info failure")
    meta, ctxs = pair if isinstance(pair, list) and len(pair) == 2 else ({}, [])
    ranked: list[dict] = []
    for u, c in zip((meta or {}).get("universe") or [], ctxs or []):
        ranked.append({
            "coin": u.get("name"),
            "premium": _round(_f(c.get("premium")), 6),
            "funding": _round(_f(c.get("funding")), 8),
            "oi": _round(_f(c.get("openInterest")), 3),
            "day_volume": _round(_f(c.get("dayNtlVlm")), 2),
            "mark_px": _f(c.get("markPx")),
        })
    if m == "premium":
        key = lambda r: abs(r.get("premium") or 0.0)  # noqa: E731
    elif m == "oi":
        key = lambda r: r.get("oi") or 0.0  # noqa: E731
    else:
        key = lambda r: r.get("day_volume") or 0.0  # noqa: E731
    ranked.sort(key=key, reverse=True)
    n = max(1, min(50, int(topN)))
    top = ranked[:n]
    # weight economy: fundingHistory ONLY for the topN coins
    fh_available = 0
    for row in top:
        try:
            hist = info.funding_history(row.get("coin") or "")
            rates = [_f(h.get("fundingRate")) for h in hist or []]
            rates = [r for r in rates if r is not None]
            if rates:
                row["funding_history"] = {
                    "hours": len(rates),
                    "avg_hourly": _round(sum(rates) / len(rates), 8),
                    "sum_hourly": _round(sum(rates), 8),
                }
                fh_available += 1
        except (ValueError, RuntimeError):
            pass  # honest: field simply absent on this row
    return {
        "metric": m,
        "ranked": len(ranked),
        "returned": len(top),
        "perps": top,
        "funding_history_available": fh_available,
        **_age("metaAndAssetCtxs", {}),
        "note": "premium is the hourly perp-vs-oracle basis (positive = "
                "perp trades rich; longs pay funding). fundingHistory "
                "fetched only for the returned coins to spare the "
                "1200 weight/min budget.",
    }


def token_transfers(contract: str, limit: int = 100,
                    from_block: int | None = None) -> dict:
    """Recent ERC-20 Transfer events for `contract` on HyperEVM via
    eth_getLogs with an ADAPTIVE window (starts 30 blocks, halves on
    too-big/failing responses, doubles on quiet ones up to 60; active
    tokens need ~30-60 block windows). Rows (newest-first, max 500):
    from, to, value (raw/1e18, 6dp), txHash, blockNumber, ts when the
    log carries a timestamp. `from_block` anchors the walk (default:
    latest - 300). Errors degrade to an honest error dict naming the
    RpcError kind (rate-limit vs window-too-wide narrative).
    Example: token_transfers(contract="0x...", limit=50)
    """
    addr = (contract or "").strip()
    if not _ADDRESS_RE.match(addr):
        raise ValueError(
            f"not a contract address: {contract!r} - expected 0x + 40 "
            f"hex chars")
    addr = addr.lower()
    n = max(1, min(500, int(limit)))
    ck = f"{addr}|{n}|{from_block}"
    with _TOOL_CACHE_LOCK:
        cached = _TRANSFERS_CACHE.get(ck)
        if cached and time.monotonic() - cached[0] <= _TRANSFERS_TTL:
            return cached[1]
    try:
        latest = evm.block_number()
        start = int(from_block) if from_block is not None \
            else max(0, latest - 300)
        walked = evm.get_logs(addr, start, latest)
    except evm.RpcError as e:
        if e.kind == "rpc-limit":
            return _err("evm", f"transfers unavailable: {e}",
                        "local 100 req/min rpc budget exhausted - narrow "
                        "the contract list or retry in a minute")
        if e.kind == "rpc-network" or e.kind == "rpc-timeout":
            return _err("evm", f"transfers unavailable: {e}",
                        "rpc.hyperliquid.xyz/evm unreachable - retry "
                        "shortly")
        return _err("evm", f"transfers unavailable: {e}",
                    "getLogs rejected the range - try a narrower "
                    "from_block (closer to latest)")
    except (ValueError, RuntimeError) as e:
        return _err("evm", f"transfers unavailable: {e}",
                    "unhandled rpc failure")
    logs = walked.get("logs") or []
    win = walked.get("window") or {}
    rows: list[dict] = []
    for log in logs:
        units = _hex_units(log.get("data"))
        topics = log.get("topics") or []
        ts_raw = log.get("blockTimestamp")
        ts = None
        if isinstance(ts_raw, str) and ts_raw.startswith("0x"):
            try:
                ts = _i(int(ts_raw, 16))
            except ValueError:
                ts = None
        elif _i(ts_raw) is not None:
            ts = _i(ts_raw)
        rows.append({
            "from": _topic_address(topics[1] if len(topics) > 1 else None),
            "to": _topic_address(topics[2] if len(topics) > 2 else None),
            "value_raw": str(units) if units is not None else None,
            "value": _round(_wei_to_units(units)),
            "txHash": log.get("transactionHash"),
            "blockNumber": (_i(int(log.get("blockNumber"), 16))
                            if isinstance(log.get("blockNumber"), str)
                            and log.get("blockNumber", "").startswith("0x")
                            else _i(log.get("blockNumber"))),
            "ts": ts,
        })
    rows.sort(key=lambda r: ((r.get("blockNumber") or 0),
                             r.get("txHash") or ""), reverse=True)
    rows = rows[:n]
    out: dict = {
        "contract": addr,
        "count": len(rows),
        "transfers": rows,
        "window": {
            "from_block": win.get("from_block"),
            "to_block": win.get("to_block"),
            "requests": win.get("requests"),
            "final_width": win.get("final_width"),
            "stopped_reason": win.get("stopped_reason"),
            "oldest_covered": win.get("oldest_covered"),
            "newest_covered": win.get("newest_covered"),
        },
        "note": win.get("note"),
    }
    with _TOOL_CACHE_LOCK:
        _TRANSFERS_CACHE[ck] = (time.monotonic(), out)
    return out


def wallet_balance(address: str) -> dict:
    """Wallet balances on HyperEVM + Hyperliquid spot: native balance
    (eth_getBalance), ERC-20 balances via eth_call balanceOf for up to
    20 tokens resolved from spotMeta tokens with 0x contract addresses
    (cap respects the 100 req/min rpc budget), and Hyperliquid spot
    balances via spotClearinghouseState. Per-token rows: token,
    balance_raw (wei), balance (units, 6dp), source.
    Example: wallet_balance(address="0x...")
    """
    addr = _normalize_addr(address)
    errors: list[dict] = []
    tokens_out: list[dict] = []

    # native balance
    try:
        wei = evm.native_balance(addr)
        tokens_out.append({
            "token": "NATIVE",
            "balance_raw": str(wei),
            "balance": _round(_wei_to_units(wei)),
            "source": "eth_getBalance",
        })
    except evm.RpcError as e:
        errors.append(_err("evm", f"native balance unavailable: {e}",
                           e.kind))

    # ERC-20s from spotMeta tokens (cap 20 to respect 100/min)
    try:
        sm = info.spot_meta()
        erc20s = []
        seen: set[str] = set()
        for t in (sm.get("tokens") or []):
            tok = (t.get("token") or "")
            if isinstance(tok, str) and _ADDRESS_RE.match(tok) \
                    and tok.lower() not in seen:
                seen.add(tok.lower())
                erc20s.append({"name": t.get("name"),
                               "contract": tok.lower()})
            if len(erc20s) >= 20:
                break
    except (ValueError, RuntimeError) as e:
        erc20s = []
        errors.append(_err("info", f"spotMeta unavailable: {e}",
                           "erc-20 scan skipped"))
    for t in erc20s:
        try:
            wei = evm.balance_of(t["contract"], addr)
        except evm.RpcError as e:
            if e.kind == "rpc-limit":
                errors.append(_err("evm", f"erc-20 scan stopped at "
                                          f"{len(tokens_out)} token(s): "
                                          f"{e}", e.kind))
                break
            errors.append(_err("evm", f"balanceOf failed for "
                                      f"{t['name']}: {e}", e.kind))
            continue
        tokens_out.append({
            "token": t["name"],
            "contract": t["contract"],
            "balance_raw": str(wei),
            "balance": _round(_wei_to_units(wei)),
            "source": "eth_call balanceOf",
        })

    # Hyperliquid spot balances
    try:
        spot = info.spot_clearinghouse_state(addr)
        for b in (spot or {}).get("balances") or []:
            tokens_out.append({
                "token": b.get("coin"),
                "balance_raw": b.get("total"),
                "balance": _round(_f(b.get("total"))),
                "source": "spotClearinghouseState",
            })
    except (ValueError, RuntimeError) as e:
        errors.append(_err("info", f"spot balances unavailable: {e}",
                           "spotClearinghouseState failure"))

    out: dict = {
        "address": addr,
        "tokens": tokens_out,
        "count": len(tokens_out),
        **_age("spotClearinghouseState", {"user": addr}),
        "note": "NATIVE is the HyperEVM base asset via eth_getBalance; "
                "ERC-20 rows cover up to 20 spot-listed contracts from "
                "spotMeta (100 req/min rpc budget); spot rows are "
                "Hyperliquid-exchange balances (coin may be an "
                "'@{index}/BASE' pair). Zero-balance rows are kept for "
                "auditability.",
    }
    if errors:
        out["warnings"] = errors
    return out


# --------------------------------------------------------------- MCP wire


def build_server():
    """FastMCP instance with the 12 read-only tools wired."""
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = FastMCP(
        "hyperliquid-agent-gateway",
        version=_VERSION,
        instructions=(
            "Hyperliquid DEX public data, read-only and keyless: perp "
            "and spot market overviews, quotes, order books, candles, "
            "trades, funding history and carry screener; account tools "
            "(liquidation_risk, trader_activity, wallet_balance) take a "
            "0x address; token_transfers reads HyperEVM ERC-20 Transfer "
            "logs. Coins are perp universe names like 'BTC' or 'ETH' "
            "(see market_overview); spot pairs look like '@/PURR'. All "
            "API numerics are strings upstream and are parsed to floats "
            "or null - null always means 'not available', never zero."),
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "ok": True, "service": "hyperliquid-agent-gateway",
            "version": _VERSION})

    # read-only by design; destructiveHint explicit False (the default
    # true would mislabel this keyless analytics gateway)
    RO = {"readOnlyHint": True, "destructiveHint": False,
          "openWorldHint": True}
    for tool in (market_overview, spot_overview, quote, order_book,
                 candles, trades, funding_history, liquidation_risk,
                 trader_activity, funding_carry_screener, token_transfers,
                 wallet_balance):
        mcp.tool(tool, annotations=RO)
    return mcp


def main():
    """Console entry point (hyperliquid-agent-gateway). stdio by default
    for local agents; --http for the hosted form (default port 8903)."""
    import argparse
    ap = argparse.ArgumentParser(prog="hyperliquid-agent-gateway")
    ap.add_argument("--http", action="store_true",
                    help="streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    mcp = build_server()
    if a.http:
        mcp.run(transport="http", host=a.host, port=a.port, show_banner=False)
    else:
        mcp.run(show_banner=False)  # stdio: no banner noise in agent logs


if __name__ == "__main__":
    main()
