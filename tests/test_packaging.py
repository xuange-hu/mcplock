"""Packaging contract.

CI installs ``.[all]`` so it can never catch a console script whose third-party
import is only satisfied by an optional extra. These tests read the source
instead of importing it, which is the only way to fail fast on that class of
bug (it already bit two sibling projects).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mcplock"
PYPROJECT = ROOT / "pyproject.toml"

THIRD_PARTY = {
    "mcp",
    "click",
    "rich",
    "httpx",
    "requests",
    "pydantic",
    "yaml",
    "typer",
}


def _scripts() -> dict[str, str]:
    """Parse [project.scripts] without a TOML dependency."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("[project.scripts]", 1)[1].split("[", 1)[0]
    out = {}
    for line in block.splitlines():
        if "=" not in line:
            continue
        name, target = line.split("=", 1)
        out[name.strip()] = target.strip().strip('"')
    return out


def _extras() -> dict[str, set[str]]:
    text = PYPROJECT.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("[project.optional-dependencies]"):
            current = None
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*\[(.*)\]\s*$", line)
        if m and current != "__base__":
            out[m.group(1)] = set(re.findall(r'"([^"]+)"', m.group(2)))
    return out


def _module_imports(mod: str) -> set[str]:
    path = SRC / f"{mod}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("name,target", sorted(_scripts().items()))
def test_console_script_target_exists(name, target):
    module = target.split(":")[0]
    assert (ROOT / "src" / (module.replace(".", "/") + ".py")).exists()
    assert hasattr(__import__(module, fromlist=["x"]), target.split(":")[1])


@pytest.mark.parametrize("name,target", sorted(_scripts().items()))
def test_console_script_dependencies_are_declared(name, target):
    """Third-party imports at module scope must be covered by base deps or by
    an extra - otherwise the installed entry point crashes on launch."""
    module = target.split(":")[0]
    short = module.split(".")[-1]
    imports = _module_imports(short) & THIRD_PARTY
    if not imports:
        return
    covered = set()
    text = PYPROJECT.read_text(encoding="utf-8")
    base = text.split("[project]", 1)[1].split("[project.optional-dependencies]", 1)[0]
    covered |= set(re.findall(r'"([^"]+)"', base.split("dependencies = [")[1].split("]")[0]))
    for dep in _extras().values():
        covered |= {re.split(r"[<>=!\[]", d)[0] for d in dep}
    missing = {i for i in imports if i not in covered}
    assert not missing, f"{name} imports {missing} but they are not declared"


def test_core_packages_import_without_third_party():
    """The CLI must work with zero installed dependencies."""
    for mod in ("snapshot", "diff", "risk", "sarif", "report", "cli", "client", "capture"):
        assert not (_module_imports(mod) & THIRD_PARTY), f"{mod} pulls a third-party import"


def test_pyproject_declares_no_base_dependencies():
    text = PYPROJECT.read_text(encoding="utf-8")
    base = text.split("[project]", 1)[1].split("[project.optional-dependencies]", 1)[0]
    assert "dependencies = []" in base


def test_version_matches_package():
    text = PYPROJECT.read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    from mcplock import __version__

    assert declared == __version__


def test_mcp_extra_pins_below_v2():
    """mcp 2.x renames FastMCP to MCPServer and moves it - stay on 1.x."""
    extras = _extras()
    assert "mcp" in extras
    dep = next(d for d in extras["mcp"] if d.startswith("mcp"))
    assert ">=1.2" in dep and ",<2" in dep


def test_readme_exists_and_has_quickstart():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "mcplock snap" in readme
    assert "pip install mcplock" in readme or "uvx mcplock" in readme


def test_examples_are_parseable_lockfiles():
    import json

    for name in ("baseline.json", "drifted.json"):
        data = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
        assert data["schema"] == "mcplock-snapshot"


def test_examples_differ_in_every_severity_class():
    from mcplock.diff import diff_snapshots
    from mcplock.snapshot import Snapshot

    base = Snapshot.from_json((ROOT / "examples" / "baseline.json").read_text(encoding="utf-8"))
    cur = Snapshot.from_json((ROOT / "examples" / "drifted.json").read_text(encoding="utf-8"))
    res = diff_snapshots(base, cur)
    assert {c.severity for c in res.changes} == {"breaking", "risky", "new", "safe"}


def test_python_supported_on_this_interpreter():
    assert sys.version_info >= (3, 9)


EXPECTED_MCP_TOOLS = [
    "mcplock_describe",
    "mcplock_diff",
    "mcplock_risk",
    "mcplock_schema_template",
]


def test_mcp_tools_are_registered():
    """Counts the @app.tool(name=...) decorators statically, so the check runs
    even where the mcp SDK is not installed."""
    from mcplock import mcp_server

    assert mcp_server.tool_names() == EXPECTED_MCP_TOOLS


def test_mcp_tool_functions_are_callable_offline():
    """The offline tools take serialized snapshots, so they must work with no
    SDK and no server process."""
    from mcplock import mcp_server

    empty = mcp_server.tool_schema_template()
    report = json.loads(mcp_server.tool_risk(empty))
    assert report["risk_score"] == 0
    described = json.loads(mcp_server.tool_describe(empty))
    assert described["counts"]["tools"] == 0


def test_mcp_server_imports_without_the_sdk():
    """The fallback FastMCP must keep the module importable; only run() fails."""
    import mcplock.mcp_server as m

    assert callable(m.main)
