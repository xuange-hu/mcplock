"""mcplock - capability lockfile and drift detection for MCP servers.

Core (``snapshot`` / ``diff`` / ``risk`` / ``cli``) is standard library only,
so the CLI runs anywhere Python does and never competes with the server's own
dependencies. The optional MCP server lives in :mod:`mcplock.mcp_server` and
pulls in the ``mcp`` SDK through the ``[mcp]`` extra.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
