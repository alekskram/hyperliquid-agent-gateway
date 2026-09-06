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
INFO_JOBS = [
    ("meta", "meta", {}),
    ("metaAndAssetCtxs", "metaAndAssetCtxs", {}),
    ("spotMeta", "spotMeta", {}),
    ("spotMetaAndAssetCtxs", "spotMetaAndAssetCtxs", {}),
    ("allMids", "allMids", {}),
    ("l2Book", "l2Book", {"coin": "BTC"}),
    ("candleSnapshot", "candleSnapshot",
     {"coin": "BTC", "interval": "1h",
      "startTime": int((time.time() - 48 * 3600) * 1000)}),
    ("fundingHistory", "fundingHistory",
     {"coin": "BTC", "startTime": int((time.time() - 168 * 3600) * 1000)}),
    ("recentTrades", "recentTrades", {"coin": "BTC"}),
    ("clearinghouseState", "clearinghouseState",
     {"user": "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"}),
    ("userFills", "userFills",
     {"user": "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"}),
    ("userFunding", "userFunding",
     {"user": "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"}),
    ("spotClearinghouseState", "spotClearinghouseState",
     {"user": "0x1fc7f7fbd00f9c37edcb53a0a823a5b9f7dc9a44"}),
]


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
        (out_dir / f"{name}.json").write_text(json.dumps(payload, indent=1))
        print(f"[ok]   {name}.json "
              f"({(out_dir / f'{name}.json').stat().st_size} bytes)")
        written += 1
    # eth_getLogs: Transfer logs for the canonical PURR contract over a
    # modest adaptive window
    if not only or "getLogs" in only or "eth_getLogs" in only:
        try:
            latest = evm.block_number()
            walked = evm.get_logs(
                "0x9bb8a77a9333b1bc70907b2a20b8d5c1f5f9d6ce",
                max(0, latest - 60), latest)
            payload = walked["logs"]
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
