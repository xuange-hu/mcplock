"""MCP client transports.

Two transports are implemented with the standard library only, so that
``mcplock snap -- npx -y some-server`` works on a machine that has never
installed the ``mcp`` SDK:

* :class:`StdioTransport`  - newline delimited JSON-RPC over a child process.
* :class:`HttpTransport`   - Streamable HTTP / SSE responses over ``urllib``.

The wire format is JSON-RPC 2.0. Every request carries an incrementing id so
responses can be matched without relying on ordering.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION = "2.0"

# Initialize handshake: 2025-06-18 is the newest revision mcplock speaks.
# Servers are expected to answer with the highest version they support.
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mcplock", "version": "0.1.0"}

DEFAULT_TIMEOUT = 20.0


class McpError(RuntimeError):
    """Raised when the server cannot be reached or answers with an error."""


class _IdGen:
    """Monotonic request id source shared by one transport instance."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n


@dataclass
class Transport:
    """Abstract transport. Subclasses implement :meth:`request`."""

    timeout: float = DEFAULT_TIMEOUT
    _idgen: _IdGen = field(default_factory=_IdGen)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Fire-and-forget call (no response expected)."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- convenience wrappers -------------------------------------------------
    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        if not isinstance(result, dict):
            raise McpError("initialize did not return an object")
        # The handshake is only complete once the client acknowledges it.
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        return _items(self.request("tools/list"))

    def list_prompts(self) -> list[dict[str, Any]]:
        return _items(self.request("prompts/list"))

    def list_resources(self) -> list[dict[str, Any]]:
        return _items(self.request("resources/list"))

    def list_resource_templates(self) -> list[dict[str, Any]]:
        return _items(self.request("resources/templates/list"))


def _items(result: Any) -> list[dict[str, Any]]:
    r"""Normalize a ``*/list`` result into a list of dicts.

    A server that does not implement a capability answers with a JSON-RPC
    error (-32601), which surfaces as ``McpError``. Empty results are fine.
    """
    if result is None:
        return []
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        for key in ("tools", "prompts", "resources", "resourceTemplates"):
            value = result.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


class StdioTransport(Transport):
    """JSON-RPC over a child process' stdin/stdout.

    MCP servers occasionally write non-JSON lines (framework banners, logger
    output) to stdout before the handshake. Those lines are skipped instead of
    failing the whole snapshot - a banner is noise, a protocol error is not.
    """

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.env = env
        self.cwd = cwd
        self.stderr_tail: list[str] = []
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        # Lines are read by a background thread into a queue. Reading inline
        # would let readline() block forever on a silent server and the
        # deadline below would never be consulted.
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------
    def _spawn(self) -> subprocess.Popen[str]:
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            # Without this, a Windows child keeps inheriting the parent's
            # console and the pipe never sees EOF on close().
            "bufsize": 1,
        }
        if self.cwd:
            kwargs["cwd"] = self.cwd
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            return subprocess.Popen(self.command, env=env, **kwargs)
        except FileNotFoundError as exc:
            raise McpError(f"command not found: {self.command[0]}") from exc
        except OSError as exc:
            raise McpError(f"cannot start {self.command[0]}: {exc}") from exc

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip("\r\n"))
                if len(self.stderr_tail) > 40:
                    del self.stderr_tail[0]
        except (ValueError, OSError):
            pass

    def _proc_alive(self) -> subprocess.Popen[str]:
        if self._proc is None:
            self._proc = self._spawn()
            assert self._proc.stderr is not None
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(self._proc,), daemon=True
            )
            self._stderr_thread.start()
            self._reader_thread = threading.Thread(
                target=self._drain_stdout, args=(self._proc,), daemon=True
            )
            self._reader_thread.start()
        return self._proc

    def _drain_stdout(self, proc: subprocess.Popen[str]) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._lines.put(line)
        except (ValueError, OSError):
            pass
        finally:
            # Sentinel: unblocks any waiter so it can report the closed pipe.
            self._lines.put("")

    # -- rpc ------------------------------------------------------------------
    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            proc = self._proc_alive()
            assert proc.stdin is not None and proc.stdout is not None
            msg: dict[str, Any] = {
                "jsonrpc": JSONRPC_VERSION,
                "id": self._idgen.next(),
                "method": method,
            }
            if params is not None:
                msg["params"] = params
            payload = json.dumps(msg, ensure_ascii=False) + "\n"
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise McpError(f"server exited while sending {method}: {exc}") from exc
            return self._read_response(proc, msg["id"], method)

    def _read_response(self, proc: subprocess.Popen[str], rid: int, method: str) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"timed out after {self.timeout}s waiting for {method}")
            try:
                line = self._lines.get(timeout=max(0.01, remaining))
            except queue.Empty:
                continue
            if not line:
                raise McpError(
                    f"server closed the connection while waiting for {method}"
                    + (
                        f"; stderr: {self.stderr_tail[-1]}"
                        if self.stderr_tail
                        else ""
                    )
                )
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Not JSON: server banner / log noise. Skip.
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("id") != rid:
                # Notifications or a response to a different request.
                continue
            if "error" in obj:
                err = obj["error"]
                if isinstance(err, dict):
                    raise McpError(
                        f"{method}: [{err.get('code')}] {err.get('message')}"
                    )
                raise McpError(f"{method}: {err}")
            return obj.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._proc is None:
            return
        assert self._proc.stdin is not None
        msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def close(self) -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            assert proc.stdin is not None
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        try:
            assert proc.stdout is not None
            proc.stdout.close()
        except OSError:
            pass
        try:
            assert proc.stderr is not None
            proc.stderr.close()
        except OSError:
            pass


class HttpTransport(Transport):
    """Streamable HTTP transport.

    Handles both response shapes allowed by the spec:
    * ``content-type: application/json`` - a single JSON-RPC reply.
    * ``content-type: text/event-stream`` - SSE frames with ``data:`` lines.

    Session ids returned in the ``mcp-session-id`` header are echoed back on
    subsequent requests, which some servers require.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: str | None = None

    def _post(self, body: dict[str, Any]) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{CLIENT_INFO['name']}/{CLIENT_INFO['version']}",
        }
        headers.update(self.headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                new_sid = resp.headers.get("mcp-session-id")
                if new_sid:
                    self.session_id = new_sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            with contextlib.suppress(OSError):
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise McpError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"cannot reach {self.url}: {exc.reason}") from exc

        if "text/event-stream" in ctype or raw.lstrip().startswith("event:"):
            return self._from_sse(raw)
        return self._from_json(raw)

    @staticmethod
    def _from_json(raw: str) -> Any:
        if not raw.strip():
            return None
        obj = json.loads(raw)
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        if not isinstance(obj, dict):
            raise McpError("unexpected JSON-RPC payload")
        if "error" in obj:
            err = obj["error"]
            raise McpError(f"[{err.get('code')}] {err.get('message')}")
        return obj.get("result")

    @staticmethod
    def _from_sse(raw: str) -> Any:
        """Pick the first JSON-RPC response object out of an SSE stream."""
        for block in raw.split("\n\n"):
            for line in block.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    if "error" in obj:
                        err = obj["error"]
                        raise McpError(f"[{err.get('code')}] {err.get('message')}")
                    return obj.get("result")
        return None

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._idgen.next(),
            "method": method,
        }
        if params is not None:
            body["params"] = params
        return self._post(body)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            body["params"] = params
        # A failed notification must never break a snapshot.
        with contextlib.suppress(McpError):
            self._post(body)


def build_transport(
    command: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Transport:
    """Pick a transport from CLI arguments. Exactly one source is allowed."""
    if bool(command) == bool(url):
        raise ValueError("provide exactly one of --command or --url")
    if command:
        return StdioTransport(command, env=env, cwd=cwd, timeout=timeout)
    assert url is not None
    return HttpTransport(url, headers=headers, timeout=timeout)
