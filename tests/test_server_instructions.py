"""Regression guard for the FastMCP server-level instructions string.

The MCP ``initialize`` response carries an optional ``instructions``
field that Cursor renders to the agent up front. We use it to
teach the LLM the canonical Figma->Prism flow + the time-cost
tradeoff for the iteration loop, so the LLM doesn't pick the
path of least resistance ("just call search_examples and synthesise
JSX without validating") when the user actually wants the
AlphaCodium-flavoured workflow.

These tests are intentionally tiny — we only verify the
instructions are non-empty and mention the load-bearing tools by
name. Drift detection: if a refactor accidentally drops the
``instructions=`` kwarg or trims away the canonical flow, this
suite fires before the regression reaches Cursor users.
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
        "start_generate_component",
        "submit_candidate",
        "get_final_artefact",
    ],
)
def test_instructions_mention_canonical_flow_tools(tool_name: str) -> None:
    """Every tool in the canonical flow must be named in the instructions.

    The instruction text guides the LLM through the four-step
    pipeline; if any of these names disappears the LLM loses the
    breadcrumb and reverts to ad-hoc tool selection.
    """
    assert tool_name in SERVER_INSTRUCTIONS, (
        f"SERVER_INSTRUCTIONS must mention {tool_name!r} so the "
        "canonical flow stays discoverable in the initialize handshake"
    )


def test_instructions_call_out_figma_png_url_for_workflow() -> None:
    """The instructions must explicitly tell the LLM to pass
    ``figma_png_url`` when starting the workflow.

    The Figma MCP almost always hands the agent a URL, not a path.
    Without this nudge in the instructions, the LLM frequently
    starts the workflow with no Figma reference at all, silently
    skipping SSIM and losing the visual-validation signal.
    """
    assert "figma_png_url" in SERVER_INSTRUCTIONS


def test_instructions_call_out_iteration_time_cost() -> None:
    """The instructions name the workflow's per-component runtime
    so the LLM can opt out of the iteration loop for trivial
    components without surprise.
    """
    # The plan's "1-3 minutes per component" framing must
    # survive in some form so the LLM (and downstream readers)
    # know the wall-clock budget at decision time.
    assert "1-3 minutes" in SERVER_INSTRUCTIONS or (
        "minute" in SERVER_INSTRUCTIONS.lower()
    )


def test_instructions_warn_against_inventing_components() -> None:
    """The instructions must remind the LLM to pick from
    ``map_figma_node`` candidates, not invent component names.
    """
    body = SERVER_INSTRUCTIONS.lower()
    assert "candidates" in body
    assert "never invent" in body or "do not invent" in body


def test_instructions_call_out_context_field_on_start_response() -> None:
    """The instructions must teach the LLM to read the new ``context``
    field on ``start_generate_component`` *before* writing iteration-1
    code — that's the load-bearing change for the May-2026 a11y
    context gap.
    """
    body = SERVER_INSTRUCTIONS.lower()
    assert "context" in body
    assert "imitation" in body or "imitation_pwspec" in body


def test_instructions_pin_consumer_style_imports() -> None:
    """The instructions must standardise on package-name imports
    (``@nutanix-ui/prism-reactjs``) so the LLM doesn't rewrite to
    relative paths just to placate the validator. The jest config's
    ``moduleNameMapper`` self-resolves the package name, so the
    same artefact works in the consumer app *and* the validator.
    """
    body = SERVER_INSTRUCTIONS
    assert "@nutanix-ui/prism-reactjs" in body
    body_lower = body.lower()
    # Either an explicit "do NOT switch to relative" warning or a
    # positive "use consumer-style/package-name" directive must
    # survive; both are acceptable phrasings.
    assert (
        "relative" in body_lower
        or "consumer-style" in body_lower
        or "package-name" in body_lower
        or "package name" in body_lower
    )
