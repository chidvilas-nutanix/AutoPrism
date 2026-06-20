"""Regression guard for the FastMCP server-level instructions string.

The MCP ``initialize`` response carries an optional ``instructions``
field that Cursor renders to the agent up front. We use it to teach the
LLM the canonical Figma->Prism flow and to make clear that this server
supplies Prism building blocks while Cursor runs validation.

These tests are intentionally tiny -- we only verify the instructions
are non-empty and mention the load-bearing tools by name. Drift
detection: if a refactor accidentally drops the ``instructions=`` kwarg
or trims away the canonical flow, this suite fires before the
regression reaches Cursor users.
"""

from __future__ import annotations

import pytest

from prism_mcp.server import SERVER_INSTRUCTIONS, build_server


def test_server_constructed_with_non_empty_instructions() -> None:
    """The constructed FastMCP instance carries ``instructions`` text."""
    server = build_server(enable_refresh_loop=False)
    assert server.instructions, (
        "build_server must set instructions= so Cursor surfaces the "
        "canonical Figma->Prism flow up-front to the agent"
    )
    assert server.instructions == SERVER_INSTRUCTIONS


@pytest.mark.parametrize(
    "tool_name",
    [
        "map_figma_node",
        "search_examples",
        "search_entities",
        "get_entity",
    ],
)
def test_instructions_mention_canonical_flow_tools(tool_name: str) -> None:
    """Every tool in the canonical flow must be named in the instructions.

    The instruction text guides the LLM through the search/mapping
    pipeline; if any of these names disappears the LLM loses the
    breadcrumb and reverts to ad-hoc tool selection.
    """
    assert tool_name in SERVER_INSTRUCTIONS, (
        f"SERVER_INSTRUCTIONS must mention {tool_name!r} so the "
        "canonical flow stays discoverable in the initialize handshake"
    )


def test_instructions_warn_against_inventing_components() -> None:
    """The instructions must remind the LLM to pick from
    ``map_figma_node`` candidates, not invent component names.
    """
    body = SERVER_INSTRUCTIONS.lower()
    assert "candidates" in body
    assert "never invent" in body or "do not invent" in body


def test_instructions_hand_validation_to_cursor() -> None:
    """The instructions must make clear the MCP does not build/validate;
    Cursor runs tsc/eslint/tests in its own loop.

    This is the load-bearing change after the verification-loop
    removal: the LLM must know to validate generated JSX itself rather
    than wait for an in-MCP iteration loop that no longer exists.
    """
    body = SERVER_INSTRUCTIONS.lower()
    assert "validate" in body
    assert "cursor" in body


def test_instructions_pin_consumer_style_imports() -> None:
    """The instructions must standardise on package-name imports
    (``@nutanix-ui/prism-reactjs``) so the LLM doesn't rewrite to
    relative paths.
    """
    body = SERVER_INSTRUCTIONS
    assert "@nutanix-ui/prism-reactjs" in body
    body_lower = body.lower()
    assert (
        "relative" in body_lower
        or "consumer-style" in body_lower
        or "package-name" in body_lower
        or "package name" in body_lower
    )
