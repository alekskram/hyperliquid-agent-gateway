"""Regressions for the live-smoke / independent-review findings:

1. l2Book "levels" is an ARRAY [bids, asks] in the live/docs shape
   (dict form tolerated); level rows carry px/sz/n (n, not nSigFigs).
2. candleSnapshot must wrap its params in a "req" object.
3. fundingHistory REQUIRES startTime; the client defaults it to 7d ago.
4. (v0.1.1, independent-review finding 1) LIVE clearinghouseState
   positions carry NO markPx and marginSummary has NO withdrawable;
   userFunding delta is an OBJECT {type, coin, usdc, ...}, not a string.
All offline: the post() surface is monkeypatched and asserted on.
"""
import json
import pathlib

import pytest

from hyperliquid_mcp import info, server as srv

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
L2BOOK = json.loads((FIXTURES / "l2Book.json").read_text())
CH_STATE = json.loads((FIXTURES / "clearinghouseState.json").read_text())
UFUND = json.loads((FIXTURES / "userFunding.json").read_text())


class TestBookArrayForm:
    def test_fixture_is_array_form(self):
        assert isinstance(L2BOOK["levels"], list)
        assert len(L2BOOK["levels"]) == 2

    def test_dict_form_parses_identically(self):
        bids, asks = L2BOOK["levels"]
        dict_form = dict(L2BOOK)
        dict_form["levels"] = {"bids": bids, "asks": asks}
        a = srv._book_sides(L2BOOK)
        b = srv._book_sides(dict_form)
        assert a == b == (bids, asks)

    def test_garbage_levels_never_raise(self):
        for bad in (None, {}, {"levels": None}, {"levels": [1]},
                    {"levels": "x"}, {"levels": [[], []]}):
            assert srv._book_sides(bad) is not None

    def test_n_field_used_for_n_sig_figs(self, monkeypatch):
        monkeypatch.setattr(info, "meta_and_asset_ctxs",
                            lambda: [{"universe": [{"name": "BTC"}]}, []])
        monkeypatch.setattr(info, "l2_book", lambda coin: L2BOOK)
        monkeypatch.setattr(info, "cache_age", lambda t, b=None: 0.0)
        ob = srv.order_book("BTC")
        bids, _ = srv._book_sides(L2BOOK)
        assert ob["n_sig_figs"] == srv._i(bids[0].get("n"))  # live field
        q = srv.quote("BTC")
        assert q["bid"] is not None and q["ask"] is not None


class TestCandleReqWrapper:
    def test_candle_snapshot_wraps_in_req(self, monkeypatch):
        seen = {}

        def fake_cached_get(req_type, body, expected_items=0):
            seen["type"] = req_type
            seen["body"] = body
            return []

        monkeypatch.setattr(info, "_cached_get", fake_cached_get)
        info.candle_snapshot("BTC", "1h", 1234567890000)
        assert seen["type"] == "candleSnapshot"
        assert seen["body"] == {"req": {"coin": "BTC", "interval": "1h",
                                        "startTime": 1234567890000}}


class TestFundingStartTimeRequired:
    def test_start_time_defaulted_when_omitted(self, monkeypatch):
        seen = {}

        def fake_cached_get(req_type, body, expected_items=0):
            seen["type"] = req_type
            seen["body"] = body
            return []

        monkeypatch.setattr(info, "_cached_get", fake_cached_get)
        info.funding_history("BTC")
        assert seen["body"]["coin"] == "BTC"
        assert "startTime" in seen["body"]  # never sent without it

    def test_explicit_start_time_passthrough(self, monkeypatch):
        seen = {}

        def fake_cached_get(req_type, body, expected_items=0):
            seen["body"] = body
            return []

        monkeypatch.setattr(info, "_cached_get", fake_cached_get)
        info.funding_history("ETH", start_time=1000, end_time=2000)
        assert seen["body"] == {"coin": "ETH", "startTime": 1000,
                                "endTime": 2000}


class TestLiveClearinghouseForm:
    """Guards the LIVE clearinghouseState / userFunding / spotMeta
    shapes the v0.1.1 fix was built against (re-recorded fixtures)."""

    def test_fixture_has_no_markpx_in_positions(self):
        for ap in CH_STATE["assetPositions"]:
            assert "markPx" not in ap["position"], (
                "fixture drifted: position carries markPx again? "
                "re-check the live shape before trusting mark_px_source")

    def test_fixture_margin_summary_has_no_withdrawable(self):
        assert "withdrawable" not in CH_STATE["marginSummary"]

    def test_fixture_has_cross_and_isolated(self):
        types = {ap["position"]["leverage"]["type"]
                 for ap in CH_STATE["assetPositions"]}
        assert types >= {"cross", "isolated"}

    def test_fixture_has_liqpx_present_and_null(self):
        liqs = [ap["position"].get("liquidationPx")
                for ap in CH_STATE["assetPositions"]]
        assert any(x is None for x in liqs)
        assert any(x is not None for x in liqs)

    def test_userfunding_delta_is_object_form(self):
        assert all(isinstance(f.get("delta"), dict) for f in UFUND[:20])
        assert all("usdc" in f["delta"] for f in UFUND[:20])
