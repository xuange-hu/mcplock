"""Transports: stdio against a real child process, HTTP against a stub.

The stdio tests spawn examples/echo_server.py, which is the same code path a
user hits with ``mcplock snap -- npx -y something``.
"""

from __future__ import annotations

import json
import sys

import pytest

from mcplock.capture import capture
from mcplock.client import (
    HttpTransport,
    McpError,
    StdioTransport,
    _items,
    build_transport,
)


def test_build_transport_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        build_transport()
    with pytest.raises(ValueError, match="exactly one"):
        build_transport(command=["x"], url="http://y")


def test_build_transport_picks_stdio():
    t = build_transport(command=["python"])
    assert isinstance(t, StdioTransport)


def test_build_transport_picks_http():
    t = build_transport(url="http://localhost:1/mcp")
    assert isinstance(t, HttpTransport)


def test_stdio_rejects_empty_command():
    with pytest.raises(ValueError):
        StdioTransport([])


# -- real child process -------------------------------------------------------

@pytest.mark.parametrize("mutated", [False, True])
def test_stdio_snapshot_against_echo_server(python_exe, echo_server, mutated):
    args = [python_exe, str(echo_server)] + (["--mutated"] if mutated else [])
    t = StdioTransport(args, timeout=30)
    try:
        snap = capture(t, {"transport": "stdio", "command": args})
    finally:
        t.close()
    assert snap.server["name"] == "echo-server"
    assert snap.protocol_version == "2025-06-18"
    names = [x.name for x in snap.tools]
    assert "read_file" in names and "run_command" in names
    assert (len(names) == 4) is mutated
    assert snap.prompts and snap.prompts[0].name == "code_review"
    assert snap.resources and snap.resources[0].uri == "file:///project/README.md"


def test_stdio_handshake_is_repeatable_in_one_process(python_exe, echo_server):
    t = StdioTransport([python_exe, str(echo_server)], timeout=30)
    try:
        first = capture(t, {})
        second = capture(t, {})
    finally:
        t.close()
    assert first.surface_digest == second.surface_digest


def test_stdio_unknown_command_raises_mcp_error():
    t = StdioTransport(["definitely-not-a-real-binary-xyz"], timeout=10)
    try:
        with pytest.raises(McpError, match="command not found|cannot start"):
            t.initialize()
    finally:
        t.close()


def test_stdio_skips_non_json_banner_lines(python_exe, tmp_path):
    """Servers that print a banner on stdout must not break the handshake."""
    script = tmp_path / "banner_server.py"
    script.write_text(
        "import sys, json\n"
        "print('starting up...', flush=True)\n"
        "for line in sys.stdin:\n"
        "    if not line.strip():\n"
        "        continue\n"
        "    r = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],"
        "'result':{'protocolVersion':'2025-06-18','serverInfo':{'name':'b','version':'1'},"
        "'capabilities':{}}}), flush=True)\n",
        encoding="utf-8",
    )
    t = StdioTransport([python_exe, str(script)], timeout=30)
    try:
        init = t.initialize()
    finally:
        t.close()
    assert init["serverInfo"]["name"] == "b"


def test_stdio_jsonrpc_error_becomes_mcp_error(python_exe, tmp_path):
    script = tmp_path / "err_server.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    if not line.strip():\n"
        "        continue\n"
        "    r = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],"
        "'error':{'code':-32601,'message':'Method not found'}}), flush=True)\n",
        encoding="utf-8",
    )
    t = StdioTransport([python_exe, str(script)], timeout=30)
    try:
        with pytest.raises(McpError, match=r"\[-32601\]"):
            t.initialize()
    finally:
        t.close()


def test_stdio_timeout_is_enforced(python_exe, tmp_path):
    script = tmp_path / "quiet_server.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    t = StdioTransport([python_exe, str(script)], timeout=1.0)
    try:
        with pytest.raises(McpError, match="timed out"):
            t.initialize()
    finally:
        t.close()


def test_capture_degrades_when_capability_missing(python_exe, tmp_path):
    """A server without prompts/resources must still produce a snapshot."""
    script = tmp_path / "tools_only.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    if not line.strip():\n"
        "        continue\n"
        "    r = json.loads(line)\n"
        "    m = r.get('method')\n"
        "    if m == 'initialize':\n"
        "        res = {'protocolVersion':'2025-06-18','serverInfo':{'name':'t','version':'1'},"
        "'capabilities':{'tools':{}}}\n"
        "    elif m == 'tools/list':\n"
        "        res = {'tools':[{'name':'a','description':'d','inputSchema':{'type':'object'}}]}\n"
        "    elif m is not None and m.startswith('notifications/'):\n"
        "        continue\n"
        "    else:\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':r['id'],"
        "'error':{'code':-32601,'message':'Method not found'}}), flush=True)\n"
        "        continue\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':res}), flush=True)\n",
        encoding="utf-8",
    )
    t = StdioTransport([python_exe, str(script)], timeout=30)
    try:
        snap = capture(t, {})
    finally:
        t.close()
    assert len(snap.tools) == 1
    assert snap.prompts == []
    assert snap.resources == []


def test_capture_records_runtime_metadata(python_exe, echo_server):
    t = StdioTransport([python_exe, str(echo_server)], timeout=30)
    try:
        snap = capture(t, {"transport": "stdio"})
    finally:
        t.close()
    assert "runtime" in snap.target
    assert snap.target["runtime"]["python"]


# -- HTTP transport -----------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: str, headers=None, ctype="application/json"):
        self._payload = payload.encode()
        self.headers = {"Content-Type": ctype}
        self.headers.update(headers or {})

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, responses):
    calls = []

    default = json.dumps({"jsonrpc": "2.0", "id": 0, "result": {}})

    def fake(req, timeout=None):
        calls.append(req)
        # initialize() sends a request AND an initialized notification, so a
        # test that only queues one reply must not blow up on the second call.
        return responses.pop(0) if responses else _FakeResp(default)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def test_http_json_response(monkeypatch):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"protocolVersion": "2025-06-18",
                                  "serverInfo": {"name": "h", "version": "2"},
                                  "capabilities": {}}})
    _patch_urlopen(monkeypatch, [_FakeResp(body)])
    t = HttpTransport("http://x/mcp")
    init = t.initialize()
    assert init["serverInfo"]["name"] == "h"


def test_http_sse_response(monkeypatch):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "a"}]}})
    sse = f"event: message\ndata: {payload}\n\n"
    _patch_urlopen(monkeypatch, [_FakeResp(sse, ctype="text/event-stream")])
    t = HttpTransport("http://x/mcp")
    assert t.list_tools() == [{"name": "a"}]


def test_http_session_id_is_echoed(monkeypatch):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
    calls = _patch_urlopen(
        monkeypatch,
        [_FakeResp(body, headers={"mcp-session-id": "abc123"}), _FakeResp(body)],
    )
    t = HttpTransport("http://x/mcp")
    t.initialize()
    t.list_tools()
    assert t.session_id == "abc123"
    # urllib capitalizes header keys at construction time but get_header()
    # does not, so look the capitalized key up directly.
    assert calls[-1].headers.get("Mcp-session-id") == "abc123"
    assert calls[0].headers.get("Mcp-session-id") is None


def test_http_error_status_becomes_mcp_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    t = HttpTransport("http://x/mcp")
    with pytest.raises(McpError, match="HTTP 500"):
        t.initialize()


def test_http_unreachable_becomes_mcp_error(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    t = HttpTransport("http://x/mcp")
    with pytest.raises(McpError, match="cannot reach"):
        t.initialize()


def test_http_notify_swallows_errors(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    HttpTransport("http://x/mcp").notify("notifications/initialized")


# -- normalization helpers ----------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected_len",
    [
        (None, 0),
        ([], 0),
        ({"tools": [{"name": "a"}]}, 1),
        ({"prompts": [{"name": "p"}]}, 1),
        ({"resources": [{"uri": "u"}]}, 1),
        ({"resourceTemplates": [{"uri": "u"}]}, 1),
        ([{"name": "a"}, "junk"], 1),
        ("not-a-list", 0),
    ],
)
def test_items_normalization(raw, expected_len):
    assert len(_items(raw)) == expected_len


def test_notify_before_start_is_noop():
    StdioTransport(["python"]).notify("notifications/initialized")


def test_close_is_idempotent():
    t = StdioTransport([sys.executable, "-c", "pass"], timeout=10)
    t.close()
    t.close()
