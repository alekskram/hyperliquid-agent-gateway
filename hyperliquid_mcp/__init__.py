"""hyperliquid_mcp - read-only, keyless MCP gateway to Hyperliquid public data.

Package layout:
    info.py    stdlib-only POST client for https://api.hyperliquid.xyz/info
    evm.py     stdlib-only JSON-RPC client for https://rpc.hyperliquid.xyz/evm
    server.py  FastMCP server wiring the 12 read-only tools

No private keys, no signing, no order placement - public data only.
"""

__version__ = "0.1.2"
