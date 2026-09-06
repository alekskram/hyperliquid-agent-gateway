"""Offline smoke test for hyperliquid_mcp.info - zero HTTP, fixtures only.

Monkeypatches urllib.request.urlopen to serve tests/fixtures/*.json by
request "type" and exercises the TTL caches + the weight bucket + retry
behavior via a fake time.monotonic. Run:

    .venv/bin/python scripts/smoke_api_offline.py
"""
import email.message
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
sys.path.insert(0, str(REPO))

import hyperliquid_mcp.info as info      # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CLOCK = FakeClock()
CALLS = []
SLEEPS = []

BY_TYPE = {
    "allMids": "allMids.json",
    "l2Book": "l2Book.json",
    "meta": "meta.json",
    "metaAndAssetCtxs": "metaAndAssetCtxs.json",
    "spotMeta": "spotMeta.json",
    "spotMetaAndAssetCtxs": "spotMetaAndAssetCtxs.json",
    "candleSnapshot": "candleSnapshot.json",
    "fundingHistory": "fundingHistory.json",
    "recentTrades": "recentTrades.json",
    "clearinghouseState": "clearinghouseState.json",
    "userFills": "userFills.json",
    "userFunding": "userFunding.json",
    "spotClearinghouseState": "spotClearinghouseState.json",
}


def fake_urlopen(req, timeout=None):
    body = json.loads(req.data.decode())
    rtype = body.get("type", "")
    CALLS.append(rtype)
    f = BY_TYPE.get(rtype)
    if f is None:
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", email.message.Message(), None)
    return FakeResponse(json.loads((FIXTURES / f).read_text()))


urllib.request.urlopen = fake_urlopen
info.time.monotonic = CLOCK
info.time.sleep = lambda s: SLEEPS.append(s)
info._CACHE.clear()
info._SPEND.clear()

# --- metaAndAssetCtxs: [meta, ctxs] pair + TTL ---------------------------
pair = info.meta_and_asset_ctxs()
check("metaAndAssetCtxs returns [meta, assetCtxs] pair",
      isinstance(pair, list) and len(pair) == 2
      and "universe" in pair[0] and isinstance(pair[1], list))
check("numerics are strings in ctxs",
      isinstance(pair[1][0]["markPx"], str))
n0 = len(CALLS)
info.meta_and_asset_ctxs()
check("60s TTL cache hit", len(CALLS) == n0)
CLOCK.advance(61)
info.meta_and_asset_ctxs()
check("refetch after TTL", len(CALLS) == n0 + 1)

# --- weight bucket --------------------------------------------------------
check("weights: allMids=2 l2Book=2 ctxs=20 candles=60",
      info.request_weight("allMids") == 2
      and info.request_weight("l2Book") == 2
      and info.request_weight("metaAndAssetCtxs") == 20
      and info.request_weight("candleSnapshot") == 60)
check("fundingHistory surcharge per 20 items",
      info.request_weight("fundingHistory", 168) == 28)
urllib.request.urlopen = fake_urlopen
info._CACHE.clear()
with info._LOCK:
    info._SPEND.clear()
    info._SPEND.append((CLOCK(), 1185))
try:
    info.all_mids()  # would fit (1187) - but next ctx would not
    ok = True
except RuntimeError:
    ok = False
check("admit passes when weight fits", ok)
info._CACHE.clear()
with info._LOCK:
    info._SPEND.clear()
    info._SPEND.append((CLOCK(), 1199))
try:
    info.all_mids()  # 1199 + 2 > 1200 -> raises (after 5s sleep)
    raised = False
except RuntimeError as e:
    raised = "1200" in str(e)
check("weight limit raises RuntimeError naming 1200", raised)
check("short wait <=5s used before raising", SLEEPS == [5.0])
SLEEPS.clear()

# --- retry on 429 ----------------------------------------------------------
SLEEPS.clear()
state = {"n": 0}


def flaky(req, timeout=None):
    state["n"] += 1
    if state["n"] == 1:
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", email.message.Message(), None)
    return FakeResponse(json.loads((FIXTURES / "allMids.json").read_text()))


urllib.request.urlopen = flaky
with info._LOCK:
    info._SPEND.clear()
out = info.post("allMids")
check("429 retried once then success",
      state["n"] == 2 and isinstance(out, dict) and "BTC" in out
      and 2.0 in SLEEPS)

# --- final 4xx -> ValueError, network down -> RuntimeError ------------------
def always_400(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 400, "Bad", email.message.Message(), None)


urllib.request.urlopen = always_400
try:
    info.post("l2Book", {"coin": "X"})
    bad = False
except ValueError:
    bad = True
check("final 4xx raises ValueError", bad)


def down(req, timeout=None):
    raise urllib.error.URLError("no route")


urllib.request.urlopen = down
try:
    info.post("allMids", retries=2)
    unreachable = False
except RuntimeError:
    unreachable = True
check("network down raises RuntimeError", unreachable)

failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:", *[f"  - {n}" for n, _, _ in failed], sep="\n")
sys.exit(1 if failed else 0)
