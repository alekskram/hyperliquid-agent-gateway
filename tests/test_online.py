"""LIVE sanity checks of the Hyperliquid /info + HyperEVM layer (opt-in,
never in CI). They hit real endpoints; run with:

    .venv/bin/python -m pytest tests/test_online.py -m online -v

Deselected by default (pyproject addopts "-m 'not online'"). Every test
degrades honestly - skipping with a reason on a well-formed upstream
error instead of failing. Total live budget: <=10 requests.
"""
import pytest

import hyperliquid_mcp.evm as evm
import hyperliquid_mcp.info as info
import hyperliquid_mcp.server as srv

ADDR = "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"  # public example
# busy live trader (40+ open positions incl. an isolated one) so the
# liquidation_risk live check actually exercises positions
RISK_ADDR = "0xbeccae9ffcb69e9d42a1d4e744abf8056149562d"


def _skip_on_error_dict(result: dict, tool: str) -> None:
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        pytest.skip(f"{tool}: {err}")


@pytest.mark.online
def test_info_meta_and_ctxs_live():
    pair = info.meta_and_asset_ctxs()
    assert isinstance(pair, list) and len(pair) == 2
    uni = pair[0].get("universe") or []
    assert len(uni) > 100  # ~233 perps
    assert any(u.get("name") == "BTC" for u in uni)
    # numerics arrive as STRINGS
    ctx = pair[1][0]
    assert isinstance(ctx.get("markPx"), str)


@pytest.mark.online
def test_market_overview_live():
    out = srv.market_overview(limit=5)
    _skip_on_error_dict(out, "market_overview")
    assert out["returned"] == 5
    assert out["totals"]["day_volume_usd"] > 0


@pytest.mark.online
def test_quote_btc_live():
    out = srv.quote("BTC")
    _skip_on_error_dict(out, "quote")
    assert out["bid"] and out["ask"] and out["spread"] >= 0


@pytest.mark.online
def test_candles_live():
    out = srv.candles("BTC", interval="1h", limit=5)
    _skip_on_error_dict(out, "candles")
    assert 1 <= out["count"] <= 5
    assert out["candles"][0]["close"] is not None


@pytest.mark.online
def test_liquidation_risk_live():
    out = srv.liquidation_risk(RISK_ADDR)
    _skip_on_error_dict(out, "liquidation_risk")
    assert "margin_summary" in out
    assert out["open_positions"] > 0
    # v0.1.1 blocker fix: live positions carry no markPx - the tool
    # must resolve marks and compute distances again
    for p in out["positions"]:
        assert p["mark_px_source"] in ("metaAndAssetCtxs", "allMids")
        assert p["mark_px"] is not None
        if p["liq_px"] is not None:
            assert p["liq_distance_pct"] is not None


@pytest.mark.online
def test_evm_chain_id():
    try:
        assert evm.chain_id() == 999
    except evm.RpcError as e:
        pytest.skip(f"evm rpc unavailable: {e.kind}: {e}")


@pytest.mark.online
def test_token_transfers_live():
    # PURR canonical spot token contract
    out = srv.token_transfers(
        "0x9b498c3c8a0b8cd8ba1d9851d40d186f1872b44e", limit=5)
    _skip_on_error_dict(out, "token_transfers")
    assert out["count"] >= 0  # window may legitimately be empty
