"""Unit tests for hyperliquid_mcp.server tools - 100% offline.

Every test monkeypatches the info / evm module surfaces (the server
only ever calls info.meta() etc., by design) so no HTTP can happen.
Data = the schema-realistic fixtures in tests/fixtures/ plus synthetic
rows for the edge cases the spec demands.
"""
import json
import time
from pathlib import Path

import pytest

import hyperliquid_mcp.evm as evm
import hyperliquid_mcp.info as info
import hyperliquid_mcp.server as srv

FIXTURES = Path(__file__).parent / "fixtures"

META_PAIR = json.loads((FIXTURES / "metaAndAssetCtxs.json").read_text())
META, CTXS = META_PAIR
SPOT_PAIR = json.loads(
    (FIXTURES / "spotMetaAndAssetCtxs.json").read_text())
SPOT_META, SPOT_CTXS = SPOT_PAIR
L2BOOK = json.loads((FIXTURES / "l2Book.json").read_text())
MIDS = json.loads((FIXTURES / "allMids.json").read_text())
CANDLES = json.loads((FIXTURES / "candleSnapshot.json").read_text())
FHIST = json.loads((FIXTURES / "fundingHistory.json").read_text())
TRADES = json.loads((FIXTURES / "recentTrades.json").read_text())
CH_STATE = json.loads((FIXTURES / "clearinghouseState.json").read_text())
FILLS = json.loads((FIXTURES / "userFills.json").read_text())
UFUND = json.loads((FIXTURES / "userFunding.json").read_text())
SCS = json.loads((FIXTURES / "spotClearinghouseState.json").read_text())
LOGS = json.loads((FIXTURES / "getLogs.json").read_text())

ADDR = "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"
BTC_MARK = float(CTXS[0]["markPx"])


@pytest.fixture
def mock_info(monkeypatch):
    """Replace the info surface with fixture-backed lambdas; leak to
    post() fails loudly."""
    def _no_post(*a, **k):  # pragma: no cover - only on a bug
        raise RuntimeError("live HTTP attempted in offline test!")

    monkeypatch.setattr(info, "post", _no_post)
    monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
    monkeypatch.setattr(info, "meta", lambda: META)
    monkeypatch.setattr(info, "spot_meta", lambda: SPOT_META)
    monkeypatch.setattr(info, "spot_meta_and_asset_ctxs",
                        lambda: [SPOT_META, SPOT_CTXS])
    monkeypatch.setattr(info, "all_mids", lambda: MIDS)
    monkeypatch.setattr(info, "l2_book", lambda coin: L2BOOK)
    monkeypatch.setattr(info, "candle_snapshot",
                        lambda coin, iv, st: CANDLES)
    monkeypatch.setattr(info, "funding_history",
                        lambda coin, **kw: FHIST)
    monkeypatch.setattr(info, "recent_trades", lambda coin: TRADES)
    monkeypatch.setattr(info, "clearinghouse_state", lambda a: CH_STATE)
    monkeypatch.setattr(info, "user_fills", lambda a: FILLS)
    monkeypatch.setattr(info, "user_funding", lambda a: UFUND)
    monkeypatch.setattr(info, "spot_clearinghouse_state", lambda a: SCS)
    # neutral freshness: everything "just fetched"
    monkeypatch.setattr(info, "cache_age", lambda t, b=None: 0.0)


@pytest.fixture
def mock_evm(monkeypatch):
    """Replace the evm surface with fixture-backed lambdas."""
    def _no_post(*a, **k):  # pragma: no cover - only on a bug
        raise RuntimeError("live RPC attempted in offline test!")

    monkeypatch.setattr(evm, "post", _no_post)
    monkeypatch.setattr(evm, "block_number", lambda: 123582)
    monkeypatch.setattr(evm, "native_balance", lambda a: 2 * 10**18)
    monkeypatch.setattr(evm, "balance_of", lambda c, a: 10**18)
    monkeypatch.setattr(evm, "get_logs",
                        lambda c, f, t: {"logs": LOGS, "window": {
                            "from_block": f, "to_block": t,
                            "requests": 3, "widths_used": [30, 60],
                            "final_width": 60, "oldest_covered": f,
                            "newest_covered": t,
                            "stopped_reason": "complete",
                            "note": "covered"}})


# ------------------------------------------------------------ numeric hygiene


class TestNumericHygiene:
    def test_f_parses_numeric_strings(self):
        assert srv._f("1.5") == 1.5
        assert srv._f("42350.1") == 42350.1

    def test_f_empty_string_none(self):
        assert srv._f("") is None
        assert srv._f("   ") is None

    def test_f_none_none(self):
        assert srv._f(None) is None

    def test_f_garbage_none(self):
        assert srv._f("abc") is None
        assert srv._f("12.3.4") is None
        assert srv._f({}) is None
        assert srv._f([1]) is None

    def test_f_never_raises(self):
        for probe in (0, 1e18, "1e5", " 42 ", "-0.5", object(), True):
            srv._f(probe)  # must not raise

    def test_i_int_cast(self):
        assert srv._i("1757112500") == 1757112500
        assert srv._i("") is None
        assert srv._i("zz") is None

    def test_f_string_numerics_all_fixtures(self):
        for c in CTXS:
            for k in ("dayNtlVlm", "openInterest", "markPx", "funding"):
                v = srv._f(c.get(k))
                assert v is None or isinstance(v, float)


# --------------------------------------------------------------- market tools


class TestMarketOverview:
    def test_rows_and_totals(self, mock_info):
        out = srv.market_overview()
        assert out["count"] == len(META["universe"])
        assert out["returned"] == 20
        top = out["perps"][0]
        for k in ("coin", "mark_px", "open_interest", "day_volume",
                  "premium", "max_leverage"):
            assert k in top
        assert out["totals"]["perps"] == len(META["universe"])
        assert out["totals"]["open_interest_usd"] > 0

    def test_sort_by_oi_default(self, mock_info):
        out = srv.market_overview(limit=5)
        ois = [r["open_interest_usd"] for r in out["perps"]]
        assert ois == sorted(ois, reverse=True)

    def test_sort_by_volume(self, mock_info):
        out = srv.market_overview(limit=5, sort="volume")
        vols = [r["day_volume"] for r in out["perps"]]
        assert vols == sorted(vols, reverse=True)

    def test_limit_respected(self, mock_info):
        assert srv.market_overview(limit=3)["returned"] == 3

    def test_freshness_field(self, mock_info):
        assert srv.market_overview()["age_seconds"] == 0.0

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom():
            raise RuntimeError("info unreachable")
        monkeypatch.setattr(info, "meta_and_asset_ctxs", boom)
        out = srv.market_overview()
        assert out["error"] and out["source"] == "info"
        assert "unreachable" in out["detail"]


class TestSpotOverview:
    def test_pair_rows(self, mock_info):
        out = srv.spot_overview()
        assert out["count"] == len(SPOT_META["universe"])
        row = out["pairs"][0]
        for k in ("pair", "display_name", "mark_px", "day_volume",
                  "tokens"):
            assert k in row

    def test_index_resolution_via_tokens(self, mock_info):
        out = srv.spot_overview()
        names = [r["display_name"] for r in out["pairs"]]
        assert any("/" in n for n in names)  # '@0/X' -> 'PURR/X' style

    def test_token_rows_carry_contract(self, mock_info):
        out = srv.spot_overview()
        toks = [t for r in out["pairs"] for t in r["tokens"]]
        assert any(t.get("token", "").startswith("0x") for t in toks)

    def test_limit(self, mock_info):
        assert srv.spot_overview(limit=2)["returned"] == 2

    def test_malformed_pair_error_dict(self, monkeypatch):
        monkeypatch.setattr(info, "spot_meta", lambda: SPOT_META)
        monkeypatch.setattr(info, "spot_meta_and_asset_ctxs",
                            lambda: {"bad": 1})
        out = srv.spot_overview()
        assert out.get("error") and out["source"] == "info"


class TestQuote:
    def test_quote_fields(self, mock_info):
        q = srv.quote("BTC")
        assert q["coin"] == "BTC"
        assert q["bid"] < q["ask"]
        assert q["mid"] is not None
        assert q["spread"] == round(q["ask"] - q["bid"], 8)
        assert q["spread_bps"] is not None
        assert q["top_bid_size"] is not None

    def test_case_insensitive(self, mock_info):
        assert srv.quote("btc")["coin"] == "BTC"

    def test_unknown_coin_helpful_error(self, mock_info):
        with pytest.raises(ValueError) as e:
            srv.quote("NOPE")
        assert "market_overview" in str(e.value)
        assert str(e.value).count(",") >= 4  # 5 example coins listed

    def test_empty_coin_raises(self, mock_info):
        with pytest.raises(ValueError):
            srv.quote("")

    def test_spot_pair_passthrough(self, mock_info):
        assert srv.quote("@/PURR")["coin"] == "@/PURR"

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(info, "all_mids", boom)
        monkeypatch.setattr(info, "l2_book", boom)
        monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
        out = srv.quote("BTC")
        assert out["error"] and "down" in out["error"]


class TestOrderBook:
    def test_levels_each_side(self, mock_info):
        ob = srv.order_book("BTC")
        assert len(ob["levels"]["bids"]) == 5
        assert len(ob["levels"]["asks"]) == 5
        assert ob["n_sig_figs"] == 5  # from fixture levels
        assert ob["total_bid_liquidity"] > 0
        assert ob["total_ask_liquidity"] > 0

    def test_depth_cap(self, mock_info):
        assert len(srv.order_book("BTC", depth=3)["levels"]["bids"]) == 3

    def test_notional_invariant(self, mock_info):
        ob = srv.order_book("BTC")
        for side, key in (("bids", "total_bid_liquidity"),
                          ("asks", "total_ask_liquidity")):
            total = sum(l["px"] * l["sz"] for l in ob["levels"][side])
            assert ob[key] == pytest.approx(total, abs=0.01)

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom(coin):
            raise ValueError("400 for l2Book")
        monkeypatch.setattr(info, "l2_book", boom)
        monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
        assert srv.order_book("BTC").get("error")


class TestCandles:
    def test_rows_newest_first(self, mock_info):
        out = srv.candles("BTC")
        ts = [c["t"] for c in out["candles"]]
        assert ts == sorted(ts, reverse=True)

    def test_limit(self, mock_info):
        assert srv.candles("BTC", limit=10)["count"] == 10

    def test_ohlcv_fields(self, mock_info):
        c = srv.candles("BTC")["candles"][0]
        for k in ("t", "open", "high", "low", "close", "volume", "trades"):
            assert k in c
        assert c["high"] >= c["low"]

    def test_interval_validation(self, mock_info):
        for good in ("1m", "15m", "1h", "4h", "1d", "1w", "1M"):
            assert srv.candles("BTC", interval=good)["interval"] == good
        with pytest.raises(ValueError, match="interval"):
            srv.candles("BTC", interval="2h")

    def test_interval_mapped_to_api(self, mock_info, monkeypatch):
        seen = []
        monkeypatch.setattr(
            info, "candle_snapshot",
            lambda coin, iv, st: (seen.append((iv, st)), CANDLES)[1])
        srv.candles("BTC", interval="1d", limit=10)
        assert seen[0][0] == "1d"
        # startTime ~ (limit+1) * interval back
        assert abs(time.time() * 1000 - seen[0][1]) < 11 * 86400_000 * 1.01

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom(*a):
            raise RuntimeError("x")
        monkeypatch.setattr(info, "candle_snapshot", boom)
        monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
        assert srv.candles("BTC").get("error")


class TestTrades:
    def test_rows_with_users(self, mock_info):
        out = srv.trades("BTC")
        assert out["count"] == 20
        t = out["trades"][0]
        assert len(t["users"]) == 2  # both sides
        assert t["side"] in ("B", "A")
        assert t["px"] is not None

    def test_newest_first(self, mock_info):
        ts = [t["time"] for t in srv.trades("BTC")["trades"]]
        assert ts == sorted(ts, reverse=True)

    def test_limit(self, mock_info):
        assert srv.trades("BTC", limit=5)["count"] == 5

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom(coin):
            raise RuntimeError("nope")
        monkeypatch.setattr(info, "recent_trades", boom)
        monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
        assert srv.trades("BTC").get("error")


class TestFundingHistory:
    def test_rows_and_premium_now(self, mock_info):
        out = srv.funding_history("BTC")
        assert out["count"] == 100
        assert out["premium_now"] is not None
        assert "funding_now" in out
        r = out["funding"][0]
        assert set(r) == {"time", "funding_rate", "premium"}

    def test_newest_first(self, mock_info):
        ts = [r["time"] for r in srv.funding_history("BTC")["funding"]]
        assert ts == sorted(ts, reverse=True)

    def test_upstream_failure_warns_but_returns(self, monkeypatch):
        def boom(coin, **kw):
            raise RuntimeError("fh down")
        monkeypatch.setattr(info, "funding_history", boom)
        monkeypatch.setattr(info, "meta_and_asset_ctxs", lambda: [META, CTXS])
        out = srv.funding_history("BTC")
        assert out["funding"] == []
        assert out["warnings"] and "fh down" in out["warnings"][0]["error"]


# -------------------------------------------------------------- account tools


class TestLiquidationRisk:
    def test_address_validation(self, mock_info):
        for bad in ("", "1234", "0x123", "xyz" * 14):
            with pytest.raises(ValueError, match="wallet address"):
                srv.liquidation_risk(bad)

    def test_margin_summary_parsed(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        ms = out["margin_summary"]
        assert ms["account_value"] == 25000.55
        assert ms["total_margin_used"] == 12000.0
        assert out["cross_maintenance_margin_used"] == 960.0

    def test_liqpx_present_not_estimated(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        btc = next(p for p in out["positions"] if p["coin"] == "BTC")
        assert btc["liq_px"] == 52800.0
        assert btc["estimated"] is False
        assert btc["liq_distance_pct"] == pytest.approx(
            (52800.0 - btc["mark_px"]) / btc["mark_px"] * 100, abs=0.01)

    def test_isolated_null_liqpx_estimated(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        eth = next(p for p in out["positions"] if p["coin"] == "ETH")
        assert eth["liq_px"] is None
        assert eth["estimated"] is True
        assert eth["liq_distance_pct"] is not None
        # isolated: mm ratio = 1/(posValue/marginUsed), dist = 100*ratio/lev
        assert eth["liq_distance_pct"] == pytest.approx(
            100.0 * (1.0 / (30000.0 / 3000.0)) / 10.0, rel=0.01)

    def test_cross_null_liqpx_estimated(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        sol = next(p for p in out["positions"] if p["coin"] == "SOL")
        assert sol["liq_px"] is None and sol["estimated"] is True
        # cross: mm ratio = crossMM/accountValue
        assert sol["liq_distance_pct"] == pytest.approx(
            100.0 * (960.0 / 25000.55) / 5.0, rel=0.01)

    def test_funding_drag(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        fd = out["funding_drag"]
        assert fd["events"] == len(UFUND)
        neg = sum(min(0.0, float(f["delta"])) for f in UFUND)
        assert fd["paid_usd"] == pytest.approx(-neg, abs=0.01)

    def test_sides(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        sides = {p["coin"]: p["side"] for p in out["positions"]}
        assert sides["BTC"] == "long" and sides["ETH"] == "short"

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom(a):
            raise RuntimeError("ch down")
        monkeypatch.setattr(info, "clearinghouse_state", boom)
        out = srv.liquidation_risk(ADDR)
        assert out["error"] and out["source"] == "info"

    def test_funding_failure_warns(self, monkeypatch):
        def boom(a):
            raise RuntimeError("uf down")
        monkeypatch.setattr(info, "user_funding", boom)
        out = srv.liquidation_risk(ADDR)
        assert out["funding_drag"] is None
        assert out["warnings"] and "uf down" in out["warnings"][0]["error"]

    def test_note_documents_estimate(self, mock_info):
        out = srv.liquidation_risk(ADDR)
        assert "ESTIMATE" in out["note"]


class TestTraderActivity:
    def test_totals(self, mock_info):
        out = srv.trader_activity(ADDR)
        assert out["fills_analyzed"] == 40
        pnl = sum(float(f["closedPnl"]) for f in FILLS)
        assert out["total_closed_pnl"] == pytest.approx(pnl, abs=0.01)
        vol = sum(float(f["px"]) * float(f["sz"]) for f in FILLS)
        assert out["total_volume"] == pytest.approx(vol, abs=1.0)

    def test_win_rate(self, mock_info):
        out = srv.trader_activity(ADDR)
        wins = sum(1 for f in FILLS if float(f["closedPnl"]) > 0)
        assert out["win_rate_pct"] == pytest.approx(
            wins / len(FILLS) * 100, abs=0.01)

    def test_per_coin_breakdown(self, mock_info):
        out = srv.trader_activity(ADDR)
        assert out["per_coin"] and "coin" in out["per_coin"][0]
        assert set(out["per_coin"][0]) >= {"coin", "fills", "closed_pnl",
                                           "volume", "fees"}

    def test_open_positions(self, mock_info):
        out = srv.trader_activity(ADDR)
        assert out["open_positions"] == 3
        assert len(out["open_position_rows"]) == 3
        assert out["funding_net"] is not None

    def test_limit(self, mock_info):
        assert srv.trader_activity(ADDR, limit=10)["fills_analyzed"] == 10

    def test_address_validation(self, mock_info):
        with pytest.raises(ValueError, match="wallet address"):
            srv.trader_activity("0xdeadbeef")

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom(a):
            raise RuntimeError("fills down")
        monkeypatch.setattr(info, "user_fills", boom)
        out = srv.trader_activity(ADDR)
        assert out["error"] and "fills down" in out["error"]


class TestFundingCarryScreener:
    def test_rank_all_from_one_call(self, mock_info):
        out = srv.funding_carry_screener(topN=5)
        assert out["ranked"] == len(META["universe"])
        assert out["returned"] == 5
        assert out["metric"] == "premium"

    def test_sort_by_abs_premium(self, mock_info):
        out = srv.funding_carry_screener(topN=5)
        prems = [abs(r["premium"]) for r in out["perps"]]
        assert prems == sorted(prems, reverse=True)

    def test_metric_oi(self, mock_info):
        out = srv.funding_carry_screener(topN=5, metric="oi")
        ois = [r["oi"] for r in out["perps"]]
        assert ois == sorted(ois, reverse=True)

    def test_metric_volume(self, mock_info):
        out = srv.funding_carry_screener(metric="volume")
        vols = [r["day_volume"] for r in out["perps"]]
        assert vols == sorted(vols, reverse=True)

    def test_bad_metric_raises(self, mock_info):
        with pytest.raises(ValueError, match="metric"):
            srv.funding_carry_screener(metric="pnl")

    def test_funding_history_only_for_topN(self, mock_info, monkeypatch):
        calls = []
        orig = FHIST

        def fh_spy(coin, **kw):
            calls.append(coin)
            return orig
        monkeypatch.setattr(info, "funding_history", fh_spy)
        out = srv.funding_carry_screener(topN=3)
        assert len(calls) == 3
        assert out["funding_history_available"] == 3
        assert all("funding_history" in r for r in out["perps"])

    def test_funding_history_failure_row_absent(self, mock_info,
                                                monkeypatch):
        def boom(coin, **kw):
            raise RuntimeError("fh down")
        monkeypatch.setattr(info, "funding_history", boom)
        out = srv.funding_carry_screener(topN=3)
        assert out["funding_history_available"] == 0
        assert all("funding_history" not in r for r in out["perps"])

    def test_upstream_failure_error_dict(self, monkeypatch):
        def boom():
            raise ValueError("screener down")
        monkeypatch.setattr(info, "meta_and_asset_ctxs", boom)
        out = srv.funding_carry_screener()
        assert out["error"] and out["source"] == "info"


# ---------------------------------------------------------------- evm tools


class TestTokenTransfers:
    def test_rows(self, mock_evm):
        out = srv.token_transfers("0x9bb8a77a9333b1bc70907b2a20b8d5c1f5f9d6ce")
        assert out["count"] == 12
        r = out["transfers"][0]
        for k in ("from", "to", "value", "value_raw", "txHash",
                  "blockNumber"):
            assert k in r
        assert r["from"].startswith("0x") and len(r["from"]) == 42

    def test_value_format_6dp(self, mock_evm):
        out = srv.token_transfers("0x9bb8a77a9333b1bc70907b2a20b8d5c1f5f9d6ce")
        for r in out["transfers"]:
            assert r["value"] == round(int(r["value_raw"]) / 1e18, 6)
            assert r["value_raw"] == str(int(r["value_raw"]))

    def test_newest_first(self, mock_evm):
        out = srv.token_transfers("0x9bb8a77a9333b1bc70907b2a20b8d5c1f5f9d6ce")
        blks = [r["blockNumber"] for r in out["transfers"]]
        assert blks == sorted(blks, reverse=True)

    def test_contract_validation(self, mock_evm):
        with pytest.raises(ValueError, match="contract address"):
            srv.token_transfers("0x1234")

    def test_rpc_limit_narrative(self, mock_evm, monkeypatch):
        def boom(c, f, t):
            raise evm.RpcError("budget", kind="rpc-limit")
        monkeypatch.setattr(evm, "get_logs", boom)
        out = srv.token_transfers("0x" + "aa" * 20)
        assert out["error"] and "100 req/min" in out["reason"]

    def test_window_too_wide_narrative(self, mock_evm, monkeypatch):
        def boom(c, f, t):
            raise evm.RpcError("query range", kind="rpc-http")
        monkeypatch.setattr(evm, "get_logs", boom)
        out = srv.token_transfers("0x" + "aa" * 20)
        assert "narrower" in out["reason"]

    def test_network_narrative(self, mock_evm, monkeypatch):
        def boom(c, f, t):
            raise evm.RpcError("no route", kind="rpc-network")
        monkeypatch.setattr(evm, "get_logs", boom)
        out = srv.token_transfers("0x" + "aa" * 20)
        assert "unreachable" in out["reason"]

    def test_window_metadata_surfaced(self, mock_evm):
        out = srv.token_transfers("0x" + "aa" * 20)
        assert out["window"]["stopped_reason"] == "complete"
        assert out["window"]["final_width"] == 60

    def test_cached_second_call(self, mock_evm, monkeypatch):
        contract = "0x" + "aa" * 20
        calls = {"n": 0}
        orig = evm.get_logs
        srv._TRANSFERS_CACHE.clear()

        def spy(c, f, t):
            calls["n"] += 1
            return orig(c, f, t)
        monkeypatch.setattr(evm, "get_logs", spy)
        srv.token_transfers(contract)
        srv.token_transfers(contract)
        assert calls["n"] == 1  # 60s tool cache
        srv._TRANSFERS_CACHE.clear()


class TestWalletBalance:
    def test_native_row(self, mock_evm, mock_info):
        out = srv.wallet_balance(ADDR)
        native = next(t for t in out["tokens"] if t["token"] == "NATIVE")
        assert native["balance"] == 2.0  # 2e18 wei
        assert native["source"] == "eth_getBalance"

    def test_erc20_rows_capped_20(self, mock_evm, mock_info):
        out = srv.wallet_balance(ADDR)
        erc20 = [t for t in out["tokens"]
                 if t["source"] == "eth_call balanceOf"]
        assert 1 <= len(erc20) <= 20

    def test_spot_rows(self, mock_evm, mock_info):
        out = srv.wallet_balance(ADDR)
        spot = [t for t in out["tokens"]
                if t["source"] == "spotClearinghouseState"]
        assert len(spot) == len(SCS["balances"])

    def test_address_validation(self, mock_evm, mock_info):
        with pytest.raises(ValueError, match="wallet address"):
            srv.wallet_balance("nothex")

    def test_rpc_limit_stops_scan(self, mock_evm, mock_info, monkeypatch):
        state = {"n": 0}

        def flaky(contract, addr):
            state["n"] += 1
            if state["n"] > 3:
                raise evm.RpcError("budget", kind="rpc-limit")
            return 10**18
        monkeypatch.setattr(evm, "balance_of", flaky)
        out = srv.wallet_balance(ADDR)
        assert any("rpc-limit" in str(w.get("reason", "")) or
                   w.get("reason") == "rpc-limit"
                   for w in out.get("warnings", []))

    def test_native_failure_warns(self, mock_evm, mock_info, monkeypatch):
        def boom(a):
            raise evm.RpcError("down", kind="rpc-network")
        monkeypatch.setattr(evm, "native_balance", boom)
        out = srv.wallet_balance(ADDR)
        assert any(w.get("source") == "evm" for w in out["warnings"])

    def test_spot_failure_warns(self, mock_evm, mock_info, monkeypatch):
        def boom(a):
            raise RuntimeError("scs down")
        monkeypatch.setattr(info, "spot_clearinghouse_state", boom)
        out = srv.wallet_balance(ADDR)
        assert any("scs down" in w.get("error", "")
                   for w in out["warnings"])


# ---------------------------------------------------------------- MCP wire


class TestBuildServer:
    def test_all_12_tools_registered(self):
        import asyncio
        mcp = srv.build_server()
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        expected = {"market_overview", "spot_overview", "quote",
                    "order_book", "candles", "trades", "funding_history",
                    "liquidation_risk", "trader_activity",
                    "funding_carry_screener", "token_transfers",
                    "wallet_balance"}
        assert names == expected

    def test_read_only_annotations(self):
        import asyncio
        mcp = srv.build_server()
        for t in asyncio.run(mcp.list_tools()):
            ann = t.annotations or {}
            ro = (ann.get("read_only_hint") if isinstance(ann, dict)
                  else getattr(ann, "read_only_hint", None))
            de = (ann.get("destructive_hint") if isinstance(ann, dict)
                  else getattr(ann, "destructive_hint", None))
            ow = (ann.get("open_world_hint") if isinstance(ann, dict)
                  else getattr(ann, "open_world_hint", None))
            assert ro is True and de is False and ow is True, t.name

    def test_call_tool_round_trip(self, mock_info):
        import asyncio
        mcp = srv.build_server()

        async def call():
            res = await mcp.call_tool("market_overview", {"limit": 3})
            text = res.content[0].text if res.content else "{}"
            return json.loads(text)

        out = asyncio.run(call())
        assert out["returned"] == 3
