"""Slice 1 smoke test.

Exercises the MCP server in-process to prove that the scaffold is wired
up correctly. This is the must-pass loop on commit zero (PRD section 8,
Slice 1) and is what CI runs.

We deliberately avoid spinning up a subprocess + stdio client here:
keeping the test in-process means CI failures point at our code, not at
transport edge cases. A real stdio E2E test arrives in a later slice.
"""

from __future__ import annotations

import pytest

from prism_mcp import __version__
from prism_mcp.server import ECHO_REPLY, SERVER_NAME, build_server


def test_package_has_version() -> None:
    """The package exposes a non-empty semver-ish version string."""
    assert isinstance(__version__, str)
    assert __version__, "expected a non-empty __version__"


@pytest.mark.asyncio
async def test_echo_tool_is_registered_and_returns_expected_string() -> None:
    """The ``echo`` tool is registered and returns the canonical reply.

    Together these two checks prove (a) the FastMCP tool-registration
    decorator ran at server construction time and (b) the in-process
    call path can execute a tool body. That's enough liveness to call
    the scaffold green.
    """
    server = build_server()

    assert server.name == SERVER_NAME

    tools = await server.list_tools()
    tool_names = [tool.name for tool in tools]
    assert "echo" in tool_names, (
        f"expected 'echo' to be registered, got {tool_names!r}"
    )

    result = await server.call_tool("echo", arguments={})

    # FastMCP 1.27.x ``call_tool`` returns a 2-tuple of
    # ``(content_blocks, structured_dict)``. The static type hint claims
    # a union, but the runtime shape is the tuple; accept either to stay
    # forward-compatible.
    content_blocks, structured = _split_call_tool_result(result)

    text_from_blocks = "".join(
        getattr(block, "text", "") for block in content_blocks
    )
    text_from_structured = (
        str(structured.get("result", "")) if structured else ""
    )

    assert ECHO_REPLY in (text_from_blocks + text_from_structured), (
        f"expected {ECHO_REPLY!r} in echo result, got {result!r}"
    )


def _split_call_tool_result(
    result: object,
) -> tuple[list[object], dict[str, object]]:
    """Normalize ``FastMCP.call_tool`` return shapes.

    The SDK has historically returned a bare ``Sequence[ContentBlock]``,
    a bare structured ``dict``, or (since 1.27.x) a 2-tuple of both. We
    canonicalize to ``(blocks, structured)`` so the assertion above
    survives across minor SDK bumps.

    Args:
        result (object): whatever ``await server.call_tool(...)`` returned.

    Returns:
        tuple[list[object], dict[str, object]]: a ``(blocks, structured)``
        pair where either side may be empty.
    """
    if isinstance(result, tuple) and len(result) == 2:
        blocks, structured = result
        return (
            list(blocks) if blocks is not None else [],
            dict(structured) if isinstance(structured, dict) else {},
        )
    if isinstance(result, dict):
        return [], dict(result)
    if isinstance(result, list):
        return result, {}
    return [], {}
