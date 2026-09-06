"""Stdlib-only POST client for the Hyperliquid /info endpoint.

Endpoint: POST https://api.hyperliquid.xyz/info with a JSON body
{"type": ...} (plus optional per-type keys). Keyless, read-only public
data - no signing, no exchange actions, ever.

Rate budget: Hyperliquid documents a weight-based limit of 1200
weight/min on /info. Every request type has a known weight (see
_WEIGHTS); before each request the module checks the rolling 60s window
and, if the next request would exceed 1200, waits up to 5s once, then
raises a clear RuntimeError naming the limit. It never sleep-blocks
forever - callers (MCP tools) degrade to an honest error dict.

TTL caches per data type keep repeated tool calls cheap:
    allMids 15s, recentTrades 15s, l2Book 5s, metaAndAssetCtxs 60s,
    spotMeta 3600s, spotMetaAndAssetCtxs 60s, candleSnapshot 300s,
    fundingHistory 300s, and per-address 60s for
    clearinghouseState/userFills/userFunding/spotClearinghouseState.

Schema gotchas (live-verified 2026-09-05/06, see API_NOTES.md): ALL
numerics arrive as STRINGS ("42350.0", "" never for prices but possible
for empty optionals); metaAndAssetCtxs returns a [meta, assetCtxs] pair
(list of two); spotMeta carries {universe, deployAuctionStatus,
registeredContracts}; userFills rows may repeat under
"fill"->"closedPnl" variants; fundingHistory is a flat
[{coin, fundingRate, premium, time}, ...] list.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "hyperliquid-agent-gateway/0.1",
      "Content-Type": "application/json"}

# ---------------------------------------------------------------------------
# Weight-based rate bucket: 1200 weight / rolling 60s on this module.

WEIGHT_LIMIT = 1200
_WEIGHTS: dict[str, int] = {
    "allMids": 2,
    "l2Book": 2,
    "metaAndAssetCtxs": 20,
    "spotMeta": 20,
    "spotMetaAndAssetCtxs": 20,
    "candleSnapshot": 60,
    "fundingHistory": 20,   # + extra per 20 items beyond the first (below)
    "recentTrades": 20,
    "clearinghouseState": 20,
    "userFills": 20,
    "userFunding": 20,
    "spotClearinghouseState": 20,
    "meta": 20,
}
# Extra weight for fundingHistory: one more unit per 20 items requested
# beyond the first item batch (per documented /info pricing).
_FUNDING_ITEMS_PER_WEIGHT = 20

_LOCK = threading.Lock()
_SPEND: list[tuple[float, int]] = []  # (monotonic ts, weight) ledger

_TIMEOUT = 25.0
_RETRIES = 3  # 1 try + 2 retries


def request_weight(req_type: str, expected_items: int = 0) -> int:
    """Weight of one /info request of `req_type`.

    fundingHistory carries +1 weight per 20 items beyond the first
    (`expected_items` is how many rows the caller wants); everything
    else is flat per _WEIGHTS.
    """
    base = _WEIGHTS.get(req_type, 20)
    if req_type == "fundingHistory" and expected_items > _FUNDING_ITEMS_PER_WEIGHT:
        base += (expected_items - 1) // _FUNDING_ITEMS_PER_WEIGHT
    return base


def _prune(now: float) -> None:
    """Drop ledger entries older than the 60s window (caller holds _LOCK)."""
    cutoff = now - 60.0
    while _SPEND and _SPEND[0][0] <= cutoff:
        _SPEND.pop(0)


def spent_last_minute() -> int:
    """Weight spent in the rolling 60s window (test/observability hook)."""
    with _LOCK:
        _prune(time.monotonic())
        return sum(w for _, w in _SPEND)


def _admit(weight: int) -> None:
    """Reserve `weight` or raise. One short <=5s wait allowed, then raise.

    Never blocks forever: an over-budget caller gets a loud RuntimeError
    naming the 1200/min limit instead of a silent multi-minute stall.
    """
    with _LOCK:
        now = time.monotonic()
        _prune(now)
        used = sum(w for _, w in _SPEND)
        if used + weight > WEIGHT_LIMIT:
            # single short wait (<=5s) in case the window is about to
            # roll over, then give up honestly
            time.sleep(5.0)
            now = time.monotonic()
            _prune(now)
            used = sum(w for _, w in _SPEND)
            if used + weight > WEIGHT_LIMIT:
                raise RuntimeError(
                    f"hyperliquid /info weight limit reached: {used} of "
                    f"{WEIGHT_LIMIT} weight/min used, next request needs "
                    f"{weight}; wait for the 60s window to roll or narrow "
                    f"the query (fewer coins / smaller limit)")
        _SPEND.append((now, weight))


# ---------------------------------------------------------------------------
# TTL caches

_TTLS: dict[str, float] = {
    "allMids": 15.0,
    "recentTrades": 15.0,
    "l2Book": 5.0,
    "metaAndAssetCtxs": 60.0,
    "spotMeta": 3600.0,
    "spotMetaAndAssetCtxs": 60.0,
    "candleSnapshot": 300.0,
    "fundingHistory": 300.0,
    "clearinghouseState": 60.0,
    "userFills": 60.0,
    "userFunding": 60.0,
    "spotClearinghouseState": 60.0,
    "meta": 60.0,
}
_CACHE: dict[str, tuple[float, Any]] = {}  # cache key -> (ts, payload)
_CACHE_LOCK = threading.Lock()


def _cache_key(req_type: str, body: dict) -> str:
    """Stable cache key: type + sorted identity params (address/coin/interval)."""
    ident = {k: v for k, v in body.items()
             if k != "type" and isinstance(v, (str, int, float))}
    suffix = "".join(f"|{k}={ident[k]}" for k in sorted(ident))
    return req_type + suffix


def cache_age(req_type: str, body: dict | None = None) -> float | None:
    """Seconds since the cached payload for this request was fetched.

    None when not cached. Tools surface this as age_seconds for honest
    freshness reporting.
    """
    key = _cache_key(req_type, dict(body or {}))
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None:
        return None
    return time.monotonic() - entry[0]


def _cached_get(req_type: str, body: dict, expected_items: int = 0) -> Any:
    """TTL-cache-aware POST. Returns the decoded JSON payload."""
    key = _cache_key(req_type, body)
    ttl = _TTLS.get(req_type, 60.0)
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None and time.monotonic() - entry[0] <= ttl:
            return entry[1]
    payload = post(req_type, body, expected_items=expected_items)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), payload)
    return payload


def post(req_type: str, body: dict | None = None, *,
         expected_items: int = 0, retries: int = _RETRIES) -> Any:
    """POST one /info request (no cache). Body always merges {"type": ...}.

    Retries with 2s*(n+1) backoff on 429/5xx (2 retries); ValueError on
    a final HTTP 4xx; RuntimeError when unreachable - same shape as the
    arcus gateway client.
    """
    full = {"type": req_type, **(body or {})}
    data = json.dumps(full).encode()
    weight = request_weight(req_type, expected_items)
    _admit(weight)
    for attempt in range(retries):
        req = urllib.request.Request(BASE, data=data, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code == 429 or 500 <= e.code <= 599) and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            hint = ("rate limited - retry after backoff" if e.code == 429
                    else e.reason)
            raise ValueError(
                f"hyperliquid /info {e.code} for type={req_type}: {hint} "
                f"(after {attempt + 1} attempt(s))") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError) as e:
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(
                f"hyperliquid /info unreachable after {retries} tries: "
                f"type={req_type} - check network/upstream status") from e
    raise RuntimeError(f"hyperliquid /info unreachable: type={req_type}")


# ---------------------------------------------------------------------------
# Typed helpers (cached) - what the tools call.

def all_mids() -> dict:
    """{"COIN": "px", ...} mids for the whole universe (15s cache)."""
    return _cached_get("allMids", {})


def l2_book(coin: str) -> dict:
    """{"coin", "time", "levels": {"bids": [...], "asks": [...]}} (5s cache)."""
    return _cached_get("l2Book", {"coin": _norm_coin(coin)})


def meta() -> dict:
    """Perp universe meta {"universe": [{name, maxLeverage, onlyIsolated}]}."""
    return _cached_get("meta", {})


def meta_and_asset_ctxs() -> list:
    """[meta, assetCtxs] pair (60s cache) - THE perp market snapshot.

    meta: {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}
    assetCtxs: [{"dayNtlVlm", "openInterest", "premium", "markPx", "midPx",
                 "oraclePx", "impactPxs", "funding", ...}, ...] aligned by
    index with meta.universe. All numerics are STRINGS.
    """
    return _cached_get("metaAndAssetCtxs", {})


def spot_meta() -> dict:
    """Spot universe (3600s cache): {"universe": [{"name": "@1/PURR",
    "tokens": [0, 1], "isCanonical": true, "index"]}], "tokens": [{"name":
    "PURR", "index": 0, "token": "0x..."}], "deployAuctionStatus": "...",
    "registeredContracts": [...]}. Note: universe rows also carry markPx
    on the pair in spotMetaAndAssetCtxs, not here.
    """
    return _cached_get("spotMeta", {})


def spot_meta_and_asset_ctxs() -> list:
    """[spotMeta, spotCtxs] pair (60s cache) - spot market snapshot.

    spotCtxs: [{"markPx", "midPx", "dayNtlVlm", "prevDayPx", "circulating",
                "coin": "@1/PURR", "funding"}, ...].
    """
    return _cached_get("spotMetaAndAssetCtxs", {})


def candle_snapshot(coin: str, interval: str, start_time: int) -> list:
    """candleSnapshot rows (300s cache): [{"t": ms, "T": ms, "s": "BTC",
    "i": "1h", "o": "...", "c": "...", "h": "...", "l": "...",
    "v": "...", "n": n}, ...] oldest-first; numerics are STRINGS."""
    return _cached_get("candleSnapshot",
                       {"req": {"coin": _norm_coin(coin),
                                "interval": interval,
                                "startTime": int(start_time)}})


def funding_history(coin: str, start_time: int | None = None,
                    end_time: int | None = None) -> list:
    """[{coin, fundingRate, premium, time}, ...] (300s cache), newest-last.

    startTime is REQUIRED upstream; when omitted it defaults to 7 days
    ago (hourly funding -> ~168 rows).
    """
    if start_time is None:
        start_time = int((time.time() - 7 * 24 * 3600) * 1000)
    body: dict = {"coin": _norm_coin(coin),
                  "startTime": int(start_time)}
    if end_time is not None:
        body["endTime"] = int(end_time)
    return _cached_get("fundingHistory", body,
                       expected_items=168)  # ~7d hourly default window


def recent_trades(coin: str) -> list:
    """[{coin, side, px, sz, time, hash, users: [maker, taker]}, ...]
    (15s cache). users carries BOTH sides' addresses; newest-last."""
    return _cached_get("recentTrades", {"coin": _norm_coin(coin)})


def clearinghouse_state(address: str) -> dict:
    """Perp account state (60s per-address cache): {"marginSummary": {
    "accountValue", "totalNtlPos", "totalRawUsd", "totalMarginUsed",
    "withdrawable"}, "crossMaintenanceMarginUsed": "...",
    "crossMarginSummary": {...}, "assetPositions": [{"position": {
    "coin", "szi", "leverage": {"type": "cross"|"isolated", "value": 20},
    "entryPx", "positionValue", "unrealizedPnl", "returnOnEquity",
    "liquidationPx", "marginUsed"}, "type": "oneWay"}, ...]}.
    liquidationPx may be null (cross positions without enough isolation
    detail). Numerics are STRINGS except flags.
    """
    return _cached_get("clearinghouseState", {"user": _norm_addr(address)})


def user_fills(address: str) -> list:
    """[{coin, dir, px, sz, time, closedPnl, fee, feeToken, builderFee,
    hash}, ...] (60s per-address cache)."""
    return _cached_get("userFills", {"user": _norm_addr(address)})


def user_funding(address: str) -> list:
    """[{coin, fundingRate, premium, time, delta}, ...] non-zero funding
    payments (60s per-address cache); delta is the USD payment (STRING,
    negative when the trader pays)."""
    return _cached_get("userFunding", {"user": _norm_addr(address)})


def spot_clearinghouse_state(address: str) -> dict:
    """HL spot balances (60s per-address cache): {"balances": [{"coin":
    "@1/PURR", "hold": "...", "total": "..."}, ...]}."""
    return _cached_get("spotClearinghouseState",
                       {"user": _norm_addr(address)})


# ---------------------------------------------------------------------------
# Input normalization

def _norm_coin(coin: str) -> str:
    """Coins are upper ('BTC'); '@{index}/NAME' spot pairs pass through."""
    c = (coin or "").strip()
    return c if c.startswith("@") else c.upper()


def _norm_addr(address: str) -> str:
    return (address or "").strip().lower()


def reset_caches() -> None:
    """Drop all TTL caches + the rate ledger (test hook)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _LOCK:
        _SPEND.clear()
