"""Optional MCP server: expose mcplock's offline engine to an LLM.

Everything here operates on *serialized snapshots*, never on a live server:
the model hands mcplock two lockfiles (or one) and gets a classified answer
back. Launching child processes is deliberately left to the CLI, so the MCP
surface stays read-only and safe to expose to an agent.

Install with the extra:  ``uvx --from "mcplock[mcp]" mcplock-mcp``
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

from .diff import diff_snapshots
from .risk import scan as risk_scan
from .snapshot import Snapshot

try:  # pragma: no cover - exercised by CI, which installs the extra
    from mcp.server.fastmcp import FastMCP

    _IMPORT_ERROR: BaseException | None = None
except ImportError as exc:  # pragma: no cover
    _IMPORT_ERROR = exc

    class FastMCP:  # type: ignore[no-redef]
        """Placeholder so the module stays importable (and statically checkable)
        without the extra. Only :meth:`run` fails - at the point where the user
        actually asked for the server."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._tools: list[str] = []

        def tool(self, *a: Any, **kw: Any):
            def wrap(fn):
                if a and callable(a[0]):
                    return a[0]
                return fn

            return wrap

        def run(self, *a: Any, **kw: Any) -> None:
            raise ImportError(
                "the mcplock MCP server needs the 'mcp' package; "
                "install it with: pip install 'mcplock[mcp]'"
            ) from _IMPORT_ERROR


SERVER_NAME = "mcplock"

_INSTRUCTIONS = (
    "mcplock answers questions about MCP server capability surfaces. "
    "Give it snapshots (the JSON files produced by `mcplock snap`) and it "
    "returns classified drift (breaking/risky/new/safe) or a capability-risk "
    "report. It never launches a server process itself."
)

app = FastMCP(SERVER_NAME, instructions=_INSTRUCTIONS)


def _load(text: str) -> Snapshot:
    """Accept either raw JSON or a path to a lockfile."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return Snapshot.from_json(stripped)
    with open(stripped, encoding="utf-8") as fh:
        return Snapshot.from_json(fh.read())


@app.tool(
    name="mcplock_diff",
    description=(
        "Compare two mcplock snapshots and classify every change as "
        "breaking / risky / new / safe. Pass the lockfile JSON text or a path."
    ),
)
def tool_diff(baseline: str, current: str) -> str:
    base = _load(baseline)
    cur = _load(current)
    res = diff_snapshots(base, cur)
    out = {
        "baseline_fingerprint": base.fingerprint,
        "current_fingerprint": cur.fingerprint,
        "counts": res.counts,
        "max_severity": res.max_severity,
        "changes": [c.to_dict() for c in res.changes],
    }
    return json.dumps(out, ensure_ascii=False, indent=2)


@app.tool(
    name="mcplock_risk",
    description=(
        "Scan one mcplock snapshot for risky declared capabilities "
        "(process exec, code eval, file write, network egress, credential "
        "access) and for prompt-injection style wording in tool descriptions."
    ),
)
def tool_risk(snapshot: str) -> str:
    snap = _load(snapshot)
    rep = risk_scan(snap)
    return json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)


@app.tool(
    name="mcplock_describe",
    description=(
        "Summarize one mcplock snapshot: server identity, protocol version, "
        "and the tool/prompt/resource counts with per-tool schema digests."
    ),
)
def tool_describe(snapshot: str) -> str:
    snap = _load(snapshot)
    out: dict[str, Any] = {
        "fingerprint": snap.fingerprint,
        "server": snap.server,
        "protocol_version": snap.protocol_version,
        "capabilities": sorted(snap.capabilities or {}),
        "counts": {
            "tools": len(snap.tools),
            "prompts": len(snap.prompts),
            "resources": len(snap.resources),
            "resource_templates": len(snap.resource_templates),
        },
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "schema_digest": t.schema_digest,
                "required": sorted(t.input_schema.get("required") or []),
                "arguments": sorted((t.input_schema.get("properties") or {}).keys()),
            }
            for t in snap.tools
        ],
        "prompts": [{"name": p.name, "arguments": p.arguments} for p in snap.prompts],
        "resources": [{"uri": r.uri, "name": r.name} for r in snap.resources],
    }
    return json.dumps(out, ensure_ascii=False, indent=2)


@app.tool(
    name="mcplock_schema_template",
    description=(
        "Return an empty mcplock snapshot skeleton, useful when writing "
        "tests or hand-crafting a baseline lockfile."
    ),
)
def tool_schema_template() -> str:
    snap = Snapshot(target={"transport": "stdio", "command": ["<your-server>"]})
    snap.target.pop("runtime", None)
    return snap.to_json()


def main() -> None:
    app.run()


def tool_names() -> list[str]:
    """Names registered through ``@app.tool(name=...)``.

    Read from source rather than from a live FastMCP object, so the contract is
    checkable on a machine where the mcp SDK is not installed.
    """
    module = sys.modules[__name__]
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.append(str(kw.value.value))
    return sorted(names)


if __name__ == "__main__":
    main()
