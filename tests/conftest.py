import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcplock.snapshot import Snapshot, Tool  # noqa: E402

EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="session")
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture(scope="session")
def baseline_path() -> Path:
    return EXAMPLES / "baseline.json"


@pytest.fixture(scope="session")
def drifted_path() -> Path:
    return EXAMPLES / "drifted.json"


@pytest.fixture(scope="session")
def echo_server() -> Path:
    return EXAMPLES / "echo_server.py"


@pytest.fixture(scope="session")
def python_exe() -> str:
    return sys.executable


def tool(name, description="", props=None, required=None, annotations=None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return Tool(
        name=name,
        title=None,
        description=description,
        input_schema=schema,
        output_schema=None,
        annotations=annotations or {},
    )


def snapshot(tools=None, prompts=None, resources=None, caps=None, server=None,
             protocol="2025-06-18"):
    return Snapshot(
        target={"transport": "stdio", "command": ["svc"]},
        server=server or {"name": "svc", "version": "1.0.0"},
        capabilities=caps if caps is not None else {"tools": {}},
        protocol_version=protocol,
        tools=list(tools or []),
        prompts=list(prompts or []),
        resources=list(resources or []),
        resource_templates=[],
    )


def roundtrip(snap: Snapshot) -> Snapshot:
    """Serialize + parse, the way a lockfile on disk would come back."""
    return Snapshot.from_json(json.dumps(snap.to_dict()))
