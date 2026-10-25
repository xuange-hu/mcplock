"""Turn a live transport into a :class:`Snapshot`."""

from __future__ import annotations

import sys
from typing import Any

from .client import McpError, Transport
from .snapshot import Prompt, Resource, Snapshot, Tool, runtime_context


def capture(
    transport: Transport,
    target: dict[str, Any],
    include_prompts: bool = True,
    include_resources: bool = True,
) -> Snapshot:
    """Run the handshake and collect the declared surface.

    Capability negotiation is best-effort: a server that omits
    ``prompts/list`` answers with JSON-RPC method-not-found, which is
    translated into an empty list instead of aborting the snapshot. Otherwise
    one unimplemented capability would make the tool useless on the very
    servers people actually run.
    """
    init = transport.initialize()

    server: dict[str, Any] = {}
    server_info = init.get("serverInfo")
    if isinstance(server_info, dict):
        server = {
            "name": server_info.get("name"),
            "version": server_info.get("version"),
            "title": server_info.get("title"),
        }
    capabilities = init.get("capabilities")
    protocol_version = init.get("protocolVersion")

    tools = [Tool.from_server(t) for t in _safe(transport.list_tools, "tools/list")]

    prompts: list[Prompt] = []
    if include_prompts:
        prompts = [
            Prompt.from_server(p) for p in _safe(transport.list_prompts, "prompts/list")
        ]

    resources: list[Resource] = []
    templates: list[Resource] = []
    if include_resources:
        resources = [
            Resource.from_server(r)
            for r in _safe(transport.list_resources, "resources/list")
        ]
        templates = [
            Resource.from_server(r)
            for r in _safe(
                transport.list_resource_templates, "resources/templates/list"
            )
        ]

    snap = Snapshot(
        target=dict(target),
        server=server,
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        protocol_version=protocol_version if isinstance(protocol_version, str) else None,
        tools=tools,
        prompts=prompts,
        resources=resources,
        resource_templates=templates,
    )
    # Runtime metadata is recorded but excluded from surface_digest, so the
    # digest stays comparable across machines.
    snap.target.setdefault("runtime", runtime_context())
    return snap


def _safe(fn, label: str) -> list[dict[str, Any]]:
    """Call a list-capability and degrade gracefully on 'not implemented'."""
    try:
        return fn()
    except McpError as exc:
        msg = str(exc)
        if "-32601" in msg or "not found" in msg.lower():
            return []
        print(f"warning: {label}: {msg}", file=sys.stderr)
        return []
    except Exception as exc:  # noqa: BLE001 - one bad capability must not kill a snapshot
        print(f"warning: {label}: {exc}", file=sys.stderr)
        return []
