"""V0.1 tool implementations. Each module exposes a pure-ish async
``*_impl`` function (DB/HTTP calls only, no MCP-protocol concerns) that
``arie_mcp.server`` wraps with ``@mcp.tool()`` and ``envelope.run_tool``."""

from __future__ import annotations
