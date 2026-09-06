"""Stdlib-only JSON-RPC client for HyperEVM (Hyperliquid EVM, chainId 999).

Endpoint: POST https://rpc.hyperliquid.xyz/evm - keyless public node.
Separate rate budget from the /info client: a HARD 100 requests/min
(1 weight each). When the next request would exceed 100 in the rolling
60s window, RpcError(kind="rpc-limit") is raised immediately - no long
blocking sleeps; callers degrade honestly.

Methods used (read-only): eth_blockNumber, eth_getLogs (ERC-20 Transfer
topic 0xddf252ad...523b3ef), eth_call (balanceOf selector 0x70a08231),
eth_getBalance (native WHYPE/ETH balance).

Adaptive get_logs window: the public node caps getLogs responses
(~1MB); active tokens need roughly 30-60 block windows. get_logs()
starts at 30 blocks wide; a failed/too-big response halves the window
(down to a floor of 2); a successful small response doubles it (up to a
cap of 60). The window used is reported back for honest degradation.

Error surface: RpcError with .kind in {"rpc-timeout", "rpc-http",
"rpc-network", "rpc-limit"} plus .status when an HTTP status applies.
"""
import itertools
import json
import threading
import time
import urllib.error
import urllib.request

DEFAULT_RPC_URL = "https://rpc.hyperliquid.xyz/evm"

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f16"
                  "3c4a11628f55a4df523b3ef")
BALANCE_OF_SELECTOR = "0x70a08231"  # keccak("balanceOf(address)")

UA = {"User-Agent": "hyperliquid-agent-gateway/0.1",
      "Content-Type": "application/json"}

# ---------------------------------------------------------------------------
# Separate rate bucket: 100 requests/min, hard.

RATE_LIMIT = 100  # requests per rolling 60s
_LOCK = threading.Lock()
_TIMESTAMPS: list[float] = []  # monotonic ts of admitted requests

_TIMEOUT = 12.0
_RETRIES = 3  # 1 try + 2 retries
_IDS = itertools.count(1)

_WINDOW_START = 30      # initial get_logs width (blocks)
_WINDOW_MIN = 2         # floor when responses are too big
_WINDOW_MAX = 60        # cap for quiet contracts
_RESPONSE_TOO_BIG = 1_000_000  # ~1MB payload cap heuristic
_MAX_SLICES = 14         # hard budget: stop after ~14 getLogs requests


class RpcError(Exception):
    """HyperEVM RPC failure with a taxonomy kind + optional HTTP status.

    kind: "rpc-timeout" (socket timeout - not retried, it already
    waited _TIMEOUT), "rpc-http" (final HTTP error or a JSON-RPC error
    payload), "rpc-network" (unreachable after retries), "rpc-limit"
    (the local 100/min request budget is exhausted).
    """

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def requests_last_minute() -> int:
    """Requests admitted in the rolling 60s window (test hook)."""
    with _LOCK:
        _prune(time.monotonic())
        return len(_TIMESTAMPS)


def _prune(now: float) -> None:
    cutoff = now - 60.0
    while _TIMESTAMPS and _TIMESTAMPS[0] <= cutoff:
        _TIMESTAMPS.pop(0)


def _admit() -> None:
    """Count one request against the 100/min budget or raise RpcError.

    No sleep-blocking: an over-budget caller gets kind="rpc-limit"
    immediately and can surface an honest error dict.
    """
    with _LOCK:
        now = time.monotonic()
        _prune(now)
        if len(_TIMESTAMPS) >= RATE_LIMIT:
            raise RpcError(
                f"hyperliquid evm rpc local request budget exhausted: "
                f"{RATE_LIMIT} requests/min; wait for the 60s window to "
                f"roll or reduce the number of token contracts queried",
                kind="rpc-limit")
        _TIMESTAMPS.append(now)


def _rpc_url() -> str:
    import os
    return os.environ.get("HL_EVM_RPC_URL") or DEFAULT_RPC_URL


def _to_int(value) -> int:
    """Block-ish scalar -> int. 0x-strings are hex, bare digits decimal."""
    if isinstance(value, bool):
        raise ValueError(f"bad block number: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lower().startswith("0x"):
            try:
                return int(s, 16)
            except ValueError:
                pass
        elif s.isdigit():
            return int(s, 10)
    raise ValueError(f"bad block number: {value!r}")


def _block_param(block) -> str:
    """Block tag passthrough ("latest" ...), everything else to 0xhex."""
    if isinstance(block, str) and block.strip().lower() in (
            "latest", "earliest", "pending", "safe", "finalized"):
        return block.strip().lower()
    return hex(_to_int(block))


def post(method: str, params: list | None = None, *, retries: int = _RETRIES):
    """POST one JSON-RPC request; returns the decoded `result` field.

    Counts against the 100/min budget BEFORE dialing. Retries (2
    retries) on 429/5xx/network errors with 2s*(n+1) backoff; timeouts
    are not retried. Any final failure raises RpcError (see class
    docstring for the kind taxonomy).
    """
    body = json.dumps({"jsonrpc": "2.0", "id": next(_IDS),
                       "method": method, "params": params or []}).encode()
    target = _rpc_url()
    for attempt in range(retries):
        _admit()  # budget check per dial attempt
        req = urllib.request.Request(target, data=body, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code == 429 or 500 <= e.code <= 599) and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            try:
                detail = e.read(300).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            hint = detail or e.reason
            kind = "rpc-limit" if e.code == 429 else "rpc-http"
            raise RpcError(
                f"evm rpc {e.code} on {method} ({target}): {hint} "
                f"(after {attempt + 1} attempt(s))",
                kind=kind, status=e.code) from e
        except TimeoutError as e:
            raise RpcError(
                f"evm rpc timeout on {method} ({target}) after {_TIMEOUT}s",
                kind="rpc-timeout") from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if isinstance(e, urllib.error.URLError) and isinstance(
                    getattr(e, "reason", None), TimeoutError):
                raise RpcError(
                    f"evm rpc timeout on {method} ({target}) after "
                    f"{_TIMEOUT}s", kind="rpc-timeout") from e
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RpcError(
                f"evm rpc unreachable on {method} ({target}) after "
                f"{retries} tries: {e}", kind="rpc-network") from e
        except ValueError as e:  # json.loads failed -> garbage body
            raise RpcError(
                f"evm rpc returned invalid JSON on {method} ({target}): {e}",
                kind="rpc-http") from e
        if "error" in payload:
            err = payload.get("error") or {}
            raise RpcError(
                f"evm rpc error on {method} ({target}): "
                f"{err.get('code')} {err.get('message')}",
                kind="rpc-http") from None
        if "result" not in payload:
            raise RpcError(
                f"evm rpc malformed response on {method} ({target}): "
                f"no result", kind="rpc-http")
        return payload["result"]
    raise RpcError(  # defensive: loop always exits above
        f"evm rpc unreachable on {method} ({target})", kind="rpc-network")


def chain_id() -> int:
    """eth_chainId as int (999 / 0x3e7 on HyperEVM mainnet)."""
    return _to_int(post("eth_chainId", []))


def block_number() -> int:
    """Latest eth_blockNumber as int."""
    return _to_int(post("eth_blockNumber", []))


def native_balance(address: str, block="latest") -> int:
    """eth_getBalance(address) in wei as int (native asset)."""
    res = post("eth_getBalance", [(address or "").strip().lower(),
                                  _block_param(block)])
    try:
        return int(res, 16)
    except (TypeError, ValueError):
        raise RpcError(
            f"eth_getBalance returned non-hex result: {res!r}",
            kind="rpc-http")


def balance_of(contract: str, address: str, block="latest") -> int:
    """ERC-20 balanceOf(address) via eth_call, wei as int."""
    data = BALANCE_OF_SELECTOR + _pad_address(address)
    res = post("eth_call", [{"to": (contract or "").strip().lower(),
                             "data": data}, _block_param(block)])
    try:
        return int(res, 16)
    except (TypeError, ValueError):
        raise RpcError(
            f"balanceOf eth_call returned non-hex result: {res!r}",
            kind="rpc-http")


def _pad_address(address: str) -> str:
    """0x-prefixed address -> 64-hex-char right-aligned ABI word."""
    a = (address or "").strip().lower()
    if a.startswith("0x"):
        a = a[2:]
    if len(a) != 40 or any(c not in "0123456789abcdef" for c in a):
        raise ValueError(f"not an EVM address: {address!r}")
    return "0" * 24 + a


def get_logs(contract: str, from_block, to_block=None) -> dict:
    """ERC-20 Transfer logs for `contract` with an ADAPTIVE window.

    The public node caps getLogs responses (~1MB) and active tokens
    produce very dense logs, so a sensible window is ~30-60 blocks.
    This helper fetches [from_block, to_block] (to_block None =
    latest) starting with a _WINDOW_START (30) block slice at the top;
    each slice that fails or comes back too big halves the width
    (floor 2) and retries the same top block; each successful small
    slice doubles the width for the NEXT slice (cap 60). At most
    _MAX_SLICES slices are attempted (budget); non-window failures
    (rate limit, network, timeout) raise RpcError immediately.

    Returns {"logs": [...], "window": {...}} with raw getLogs log
    dicts sorted ascending by (blockNumber, logIndex) and re-org
    removed entries dropped; window carries the walk metadata.
    """
    addr = (contract or "").strip().lower()
    if not addr:
        raise ValueError("contract address required")
    to_bn = _to_int(to_block) if to_block is not None else block_number()
    from_bn = _to_int(from_block)
    if from_bn > to_bn:
        raise ValueError(f"from_block {from_bn} above to_block {to_bn}")

    logs: list[dict] = []
    width = _WINDOW_START
    widths_used: list[int] = []
    slices = 0
    hi = to_bn
    oldest = newest = None
    while True:
        if hi < from_bn:
            reason = "complete"
            break
        if slices >= _MAX_SLICES:
            reason = "budget"
            break
        lo = max(hi - width + 1, from_bn)
        slices += 1
        try:
            batch = post("eth_getLogs", [{
                "address": addr,
                "topics": [TRANSFER_TOPIC],
                "fromBlock": hex(lo),
                "toBlock": hex(hi),
            }])
        except RpcError as e:
            if _is_window_error(e):
                new_width = max(_WINDOW_MIN, width // 2)
                if new_width == width:
                    # cannot shrink further: this slice is unreachable
                    reason = "window-closed"
                    break
                width = new_width
                continue
            raise  # non-window failures stay honest and loud
        raw_len = len(json.dumps(batch or []))
        if raw_len > _RESPONSE_TOO_BIG:
            # treat as too big: shrink and retry the same top block
            new_width = max(_WINDOW_MIN, width // 2)
            if new_width == width:
                reason = "response-too-big"
                break
            width = new_width
            continue
        widths_used.append(width)
        for entry in batch or []:
            if isinstance(entry, dict) and not entry.get("removed"):
                logs.append(entry)
        if newest is None:
            newest = hi
        oldest = lo
        hi = lo - 1
        # success + small: widen for the next slice (up to 60)
        if raw_len < _RESPONSE_TOO_BIG // 4:
            width = min(_WINDOW_MAX, width * 2)

    logs.sort(key=lambda l: (_to_int(l.get("blockNumber") or "0x0"),
                             _to_int(l.get("logIndex") or "0x0")))
    if reason == "complete":
        note = (f"covered blocks {from_bn}..{to_bn} in {slices} "
                f"eth_getLogs request(s), {len(logs)} Transfer log(s)")
    elif reason == "window-closed":
        note = (f"eth_getLogs window closed at block {hi}: provider "
                f"refuses even a {width}-block window ({slices} request"
                f"(s)); blocks below {hi} unreachable on this node")
    elif reason == "response-too-big":
        note = (f"eth_getLogs response exceeded ~1MB even at width "
                f"{width}; narrow the window (from_block closer to "
                f"latest) or query a quieter contract")
    else:
        note = (f"stopped after {slices} eth_getLogs request(s) "
                f"(budget cap); blocks {from_bn}..{hi} not fully covered")
    return {
        "logs": logs,
        "window": {
            "from_block": from_bn,
            "to_block": to_bn,
            "requests": slices,
            "widths_used": widths_used,
            "final_width": width,
            "oldest_covered": oldest,
            "newest_covered": newest,
            "stopped_reason": reason,  # complete|window-closed|budget|response-too-big
            "note": note,
        },
    }


def _is_window_error(e: RpcError) -> bool:
    """Does this RpcError mean 'range too wide/old for this node'?

    Matches HTTP 413 (payload too large) plus the usual JSON-RPC /
    HTTP phrasings for query-range and response-size limits. Kind
    rpc-limit (local budget) and timeouts do NOT count.
    """
    if e.kind in ("rpc-limit", "rpc-timeout"):
        return False
    status = getattr(e, "status", None)
    if status in (413,):
        return True
    text = str(e).lower()
    return any(mark in text for mark in (
        "query range", "block range", "too wide", "too many results",
        "response too large", "response size", "1mb", "exceed"))


def reset_rate() -> None:
    """Clear the request-budget ledger (test hook)."""
    with _LOCK:
        _TIMESTAMPS.clear()
