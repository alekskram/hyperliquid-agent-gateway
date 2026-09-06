#!/usr/bin/env python3
"""Live-response recorder: POSTs the /info types + eth_getLogs once
each and saves the raw JSON to tests/fixtures/ for the offline suite.

Total budget: 12 /info requests (weight ~264) + 1-2 EVM requests -
comfortably inside both rate buckets. NOT run by CI or the test suite;
run manually when fixtures need refreshing:

    python scripts/recorder.py --out tests/fixtures
    python scripts/recorder.py --out tests/fixtures --only metaAndAssetCtxs,spotMeta

Writes <name>.json per type (exact filenames the offline tests read).
Existing files are overwritten only for the types actually fetched.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import hyperliquid_mcp.evm as evm        # noqa: E402
import hyperliquid_mcp.info as info      # noqa: E402

# (fixture filename, info request type, extra body) - the 11 /info types
# Account types use a busy live trader (44 open positions incl. an
# isolated one) so clearinghouseState/userFills/userFunding fixtures
# carry rich REAL data in the LIVE response shape (no markPx in
# positions, no withdrawable in marginSummary).
_RECORD_USER = "0xbeccae9ffcb69e9d42a1d4e744abf8056149562d"
INFO_JOBS = [
    ("meta", "meta", {}),
    ("metaAndAssetCtxs", "metaAndAssetCtxs", {}),
    ("spotMeta", "spotMeta", {}),
    ("spotMetaAndAssetCtxs", "spotMetaAndAssetCtxs", {}),
    ("allMids", "allMids", {}),
    ("l2Book", "l2Book", {"coin": "BTC"}),
    ("candleSnapshot", "candleSnapshot",
     {"req": {"coin": "BTC", "interval": "1h",
              "startTime": int((time.time() - 48 * 3600) * 1000)}}),
    ("fundingHistory", "fundingHistory",
     {"coin": "BTC", "startTime": int((time.time() - 168 * 3600) * 1000)}),
    ("recentTrades", "recentTrades", {"coin": "BTC"}),
    ("clearinghouseState", "clearinghouseState",
     {"user": _RECORD_USER}),
    ("userFills", "userFills",
     {"user": _RECORD_USER}),
    ("userFunding", "userFunding",
     {"user": _RECORD_USER}),
    ("spotClearinghouseState", "spotClearinghouseState",
     {"user": _RECORD_USER}),
]

# cap list-shaped fixtures so the repo stays lean; the offline suite
# does not need 2000 rows to prove parsing
_MAX_ROWS = {"userFills": 500, "userFunding": 500}


def record(out_dir: Path, only: list[str] | None = None) -> int:
    """Fetch each requested type once, write <name>.json. Returns the
    number of files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, rtype, body in INFO_JOBS:
        if only and rtype not in only and name not in only:
            continue
        try:
            payload = info.post(rtype, body, expected_items=1)
        except (ValueError, RuntimeError) as e:
            print(f"[skip] {name}: {e}")
            continue
        if isinstance(payload, list) and name in _MAX_ROWS:
            payload = payload[:_MAX_ROWS[name]]
        (out_dir / f"{name}.json").write_text(json.dumps(payload, indent=1))
        print(f"[ok]   {name}.json "
              f"({(out_dir / f'{name}.json').stat().st_size} bytes)")
        written += 1
    # eth_getLogs: Transfer logs for the canonical PURR contract over a
    # modest adaptive window; skipped when the window came back empty
    # (protects an existing non-empty fixture from being blanked)
    if not only or "getLogs" in only or "eth_getLogs" in only:
        try:
            latest = evm.block_number()
            walked = evm.get_logs(
                "0x9b498c3c8a0b8cd8ba1d9851d40d186f1872b44e",
                max(0, latest - 60), latest)
            payload = walked["logs"]
            if not payload:
                print("[skip] getLogs: 0 logs in window (existing "
                      "fixture kept)")
            else:
                (out_dir / "getLogs.json").write_text(
                    json.dumps(payload, indent=1))
                print(f"[ok]   getLogs.json ({len(payload)} logs)")
                written += 1
        except (evm.RpcError, ValueError, RuntimeError) as e:
            print(f"[skip] getLogs: {e}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Record live Hyperliquid /info + HyperEVM responses "
                    "to tests/fixtures/")
    ap.add_argument("--out", default="tests/fixtures",
                    help="output directory (default tests/fixtures)")
    ap.add_argument("--only", default=None,
                    help="comma-separated request types to record "
                         "(default: all)")
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    out = Path(a.out)
    n = record(out, only)
    print(f"\n{n} fixture(s) written to {out}/")
    if n == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
