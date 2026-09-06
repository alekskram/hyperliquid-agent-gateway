"""Regressions for the three live-smoke findings (2026-09-06):

1. l2Book "levels" is an ARRAY [bids, asks] in the live/docs shape
   (dict form tolerated); level rows carry px/sz/n (n, not nSigFigs).
2. candleSnapshot must wrap its params in a "req" object.
3. fundingHistory REQUIRES startTime; the client defaults it to 7d ago.
All offline: the post() surface is monkeypatched and asserted on.
"""
import json
import pathlib

import pytest

from hyperliquid_mcp import info, server as srv

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
L2BOOK = json.loads((FIXTURES / "l2Book.json").read_text())


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
        assert ob["n_sig_figs"] == 5  # from live "n" field
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
