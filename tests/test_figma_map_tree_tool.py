"""Tests for the page-level MCP tool wrapper (Phase 7 + 9).

* The ``map_figma_tree`` tool is registered on the server.
* ``parse_figma_url``, ``_fetch_figma_tree``, and ``walk_tree``
  remain package-private (not exposed as MCP tools).
* The SERVER_INSTRUCTIONS string contains the new page-level
  flow section.
* ``FetchError``s from the fetcher are translated into
  ``[<code>] <message>`` messages and re-raised as ``ValueError``
  for FastMCP.

Real REST calls never happen here — :func:`_fetch_figma_tree_full` is
monkey-patched globally.
"""

from __future__ import annotations

from typing import Any

import pytest

from prism_mcp.figma.fetch import FetchedTree, FetchError, FetchErrorCode
from prism_mcp.server import (
    SERVER_INSTRUCTIONS,
    _fetch_error_to_mcp,
    build_server,
)

# --------------------------------------------------------------------------
# SERVER_INSTRUCTIONS — page-level section is present.
# --------------------------------------------------------------------------


def test_server_instructions_contain_page_level_section() -> None:
    """The new section header must appear so Cursor surfaces the
    page-level flow alongside the per-node flow."""
    assert "PAGE-LEVEL FIGMA -> PRISM FLOW" in SERVER_INSTRUCTIONS


def test_server_instructions_mention_map_figma_tree() -> None:
    """The page-level tool must be referenced by name so the LLM
    knows how to invoke the flow."""
    assert "map_figma_tree" in SERVER_INSTRUCTIONS


def test_server_instructions_mention_all_fetch_error_codes() -> None:
    """The skill recovers per-code; the instructions must list the
    full taxonomy so the LLM knows what to expect."""
    codes = (
        FetchErrorCode.missing_token,
        FetchErrorCode.invalid_token,
        FetchErrorCode.file_not_found,
        FetchErrorCode.node_not_found,
        FetchErrorCode.rate_limited,
        FetchErrorCode.network_timeout,
        FetchErrorCode.tree_too_large,
        FetchErrorCode.transport_error,
        FetchErrorCode.invalid_url,
    )
    for code in codes:
        assert code in SERVER_INSTRUCTIONS, (
            f"FetchErrorCode.{code} must appear in SERVER_INSTRUCTIONS so "
            "the page-level skill can recover from each failure mode"
        )


# --------------------------------------------------------------------------
# Tool registration.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_figma_tree_is_registered() -> None:
    """``map_figma_tree`` shows up in ``list_tools()``."""
    server = build_server(enable_refresh_loop=False)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "map_figma_tree" in names


@pytest.mark.asyncio
async def test_private_fetch_helpers_are_not_registered() -> None:
    """``_fetch_figma_tree``, ``parse_figma_url``, and ``walk_tree``
    are package-private — never exposed as MCP tools.

    Exposing them would tempt callers to bypass the walker or to
    skip the noise filter, leaving the prompt cache hot on raw
    Figma JSON. Keeping them off the registry forces the
    canonical path."""
    server = build_server(enable_refresh_loop=False)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    for forbidden in (
        "fetch_figma_tree",
        "_fetch_figma_tree",
        "parse_figma_url",
        "walk_tree",
    ):
        assert forbidden not in names, (
            f"{forbidden!r} must not be exposed as an MCP tool; "
            "the public entrypoint is map_figma_tree"
        )


# --------------------------------------------------------------------------
# Error-translation helper.
# --------------------------------------------------------------------------


def test_fetch_error_to_mcp_includes_code_message_and_hint() -> None:
    exc = FetchError(
        code=FetchErrorCode.invalid_token,
        message="403 from Figma",
    )
    rendered = _fetch_error_to_mcp(exc)
    assert "[invalid_token]" in rendered
    assert "403 from Figma" in rendered
    assert "Hint:" in rendered


def test_fetch_error_to_mcp_handles_missing_hint() -> None:
    exc = FetchError(
        code="custom_unknown",
        message="something went wrong",
        hint="",
    )
    rendered = _fetch_error_to_mcp(exc)
    assert "[custom_unknown]" in rendered
    assert "something went wrong" in rendered
    assert "Hint:" not in rendered


# --------------------------------------------------------------------------
# End-to-end map_figma_tree with mocked fetcher (no network).
# --------------------------------------------------------------------------


_MINI_TREE = {
    "id": "1:1",
    "name": "Page",
    "type": "FRAME",
    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 320, "height": 200},
    "fills": [
        {"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}, "opacity": 1.0}
    ],
    "children": [
        {
            "id": "1:2",
            "name": "Modal",
            "type": "INSTANCE",
            "componentId": "1:2:comp",
            "absoluteBoundingBox": {
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 100,
            },
            "fills": [
                {
                    "type": "SOLID",
                    "color": {"r": 1, "g": 1, "b": 1},
                    "opacity": 1.0,
                }
            ],
        },
    ],
}

# The sibling ``components`` map the P1 fetch fix threads alongside the
# document. The Modal instance's ``componentId`` resolves here to its
# global ``componentKey`` + logical name + styleguide URL.
_MINI_COMPONENTS = {
    "1:2:comp": {
        "key": "modalkey123",
        "name": "Modal/ \u2705 Standard Modal",
        "description": (
            "http://prism-styleguide/v2/index.html#/Components/Modal?id=modal"
        ),
        "remote": True,
        "documentationLinks": [],
    }
}


@pytest.mark.asyncio
async def test_map_figma_tree_returns_walker_output_when_fetch_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: the tool wrapper threads the mocked fetch into
    the walker and returns a dict-shaped FigmaTreeMapping."""

    async def _fake_fetch(**kwargs: Any) -> FetchedTree:
        return FetchedTree(
            document=_MINI_TREE,
            components=_MINI_COMPONENTS,
            component_sets={},
            styles={},
        )

    monkeypatch.setattr(
        "prism_mcp.server._fetch_figma_tree_full", _fake_fetch
    )
    # ``build_server`` defers the library construction; we never
    # touch the library because the walker only emits agenda rows
    # when the noise filter passes — and the mini fixture has 2
    # nodes so map_figma_node may be called. We stub the bound
    # mapping function via the library so it never tries to load
    # the index.
    server = _build_server_with_stub_library()

    tool = await _get_tool(server, "map_figma_tree")
    result = await tool.run(
        {
            "input": {
                "node_url": "https://www.figma.com/design/k/x?node-id=1-1",
            }
        }
    )

    payload = _extract_payload(result)
    assert "summary" in payload
    assert payload["summary"]["input_nodes"] >= 1
    assert isinstance(payload["agenda"], list)

    # P1 fetch fix: the Modal INSTANCE's exact identity must be
    # resolved from the threaded components map and surfaced on the
    # lean agenda row.
    identities = [
        row.get("figma_component")
        for row in payload["agenda"]
        if row.get("figma_component")
    ]
    assert identities, "expected the Modal instance to carry figma_component"
    modal = identities[0]
    assert modal["component_key"] == "modalkey123"
    assert modal["component_name"] == "Modal/ \u2705 Standard Modal"
    assert modal["remote"] is True
    assert modal["doc_url"] and modal["doc_url"].startswith("http")


@pytest.mark.asyncio
async def test_map_figma_tree_codespec_detail_returns_render_ready_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``response_detail="codespec"`` returns the P8 render-ready spec.

    The wire shape is the :class:`PrismCodeSpec` dict — ``roots`` /
    ``imports`` / ``tokens`` / ``stats`` / ``warnings`` — not the lean
    agenda. Every spec node carries a resolved ``tag``.
    """

    async def _fake_fetch(**kwargs: Any) -> FetchedTree:
        return FetchedTree(
            document=_MINI_TREE,
            components=_MINI_COMPONENTS,
            component_sets={},
            styles={},
        )

    monkeypatch.setattr(
        "prism_mcp.server._fetch_figma_tree_full", _fake_fetch
    )
    server = _build_server_with_stub_library()
    tool = await _get_tool(server, "map_figma_tree")
    result = await tool.run(
        {
            "input": {
                "node_url": "https://www.figma.com/design/k/x?node-id=1-1",
                "response_detail": "codespec",
            }
        }
    )

    payload = _extract_payload(result)
    assert set(payload) == {"roots", "imports", "tokens", "stats", "warnings"}
    assert payload["roots"], "expected at least one render-ready root"
    assert payload["stats"]["nodes"] >= 1

    def _tags(node: dict[str, Any]) -> list[str]:
        out = [node["tag"]]
        for child in node["children"]:
            out.extend(_tags(child))
        return out

    all_tags = [t for r in payload["roots"] for t in _tags(r)]
    assert all(isinstance(t, str) and t for t in all_tags)


@pytest.mark.asyncio
async def test_map_figma_tree_surfaces_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FetchError from the fetcher becomes a Cursor-facing
    ``ValueError`` carrying the structured code prefix."""

    async def _fake_fetch(**kwargs: Any) -> FetchedTree:
        raise FetchError(
            code=FetchErrorCode.invalid_token,
            message="403 from Figma",
        )

    monkeypatch.setattr(
        "prism_mcp.server._fetch_figma_tree_full", _fake_fetch
    )
    server = _build_server_with_stub_library()
    tool = await _get_tool(server, "map_figma_tree")

    with pytest.raises(Exception) as ei:
        await tool.run(
            {
                "input": {
                    "node_url": "https://www.figma.com/design/k/x?node-id=1-1",
                }
            }
        )

    # FastMCP wraps the ValueError into a ToolError-shaped exception.
    assert "[invalid_token]" in str(ei.value)


# --------------------------------------------------------------------------
# Helpers — build a server whose Library is a stub.
# --------------------------------------------------------------------------


def _build_server_with_stub_library() -> Any:
    """Construct a FastMCP server whose Library is a stub.

    The page-level wrapper calls ``library.index() / hybrid_searcher() /
    composition_graph() / color_token_index() / a11y_rules()`` per
    agenda row. We subclass the real :class:`Library` to satisfy the
    ``isinstance`` check in ``_ServerState.library`` but skip the
    real constructor (which would require a registry + encoder).
    """
    from prism_mcp.a11y import A11yRules
    from prism_mcp.embeddings import ExampleHit
    from prism_mcp.graph import build_composition_graph
    from prism_mcp.indexer import Index
    from prism_mcp.library import Library
    from prism_mcp.tokens_index import build_color_token_index

    class _StubSearcher:
        def search(self, **kwargs: Any) -> list[ExampleHit]:
            return []

    class _StubLibrary(Library):
        # Skip the real __init__ entirely — we don't need its
        # registry / cache / encoder construction.
        def __init__(self) -> None:  # type: ignore[override]
            pass

        def index(self) -> Index:  # type: ignore[override]
            return Index(entities=[], version="t")

        def hybrid_searcher(self) -> Any:  # type: ignore[override]
            return _StubSearcher()

        def composition_graph(self):  # type: ignore[override]
            return build_composition_graph(chunks=[], version="t")

        def color_token_index(self):  # type: ignore[override]
            return build_color_token_index(entities=[], version="t")

        def a11y_rules(self) -> A11yRules:  # type: ignore[override]
            return A11yRules(
                version="t",
                title=None,
                global_rules=[],
                per_component=[],
            )

    return build_server(
        library_factory=lambda: _StubLibrary(),
        enable_refresh_loop=False,
    )


async def _get_tool(server: Any, name: str) -> Any:
    """Look up a FastMCP tool by name and return its tool wrapper."""
    tool_manager = server._tool_manager
    return tool_manager._tools[name]


def _extract_payload(result: Any) -> dict[str, Any]:
    """Pull the dict payload out of FastMCP's tool result.

    FastMCP ``Tool.run`` returns the raw tool body when
    ``convert_result=False`` (the default in our path), so
    ``result`` is already the dict we want."""
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured
    import json

    content = getattr(result, "content", None)
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    raise AssertionError(f"could not extract payload from {result!r}")
