"""Unit tests for hyperliquid_mcp.info - 100% offline (fixtures + fake
clock). Covers: TTL caches, weight accounting (1200/min), retries on
429/5xx, ValueError on final 4xx, RuntimeError on unreachable, the
fundingHistory weight surcharge, and the string-numeric doc invariants."""
import json
import urllib.error
import urllib.request

import pytest

from hyperliquid_mcp import info


class TestTTLCache:
    def test_meta_pair_cached_within_ttl(self, info_mock):
        mod, calls, clock = info_mock
        info.meta_and_asset_ctxs()
        n = len(calls)
        info.meta_and_asset_ctxs()
        assert len(calls) == n
        assert calls.count("metaAndAssetCtxs") == 1

    def test_meta_pair_refetch_after_ttl(self, info_mock):
        mod, calls, clock = info_mock
        info.meta_and_asset_ctxs()
        n = len(calls)
        clock.advance(info._TTLS["metaAndAssetCtxs"] + 1)
        info.meta_and_asset_ctxs()
        assert len(calls) == n + 1

    def test_l2book_short_ttl(self, info_mock):
        mod, calls, clock = info_mock
        info.l2_book("BTC")
        info.l2_book("BTC")
        assert calls.count("l2Book") == 1
        clock.advance(info._TTLS["l2Book"] + 1)  # 5s
        info.l2_book("BTC")
        assert calls.count("l2Book") == 2

    def test_spotmeta_long_ttl(self, info_mock):
        mod, calls, clock = info_mock
        info.spot_meta()
        clock.advance(1800)  # 30min < 3600s
        info.spot_meta()
        assert calls.count("spotMeta") == 1

    def test_per_address_cache_isolation(self, info_mock):
        mod, calls, clock = info_mock
        a = info.clearinghouse_state("0x" + "11" * 20)
        b = info.clearinghouse_state("0x" + "22" * 20)
        assert calls.count("clearinghouseState") == 2

    def test_cache_age_reports_seconds(self, info_mock):
        mod, calls, clock = info_mock
        info.meta_and_asset_ctxs()
        clock.advance(30)
        age = info.cache_age("metaAndAssetCtxs", {})
        assert age is not None and 29 <= age <= 31

    def test_cache_age_none_when_uncached(self, info_mock):
        assert info.cache_age("recentTrades", {"coin": "BTC"}) is None


class TestRequestBodies:
    def test_body_merges_type(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        seen = []

        def fake(req, timeout=None):
            seen.append(json.loads(req.data.decode()))
            from tests.conftest import FakeResponse
            return FakeResponse({})

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        info.post("clearinghouseState", {"user": "0xabc"})
        assert seen == [{"type": "clearinghouseState", "user": "0xabc"}]

    def test_user_agent_header_mandatory(self, info_mock, monkeypatch):
        seen = []

        def fake(req, timeout=None):
            seen.append(dict(req.headers))
            from tests.conftest import FakeResponse
            return FakeResponse({})

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        info.post("allMids")
        assert any(
            (k.lower(), v) == ("user-agent", "hyperliquid-agent-gateway/0.1")
            for h in seen for k, v in h.items())


class TestWeightBucket:
    def test_request_weight_table(self):
        assert info.request_weight("allMids") == 2
        assert info.request_weight("l2Book") == 2
        assert info.request_weight("metaAndAssetCtxs") == 20
        assert info.request_weight("candleSnapshot") == 60
        assert info.request_weight("userFills") == 20

    def test_funding_history_weight_surcharge(self):
        assert info.request_weight("fundingHistory", 20) == 20
        assert info.request_weight("fundingHistory", 100) == 24
        assert info.request_weight("fundingHistory", 168) == 28

    def test_spent_last_minute_accounts(self, info_mock):
        mod, calls, clock = info_mock
        info.all_mids()
        info.all_mids()
        # second call is a TTL-cache hit: only ONE network request made
        assert info.spent_last_minute() == 2
        assert calls.count("allMids") == 1

    def test_budget_exhaustion_raises(self, info_mock):
        mod, calls, clock = info_mock
        # burn 1190 weight (59 x metaAndAssetCtxs at 20... use ledger)
        with info._LOCK:
            info._SPEND.clear()
            info._SPEND.extend([(clock(), w) for w in [1188]])
        with pytest.raises(RuntimeError, match="1200 weight/min"):
            info.meta_and_asset_ctxs()  # 20 more > 1200

    def test_budget_rolls_off_after_60s(self, info_mock):
        mod, calls, clock = info_mock
        with info._LOCK:
            info._SPEND.clear()
            info._SPEND.extend([(clock() - 61, 1200)])
        info.all_mids()  # old ledger entries pruned: must pass

    def test_budget_error_names_limit(self, info_mock):
        mod, calls, clock = info_mock
        with info._LOCK:
            info._SPEND.clear()
            info._SPEND.extend([(clock(), 1199)])
        with pytest.raises(RuntimeError) as e:
            info.candle_snapshot("BTC", "1h", 0)
        assert "1200" in str(e.value) and "weight" in str(e.value)


class TestRetries:
    def test_retry_on_429_then_success(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        sleeps: list[float] = []
        monkeypatch.setattr(info.time, "sleep",
                            lambda s: sleeps.append(s))
        state = {"n": 0}

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise _http_error(req.full_url, 429, "Too Many Requests")
            from tests.conftest import FakeResponse
            return FakeResponse(load("allMids.json"))

        monkeypatch.setattr(urllib.request, "urlopen", flaky)
        out = info.post("allMids")
        assert isinstance(out, dict) and "BTC" in out
        assert state["n"] == 2
        assert 2.0 in sleeps  # backoff 2s*(0+1)

    def test_retry_on_503_then_success(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        state = {"n": 0}

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] <= 2:
                raise _http_error(req.full_url, 503, "Service Unavailable")
            from tests.conftest import FakeResponse
            return FakeResponse(load("allMids.json"))

        monkeypatch.setattr(urllib.request, "urlopen", flaky)
        info.post("allMids")
        assert state["n"] == 3  # 1 try + 2 retries

    def test_final_4xx_raises_valueerror(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _throw(
                req.full_url, 400, "Bad Request"))
        with pytest.raises(ValueError, match="400"):
            info.post("l2Book", {"coin": "NOPE"})

    def test_network_down_raises_runtimeerror(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock

        def down(req, timeout=None):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(urllib.request, "urlopen", down)
        monkeypatch.setattr(info.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="unreachable"):
            info.post("allMids", retries=2)

    def test_two_retries_max(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        state = {"n": 0}

        def always_500(req, timeout=None):
            state["n"] += 1
            raise _http_error(req.full_url, 500, "Server Error")

        monkeypatch.setattr(urllib.request, "urlopen", always_500)
        monkeypatch.setattr(info.time, "sleep", lambda s: None)
        with pytest.raises(ValueError):
            info.post("allMids")
        assert state["n"] == 3  # exactly 1 + 2 retries


def _http_error(url, code, msg):
    import email.message
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(), None)


def _throw(url, code, msg):
    raise _http_error(url, code, msg)


def load(name):
    from tests.conftest import load as _load
    return _load(name)


class TestTypedHelpers:
    def test_all_mids_shape(self, info_mock):
        mod, calls, clock = info_mock
        mids = info.all_mids()
        assert mids["BTC"] and isinstance(mids["BTC"], str)

    def test_coin_normalization_upper(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        seen = []

        def fake(req, timeout=None):
            seen.append(json.loads(req.data.decode()))
            from tests.conftest import FakeResponse
            return FakeResponse(load("l2Book.json"))

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        info.l2_book("btc")
        assert seen[0]["coin"] == "BTC"

    def test_spot_pair_name_passthrough(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        seen = []

        def fake(req, timeout=None):
            seen.append(json.loads(req.data.decode()))
            from tests.conftest import FakeResponse
            return FakeResponse(load("l2Book.json"))

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        info.l2_book("@/PURR")
        assert seen[0]["coin"] == "@/PURR"

    def test_address_normalization_lower(self, info_mock, monkeypatch):
        mod, calls, clock = info_mock
        seen = []

        def fake(req, timeout=None):
            seen.append(json.loads(req.data.decode()))
            from tests.conftest import FakeResponse
            return FakeResponse(load("clearinghouseState.json"))

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        info.clearinghouse_state("0x" + "AB" * 20)
        assert seen[0]["user"] == "0x" + "ab" * 20
