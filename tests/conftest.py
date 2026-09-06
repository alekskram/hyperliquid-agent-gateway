"""Test isolation: reset gateway caches and fixtures dir BEFORE any
hyperliquid_mcp import carries state. No test in this suite may open a
socket: urllib.request.urlopen is monkeypatched to serve
tests/fixtures/*.json."""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """time.monotonic stand-in we advance by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def load(name: str):
    """Parse a fixture file."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def info_mock(monkeypatch):
    """Fresh info module state + fixture-backed urlopen keyed by
    request "type". Yields (info module, calls list, clock)."""
    import hyperliquid_mcp.info as info

    clock = FakeClock()
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        rtype = body.get("type", "")
        calls.append(rtype)
        by_type = {
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
        f = by_type.get(rtype)
        if f is None:
            raise _http_error(req.full_url, 404, f"unknown type {rtype}")
        return FakeResponse(load(f))

    monkeypatch.setattr(info, "_CACHE", {})
    monkeypatch.setattr(info, "_SPEND", [])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(info.time, "monotonic", clock)
    return info, calls, clock


def _http_error(url: str, code: int, msg: str):
    import email.message
    import urllib.error
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(), None)
