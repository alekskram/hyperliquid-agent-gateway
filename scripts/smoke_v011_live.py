"""Live smoke for v0.1.1 (budget: <=10 /info calls, per issue plan).

Order mandated by the ticket: liquidation_risk FIRST (blocker fix),
then previous-fix controls (quote/order_book/candles), then optional
funding_carry_screener if budget remains. Prints one line per call.
"""
import json
import sys

sys.path.insert(0, ".")
from hyperliquid_mcp import server as srv  # noqa: E402

RISK_ADDR = "0xbeccae9ffcb69e9d42a1d4e744abf8056149562d"  # busy trader
SPENT = []


def call(name, fn):
    SPENT.append(name)
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: EXCEPTION {type(e).__name__}: {e}")
        return None
    return out


# 1-2) liquidation_risk on a live address WITH positions (blocker fix)
lr = call("liquidation_risk", lambda: srv.liquidation_risk(RISK_ADDR))
if lr:
    poss = lr.get("positions") or []
    src = {}
    for p in poss:
        src[p.get("mark_px_source")] = src.get(p.get("mark_px_source"), 0) + 1
    no_mark = [p for p in poss if p["mark_px"] is None]
    with_dist = [p for p in poss if p["liq_distance_pct"] is not None]
    iso = [p for p in poss if p.get("leverage_type") == "isolated"]
    fd = lr.get("funding_drag") or {}
    print(f"[{'PASS' if poss else 'FAIL'}] liquidation_risk: "
          f"open_positions={lr.get('open_positions')} "
          f"mark_src={src} no_mark={len(no_mark)} "
          f"with_liq_dist={len(with_dist)}/{len(poss)} "
          f"isolated={len(iso)} funding_drag_events={fd.get('events')}")
    if lr.get("warnings"):
        print(f"  warnings: {json.dumps(lr['warnings'])[:300]}")
    # show one sample row
    if poss:
        s = dict(poss[0])
        print(f"  sample: coin={s.get('coin')} mark={s.get('mark_px')} "
              f"src={s.get('mark_px_source')} liq={s.get('liq_px')} "
              f"dist%={s.get('liq_distance_pct')} est={s.get('estimated')}")

# 3-5) previous-fix controls
q = call("quote", lambda: srv.quote("BTC"))
if q:
    ok = q.get("bid") and q.get("ask") and q.get("mid")
    print(f"[{'PASS' if ok else 'FAIL'}] quote BTC: "
          f"bid={q.get('bid')} ask={q.get('ask')} spread_bps={q.get('spread_bps')}")

ob = call("order_book", lambda: srv.order_book("BTC"))
if ob:
    lv = ob.get("levels") or {}
    ok = lv.get("bids") and lv.get("asks")
    print(f"[{'PASS' if ok else 'FAIL'}] order_book BTC: "
          f"bids={len(lv.get('bids') or [])} asks={len(lv.get('asks') or [])} "
          f"n_sig_figs={ob.get('n_sig_figs')}")

c = call("candles", lambda: srv.candles("BTC"))
if c:
    ok = (c.get("count") or 0) > 0
    first = (c.get("candles") or [{}])[0]
    print(f"[{'PASS' if ok else 'FAIL'}] candles BTC: count={c.get('count')} "
          f"newest_close={first.get('close')}")

# budget: liquidation_risk used clearinghouseState+userFunding(2) +
# metaAndAssetCtxs/allMids via cache = ~4; quote=allMids+l2Book(+1 new
# l2Book); order_book cached l2Book; candles candleSnapshot(+1);
# spent /info weight so far ~ 6 calls of 10 -> screener fits
fc = call("funding_carry_screener",
          lambda: srv.funding_carry_screener(topN=5))
if fc:
    rows = fc.get("rows") or fc.get("top") or []
    ok = len(rows) > 0
    r0 = rows[0] if rows else {}
    print(f"[{'PASS' if ok else 'FAIL'}] funding_carry_screener: rows={len(rows)} "
          f"top={r0.get('coin')} premium={r0.get('premium')}")

print(f"SMOKE CALLS SPENT: {len(SPENT)} -> {SPENT}")
