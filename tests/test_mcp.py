"""MCP surface: the served names must match tool_names(), with descriptions."""

from __future__ import annotations

import asyncio

import pytest

from mcplock import mcp_server

pytest.importorskip("mcp")


def test_served_tools_match_tool_names_with_descriptions():
    """The names an agent calls must be the names tool_names() promises, and
    FastMCP serves an empty description when the registered function has no
    docstring - an unidentified tool is an uncallable tool."""
    tools = asyncio.run(mcp_server.app.list_tools())
    served = {t.name for t in tools}
    manifest = set(mcp_server.tool_names())
    assert served == manifest, f"only served: {served - manifest}; only manifest: {manifest - served}"
    for t in tools:
        assert (t.description or "").strip(), f"{t.name} has no description"


def test_server_stays_read_only():
    """The docstring promises the MCP surface never launches a process; the
    tool list is the place that promise is pinned."""
    names = set(mcp_server.tool_names())
    assert not any("run" in n or "exec" in n or "spawn" in n for n in names)
