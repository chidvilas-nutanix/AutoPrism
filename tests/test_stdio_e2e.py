"""End-to-end test driving the MCP server over real stdio.

Spawns the ``prism-mcp`` console script as a subprocess and speaks
JSON-RPC 2.0 to it over stdin/stdout, exactly the way Cursor (or any
other MCP client) does in production. Everything below the protocol
line is exercised: process startup, FastMCP lifespan, refresh-loop
cold start, cache fallback when the registry is unreachable, tool
dispatch, and JSON framing.

The test deliberately avoids hitting the real Artifactory:

* ``PRISM_MCP_REGISTRY_URL`` is pointed at a closed loopback port so
  the cold-start refresh fails fast with ``ConnectionRefused``.
* The cache is pre-seeded with the synthetic tarball used by every
  other test, so the offline-fallback path in ``Library`` succeeds
  and the tool calls have real entities to return.
* No JFROG credentials are required for tests; the server logs a
  "no credentials configured" warning and continues — exactly the
  Slice 8 cold-start-with-cache code path.

PRD §8 lists this as "E2E / acceptance: spawn the MCP server over
stdio in a subprocess". This is the realization of that requirement.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from prism_mcp.cache import Cache
from tests.conftest import make_prism_tarball

VERSION = "2.54.0"
PACKAGE = "@nutanix-ui/prism-reactjs"
INITIALIZE_TIMEOUT_S = 15.0
TOOL_CALL_TIMEOUT_S = 15.0
SHUTDOWN_TIMEOUT_S = 5.0

# A loopback port that should always refuse SYNs in test environments,
# giving us a sub-millisecond ConnectionRefused so the refresh loop
# never blocks the test.
UNREACHABLE_REGISTRY = "http://127.0.0.1:1/"

JsonValue = Any


class StdioClient:
    """JSON-RPC 2.0 client over a subprocess's stdin/stdout.

    Mirrors what Cursor does on the wire: one JSON object per line,
    requests carry an ``id``, notifications don't. A background
    thread drains stderr into memory so the subprocess never blocks
    on a full pipe; the captured text is exposed via :meth:`stderr`
    for use in failure messages.

    Args:
        process (subprocess.Popen[str]): a running ``prism-mcp``
            subprocess opened with ``stdin``/``stdout``/``stderr``
            as pipes and ``text=True``.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._next_id = 1
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._readers_done = threading.Event()

        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _drain_stdout(self) -> None:
        """Push every stdout line onto the queue, sentinel on EOF."""
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._stdout_queue.put(line)
        finally:
            self._stdout_queue.put("")

    def _drain_stderr(self) -> None:
        """Capture stderr so the pipe never fills and dump on failure."""
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr_lines.append(line)
        finally:
            self._readers_done.set()

    def stderr(self) -> str:
        """Return all captured stderr so far (joined)."""
        return "".join(self._stderr_lines)

    def _write(self, payload: dict[str, JsonValue]) -> None:
        """Write a single JSON-RPC message followed by ``\\n``."""
        assert self._process.stdin is not None
        line = json.dumps(payload) + "\n"
        self._process.stdin.write(line)
        self._process.stdin.flush()

    def _read_response(
        self, request_id: int, timeout: float
    ) -> dict[str, JsonValue]:
        """Read lines until we find a response matching ``request_id``.

        Args:
            request_id (int): the ``id`` we sent on the request.
            timeout (float): wall-clock seconds before we give up and
                fail the test.

        Returns:
            dict: parsed JSON-RPC response object.

        Raises:
            AssertionError: on timeout, EOF, or a non-JSON line.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for id={request_id}; stderr "
                    f"so far:\n{self.stderr()}"
                )
            try:
                raw = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise AssertionError(
                    f"timed out waiting for id={request_id}; stderr "
                    f"so far:\n{self.stderr()}"
                ) from exc
            if raw == "":
                raise AssertionError(
                    "server stdout closed before id="
                    f"{request_id} arrived; stderr:\n{self.stderr()}"
                )
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"non-JSON line on stdout: {raw!r}; stderr:\n"
                    f"{self.stderr()}"
                ) from exc
            if message.get("id") == request_id:
                return message
            # Otherwise it's a notification or a response to a different
            # in-flight request; we ignore it. The MCP server only emits
            # responses we ask for in this test, so this branch is rare.

    def request(
        self,
        method: str,
        params: dict[str, JsonValue] | None = None,
        timeout: float = TOOL_CALL_TIMEOUT_S,
    ) -> dict[str, JsonValue]:
        """Send a request and return the matching response object."""
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, JsonValue] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._read_response(request_id, timeout=timeout)

    def notify(self, method: str) -> None:
        """Send a notification (no ``id``, no response expected)."""
        self._write({"jsonrpc": "2.0", "method": method})


@contextmanager
def _spawn_server(env: dict[str, str]) -> Iterator[StdioClient]:
    """Spawn ``prism-mcp`` and yield a :class:`StdioClient`."""
    cmd = [sys.executable, "-m", "prism_mcp.server"]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    client = StdioClient(process)
    try:
        yield client
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.terminate()
            process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=SHUTDOWN_TIMEOUT_S)


@pytest.fixture()
def seeded_cache(tmp_path: Path) -> Path:
    """Pre-seed a temp cache with the synthetic tarball.

    Returns the cache root path; the layout is the same
    ``<root>/<version>/package/...`` the real server expects.
    """
    cache_root = tmp_path / "prism-mcp-cache"
    cache = Cache(cache_root)
    tarball = make_prism_tarball(version=VERSION)
    cache.install_tarball(VERSION, tarball)
    return cache_root


def _server_env(cache_root: Path) -> dict[str, str]:
    """Return a clean env that points the server at our temp cache.

    We start from the parent env so ``PATH`` / ``HOME`` / locale flow
    through (otherwise ``Path.home()`` resolution and console-script
    discovery break on some systems), but strip every JFROG_*
    credential the test runner might have exported. The Slice 8
    cold-start cache fallback is what we want to exercise — accidentally
    inheriting real creds would route us through the live Artifactory
    path instead.
    """
    env = dict(os.environ)
    for key in ("JFROG_AUTH", "JFROG_EMAIL", "JFROG_API_KEY"):
        env.pop(key, None)

    env["PRISM_MCP_CACHE_DIR"] = str(cache_root)
    env["PRISM_MCP_REGISTRY_URL"] = UNREACHABLE_REGISTRY
    env["PRISM_MCP_PACKAGE_NAME"] = PACKAGE
    return env


def _initialize(client: StdioClient) -> dict[str, JsonValue]:
    """Drive the MCP initialize handshake."""
    response = client.request(
        "initialize",
        params={
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "stdio-e2e-test", "version": "0.0"},
        },
        timeout=INITIALIZE_TIMEOUT_S,
    )
    client.notify("notifications/initialized")
    return response


def test_stdio_initialize_and_list_tools(seeded_cache: Path) -> None:
    """The handshake succeeds and ``tools/list`` returns the v1 surface."""
    env = _server_env(seeded_cache)

    with _spawn_server(env) as client:
        init_response = _initialize(client)

        assert init_response["jsonrpc"] == "2.0"
        assert "result" in init_response, (
            f"initialize failed; stderr:\n{client.stderr()}"
        )
        assert init_response["result"]["serverInfo"]["name"] == "prism-mcp"

        tools_response = client.request("tools/list")

    assert "result" in tools_response
    tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
    assert tool_names == {
        "echo",
        "get_library_meta",
        "list_entities",
        "get_entity",
        "search_entities",
    }


def test_stdio_call_get_library_meta_against_cached_version(
    seeded_cache: Path,
) -> None:
    """The cold-start cache fallback path serves a real ``get_library_meta``.

    The registry URL points at a dead port (Slice 8's "offline" code
    path) and the cache is pre-seeded, so we expect ``from_cache=True``
    in the structured tool result. This is the demo PRD §8 calls out
    "disable VPN, restart, observe degraded-mode log line and a still-
    functional ``get_entity`` against the cached version" — exercised
    end-to-end over real stdio.
    """
    env = _server_env(seeded_cache)

    with _spawn_server(env) as client:
        _initialize(client)

        response = client.request(
            "tools/call",
            params={"name": "get_library_meta", "arguments": {}},
        )

    assert "result" in response, (
        f"tools/call failed; stderr:\n{client.stderr()}"
    )
    result = response["result"]

    # FastMCP returns structured content under ``structuredContent`` for
    # tools that return dicts (which ``get_library_meta`` does).
    structured = result.get("structuredContent") or {}
    assert structured.get("package_name") == PACKAGE
    assert structured.get("version") == VERSION
    assert structured.get("from_cache") is True, (
        "expected cache fallback; stderr:\n" + client.stderr()
    )


def test_stdio_call_list_entities_returns_seeded_components(
    seeded_cache: Path,
) -> None:
    """``list_entities`` over real stdio returns rows from the cache."""
    env = _server_env(seeded_cache)

    with _spawn_server(env) as client:
        _initialize(client)

        response = client.request(
            "tools/call",
            params={
                "name": "list_entities",
                "arguments": {"type": "component"},
            },
        )

    assert "result" in response, (
        f"tools/call failed; stderr:\n{client.stderr()}"
    )
    structured = response["result"].get("structuredContent") or {}
    assert structured.get("version") == VERSION
    names = {row["name"] for row in structured.get("entities", [])}
    # The default ``make_prism_tarball`` ships Button + Modal.
    assert {"Button", "Modal"} <= names, (
        f"unexpected component set; stderr:\n{client.stderr()}"
    )
