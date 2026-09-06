"""Unit tests for hyperliquid_mcp.evm - 100% offline. Covers the
100/min request budget, RpcError taxonomy, adaptive get_logs window
(halve on too-big, double on quiet, floor/cap), _pad_address, and
_to_int parsing."""
import json
import urllib.error
import urllib.request

import pytest

from hyperliquid_mcp import evm


def _rpc_result(method, result):
    return {"jsonrpc": "2.0", "id": 1, "method": method,
            "result": result}


class TestRateBudget:
    def test_admit_counts_requests(self, monkeypatch):
        evm.reset_rate()
        monkeypatch.setattr(evm.time, "monotonic", lambda: 1000.0)
        with evm._LOCK:
            evm._TIMESTAMPS.extend([990.0 + i for i in range(5)])
        assert evm.requests_last_minute() == 5
        evm.reset_rate()

    def test_limit_raises_rpc_limit(self, monkeypatch):
        evm.reset_rate()
        clock = iter([])  # unused
        monkeypatch.setattr(evm.time, "monotonic", lambda: 1000.0)
        with evm._LOCK:
            evm._TIMESTAMPS.clear()
            evm._TIMESTAMPS.extend([1000.0 - 1] * evm.RATE_LIMIT)
        with pytest.raises(evm.RpcError) as e:
            evm.post("eth_blockNumber")
        assert e.value.kind == "rpc-limit"
        assert "100" in str(e.value)
        evm.reset_rate()

    def test_budget_rolls_off_after_60s(self, monkeypatch):
        evm.reset_rate()
        monkeypatch.setattr(evm.time, "monotonic", lambda: 2000.0)
        with evm._LOCK:
            evm._TIMESTAMPS.clear()
            evm._TIMESTAMPS.extend([1930.0] * evm.RATE_LIMIT)  # old
        # pruned on read; posting must not raise rpc-limit
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _resp(_rpc_result(
                "eth_blockNumber", "0x1234")))
        assert evm.block_number() == 0x1234
        evm.reset_rate()

    def test_info_and_evm_budgets_are_separate(self):
        from hyperliquid_mcp import info
        assert info.WEIGHT_LIMIT == 1200
        assert evm.RATE_LIMIT == 100
        assert info._SPEND is not evm._TIMESTAMPS


class TestErrors:
    def test_http_500_final_raises_rpc_http(self, monkeypatch):
        evm.reset_rate()

        def always_500(req, timeout=None):
            raise _http_error(req.full_url, 500, "Server Error")

        monkeypatch.setattr(urllib.request, "urlopen", always_500)
        monkeypatch.setattr(evm.time, "sleep", lambda s: None)
        with pytest.raises(evm.RpcError) as e:
            evm.post("eth_blockNumber")
        assert e.value.kind == "rpc-http"
        assert e.value.status == 500
        evm.reset_rate()

    def test_jsonrpc_error_payload_raises_rpc_http(self, monkeypatch):
        evm.reset_rate()
        payload = {"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000, "message": "query range too wide"}}

        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _resp(payload))
        with pytest.raises(evm.RpcError, match="query range"):
            evm.post("eth_getLogs", [{}])
        evm.reset_rate()

    def test_timeout_not_retried(self, monkeypatch):
        evm.reset_rate()
        calls = {"n": 0}

        def slow(req, timeout=None):
            calls["n"] += 1
            raise TimeoutError("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", slow)
        with pytest.raises(evm.RpcError) as e:
            evm.post("eth_blockNumber")
        assert e.value.kind == "rpc-timeout"
        assert calls["n"] == 1
        evm.reset_rate()

    def test_network_unreachable_after_retries(self, monkeypatch):
        evm.reset_rate()

        def down(req, timeout=None):
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(urllib.request, "urlopen", down)
        monkeypatch.setattr(evm.time, "sleep", lambda s: None)
        with pytest.raises(evm.RpcError) as e:
            evm.post("eth_blockNumber")
        assert e.value.kind == "rpc-network"
        evm.reset_rate()


class TestScalars:
    def test_to_int_hex_and_decimal(self):
        assert evm._to_int("0x3e8") == 1000
        assert evm._to_int("1000") == 1000
        assert evm._to_int(1000) == 1000

    def test_to_int_rejects_garbage(self):
        for bad in ("latest", "0xzz", "", None, True):
            with pytest.raises(ValueError):
                evm._to_int(bad)

    def test_block_param_tags(self):
        assert evm._block_param("latest") == "latest"
        assert evm._block_param(5) == "0x5"

    def test_pad_address(self):
        addr = "0xAbC0000000000000000000000000000000000012"
        padded = evm._pad_address(addr)
        assert padded == "0" * 24 + addr[2:].lower()
        assert len(padded) == 64
        with pytest.raises(ValueError):
            evm._pad_address("0x1234")

    def test_chain_id_999(self, monkeypatch):
        evm.reset_rate()
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _resp(_rpc_result(
                "eth_chainId", "0x3e7")))
        assert evm.chain_id() == 999
        evm.reset_rate()


class TestCalls:
    def test_native_balance(self, monkeypatch):
        evm.reset_rate()
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _resp(_rpc_result(
                "eth_getBalance", "0x1bc16d674ec80000")))  # 2e18
        assert evm.native_balance("0x" + "ab" * 20) == 2 * 10**18
        evm.reset_rate()

    def test_balance_of_calldata(self, monkeypatch):
        evm.reset_rate()
        seen = []

        def fake(req, timeout=None):
            body = json.loads(req.data.decode())
            seen.append(body["params"][0])
            return _resp(_rpc_result("eth_call", "0x0"))

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        addr = "0x" + "cd" * 20
        evm.balance_of("0x" + "ee" * 20, addr)
        assert seen[0]["data"] == evm.BALANCE_OF_SELECTOR + "0" * 24 + \
            addr[2:]
        assert seen[0]["to"] == "0x" + "ee" * 20
        evm.reset_rate()


class TestAdaptiveWindow:
    """The get_logs walk: 30-block start, halve on too-big, double on
    quiet (cap 60), floor 2, budget 14 slices."""

    def _mock_logs(self, monkeypatch, responder, clock_now=1_000_000.0):
        evm.reset_rate()
        monkeypatch.setattr(evm.time, "monotonic", lambda: clock_now)
        monkeypatch.setattr(urllib.request, "urlopen", responder)
        # block_number via the same responder
        return lambda: evm.get_logs(
            "0x" + "aa" * 20, 100, 250)

    def test_quiet_contract_grows_window(self, monkeypatch):
        widths = []

        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(250)))
            rng = body["params"][0]
            widths.append(int(rng["toBlock"], 16) -
                          int(rng["fromBlock"], 16) + 1)
            return _resp(_rpc_result("eth_getLogs", []))  # tiny payload

        monkeypatch.setattr(urllib.request, "urlopen", responder)
        monkeypatch.setattr(evm.time, "monotonic", lambda: 1_000_000.0)
        evm.reset_rate()
        out = evm.get_logs("0x" + "aa" * 20, 0, 250)
        assert widths[0] == 30            # start
        assert widths[1] == 60            # doubled, capped
        assert widths[2] == 60            # stays at cap
        # last slice may be truncated by the from_block floor - fine
        assert out["window"]["stopped_reason"] == "complete"
        assert out["window"]["oldest_covered"] == 0
        evm.reset_rate()

    def test_busy_contract_shrinks_window(self, monkeypatch):
        widths = []
        big = [{"topics": ["0x" + "00" * 32], "data": "0x" + "ff" * 32,
                "blockNumber": "0x1", "transactionHash": "0x" + "11" * 32,
                "logIndex": "0x0"}] * 400  # > 1MB when serialized

        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(250)))
            rng = body["params"][0]
            widths.append(int(rng["toBlock"], 16) -
                          int(rng["fromBlock"], 16) + 1)
            return _resp(_rpc_result("eth_getLogs", big))

        monkeypatch.setattr(evm, "_RESPONSE_TOO_BIG", 1000)  # tiny cap
        get = self._mock_logs(monkeypatch, responder)
        out = get()
        assert widths[0] == 30
        assert widths[1] == 15   # halved
        assert widths[2] == 7    # halved again
        assert widths[3] == 3    # halved again
        assert widths[4] == 2    # floor
        assert out["window"]["stopped_reason"] == "response-too-big"
        evm.reset_rate()

    def test_rpc_error_halves_then_raises_or_stops(self, monkeypatch):
        widths = []

        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(250)))
            rng = body["params"][0]
            widths.append(int(rng["toBlock"], 16) -
                          int(rng["fromBlock"], 16) + 1)
            # every getLogs fails with a range error
            raise _http_error(req.full_url, 400, "query range too wide")

        monkeypatch.setattr(evm.time, "sleep", lambda s: None)
        get = self._mock_logs(monkeypatch, responder)
        out = get()
        assert widths == [30, 15, 7, 3, 2]
        assert out["window"]["stopped_reason"] == "window-closed"
        evm.reset_rate()

    def test_rpc_limit_not_treated_as_window_error(self, monkeypatch):
        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(250)))
            raise evm.RpcError("local budget", kind="rpc-limit")

        get = self._mock_logs(monkeypatch, responder)
        with pytest.raises(evm.RpcError) as e:
            get()
        assert e.value.kind == "rpc-limit"
        evm.reset_rate()

    def test_budget_cap_on_many_slices(self, monkeypatch):
        calls = {"n": 0}

        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(10**6)))
            calls["n"] += 1
            return _resp(_rpc_result("eth_getLogs", []))

        monkeypatch.setattr(evm.time, "monotonic", lambda: 1_000_000.0)
        monkeypatch.setattr(urllib.request, "urlopen", responder)
        out = evm.get_logs("0x" + "aa" * 20, 0, 10**6)
        assert calls["n"] == evm._MAX_SLICES
        assert out["window"]["stopped_reason"] == "budget"
        evm.reset_rate()

    def test_transfer_topic_constant(self):
        assert evm.TRANSFER_TOPIC.startswith("0xddf252ad1be2c89b")
        assert len(evm.TRANSFER_TOPIC) == 66

    def test_logs_sorted_and_removed_dropped(self, monkeypatch):
        logs = [
            {"topics": [], "data": "0x1", "blockNumber": "0x2",
             "transactionHash": "0xb", "logIndex": "0x0"},
            {"topics": [], "data": "0x2", "blockNumber": "0x1",
             "transactionHash": "0xa", "logIndex": "0x1"},
            {"topics": [], "data": "0x3", "blockNumber": "0x1",
             "transactionHash": "0xa", "logIndex": "0x0",
             "removed": True},
        ]

        def responder(req, timeout=None):
            body = json.loads(req.data.decode())
            if body["method"] == "eth_blockNumber":
                return _resp(_rpc_result("eth_blockNumber", hex(2)))
            return _resp(_rpc_result("eth_getLogs", logs))

        monkeypatch.setattr(urllib.request, "urlopen", responder)
        out = evm.get_logs("0x" + "aa" * 20, 1, 2)
        assert [l["blockNumber"] for l in out["logs"]] == ["0x1", "0x2"]
        assert all(not l.get("removed") for l in out["logs"])
        evm.reset_rate()


def _http_error(url, code, msg):
    import email.message
    return urllib.error.HTTPError(
        url, code, msg, email.message.Message(), None)


class _resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
