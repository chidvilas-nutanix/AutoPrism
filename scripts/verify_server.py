"""Manual end-to-end verification of the prism-mcp server.

Spawns ``uv run prism-mcp``, drives a JSON-RPC 2.0 handshake over stdio,
and prints a short summary so you can confirm the server actually
reached Artifactory and indexed real Prism entities.

Unlike a shell ``printf | uv run prism-mcp`` pipeline, this script keeps
stdin open until every expected response has been read on stdout. That
avoids the "stdin closed mid-call so the server cancelled the in-flight
tool" race we hit before.

Usage:
    PRISM_MCP_CA_BUNDLE=~/.cache/prism-mcp/canaveral-ca-bundle.pem \
        JFROG_EMAIL=... JFROG_API_KEY=... \
        uv run python scripts/verify_server.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REQUESTS: list[dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "verify_server.py", "version": "0.0"},
        },
    },
    {
        # No id => notification, no response expected.
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    },
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_library_meta", "arguments": {}},
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "search_examples",
            "arguments": {"query": "button with click handler", "top_k": 5},
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "search_entities",
            "arguments": {"query": "modal dialog", "top_k": 5},
        },
    },
]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    stderr_path = repo_root / "/tmp/prism-mcp.stderr"
    with open("/tmp/prism-mcp.stderr", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            ["uv", "run", "prism-mcp"],
            cwd=str(repo_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            env=os.environ.copy(),
            bufsize=0,
        )

    assert proc.stdin is not None
    assert proc.stdout is not None

    expected_response_ids = {req["id"] for req in REQUESTS if "id" in req}
    responses: dict[int, dict[str, Any]] = {}

    try:
        for req in REQUESTS:
            line = (json.dumps(req) + "\n").encode("utf-8")
            proc.stdin.write(line)
            proc.stdin.flush()

        while expected_response_ids - responses.keys():
            raw = proc.stdout.readline()
            if not raw:
                break
            msg = json.loads(raw.decode("utf-8"))
            mid = msg.get("id")
            if mid in expected_response_ids:
                responses[mid] = msg
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    print(f"stderr -> {stderr_path}")
    print(f"got {len(responses)}/{len(expected_response_ids)} responses")
    print()

    _summarize(responses)
    return 0 if len(responses) == len(expected_response_ids) else 1


def _summarize(responses: dict[int, dict[str, Any]]) -> None:
    for mid in sorted(responses):
        msg = responses[mid]
        if "error" in msg:
            print(f"id={mid} ERROR {msg['error']}")
            continue
        result = msg.get("result", {})
        if mid == 1:
            info = result.get("serverInfo", {})
            print(
                f"id=1 initialize OK  name={info.get('name')}  "
                f"version={info.get('version')}"
            )
        elif mid == 2:
            names = [t["name"] for t in result.get("tools", [])]
            print(f"id=2 tools/list      {len(names)} tools: {names}")
        elif mid == 3:
            sc = result.get("structuredContent", {})
            print("id=3 get_library_meta")
            for key in (
                "package_name",
                "version",
                "from_cache",
                "source_url",
                "cache_path",
            ):
                print(f"   {key}: {sc.get(key)}")
        elif mid == 4:
            sc = result.get("structuredContent", {})
            hits = sc.get("results", [])
            print(
                f"id=4 search_examples version={sc.get('version')}  "
                f"hits={len(hits)}"
            )
            for hit in hits[:10]:
                title = (hit.get("title") or "")[:50]
                print(f"   - {hit.get('component_name', '?'):20s} {title}")
        elif mid == 5:
            sc = result.get("structuredContent", {})
            hits = sc.get("results", [])
            print(f"id=5 search_entities query='modal dialog' hits={len(hits)}")
            for hit in hits[:5]:
                print(
                    f"   - {hit['name']:30s} type={hit['type']:10s} "
                    f"score={hit.get('score', 0):.3f} why={hit.get('why_matched', [])}"
                )


if __name__ == "__main__":
    sys.exit(main())
