"""Tests for the slice-12 reflection scaffold.

The scaffold is the AlphaCodium "pre-process" stage: take a free-
form spec + a component name, fan out to the slice-9..11 indices,
return a structured :class:`ReflectionContext` that the Cursor
agent loop reads as input *before* it generates JSX.

We intentionally accept the four sub-indices as explicit arguments
(``hybrid_searcher``, ``composition_graph``, ``color_token_index``,
``a11y_rules``) instead of a whole :class:`Library` — keeps this
module independent and trivially testable with the same fixtures
the slice-9/10/11 tests already use.
"""

from __future__ import annotations

from prism_mcp.a11y import A11yRules, ComponentA11y
from prism_mcp.embeddings import ExampleHit
from prism_mcp.graph import build_composition_graph
from prism_mcp.parsers.examples_md_code import ExampleChunk
from prism_mcp.tokens_index import (
    ColorTokenIndex,
    build_color_token_index,
)
from prism_mcp.workflow.contracts import ReflectionContext
from prism_mcp.workflow.reflection import (
    build_reflection_context,
    extract_hex_literals,
)

# --------------------------------------------------------------------------
# Minimal stub for the slice-9 HybridSearcher so tests don't pull in
# the real fastembed model.
# --------------------------------------------------------------------------


class _StubHybridSearcher:
    """Deterministic stub returning preset hits regardless of query."""

    def __init__(self, hits: list[ExampleHit]) -> None:
        self._hits = hits

    def search(self, **kwargs: object) -> list[ExampleHit]:
        top_k = int(kwargs.get("top_k", 3))
        return self._hits[:top_k]


def _hit(code: str, component_name: str = "Modal") -> ExampleHit:
    """Build a small ExampleHit fixture for the stub searcher."""
    return ExampleHit(
        component_name=component_name,
        title="Example",
        code=code,
        imports=[component_name],
        score=1.0,
    )


def _graph(chunks: list[ExampleChunk]):
    """Build a CompositionGraph from a chunk list."""
    return build_composition_graph(chunks=chunks, version="t")


def _chunk(component: str, imports: list[str]) -> ExampleChunk:
    """Minimal ExampleChunk fixture."""
    return ExampleChunk(
        component_name=component,
        title="t",
        code="<x/>",
        language_tag="jsx",
        imports=imports,
    )


def _color_index(tokens: list[tuple[str, str]]) -> ColorTokenIndex:
    """Build a ColorTokenIndex from (name, hex) pairs.

    We re-use the real :func:`build_color_token_index` so the test
    exercises the same Oklab-distance ranking the production code
    will use.
    """
    from prism_mcp.entities import Entity

    entities = [
        Entity(
            name=name,
            type="token",
            version="t",
            category="color",
            value=hex_value,
            source_file="src/styles/v2/Colors.less",
        )
        for name, hex_value in tokens
    ]
    return build_color_token_index(entities=entities, version="t")


def _a11y_rules(per_component: list[ComponentA11y] | None = None) -> A11yRules:
    """Build a minimal A11yRules fixture."""
    return A11yRules(
        version="t",
        title=None,
        global_rules=[],
        per_component=per_component or [],
    )


# --------------------------------------------------------------------------
# extract_hex_literals: parse Figma-style hex from a free-form spec.
# --------------------------------------------------------------------------


def test_extract_hex_literals_finds_hashed_codes() -> None:
    """Six-char hex codes prefixed with ``#`` are surfaced."""
    spec = "Use #1B6BCC for primary and #FF0000 for danger."

    hexes = extract_hex_literals(spec)

    assert hexes == ["#1B6BCC", "#FF0000"]


def test_extract_hex_literals_normalises_three_digit_codes() -> None:
    """Three-digit shorthand (``#FFF``) is expanded to six digits."""
    spec = "Background is #FFF; border is #000."

    assert extract_hex_literals(spec) == ["#FFFFFF", "#000000"]


def test_extract_hex_literals_deduplicates_preserving_order() -> None:
    """A hex mentioned twice appears once, in first-seen order."""
    spec = "#1B6BCC, #1B6BCC, then #FF0000"

    assert extract_hex_literals(spec) == ["#1B6BCC", "#FF0000"]


def test_extract_hex_literals_ignores_non_hex_tokens() -> None:
    """``#header`` (anchor) and ``primary-1`` (token) are not hex.

    The parser is conservative on purpose — false positives leak
    into ``color_token_index.query`` and would muddy the matches.
    """
    spec = "#header anchors to color-primary-1 see #not-a-hex."

    assert extract_hex_literals(spec) == []


# --------------------------------------------------------------------------
# build_reflection_context: end-to-end shape over real indices.
# --------------------------------------------------------------------------


def test_reflection_context_carries_examples_from_searcher() -> None:
    """Top-k hits from the hybrid searcher become ``examples``."""
    searcher = _StubHybridSearcher([_hit("<A/>"), _hit("<B/>"), _hit("<C/>")])
    graph = _graph(chunks=[])
    color_index = _color_index([])
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="X",
        spec_text="modal that submits a form",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
        top_k_examples=2,
    )

    assert ctx.examples == ["<A/>", "<B/>"]


def test_reflection_context_carries_related_from_graph() -> None:
    """Composition-graph neighbours become ``related``."""
    searcher = _StubHybridSearcher([])
    graph = _graph(
        chunks=[
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "StackingLayout"]),
        ]
    )
    color_index = _color_index([])
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="Modal",
        spec_text="",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
        top_k_related=5,
    )

    assert ctx.related == ["Button", "StackingLayout"]


def test_reflection_context_carries_token_hints_for_hex_in_spec() -> None:
    """Hex literals in the spec are matched against the token index."""
    searcher = _StubHybridSearcher([])
    graph = _graph(chunks=[])
    color_index = _color_index(
        [
            ("color-primary", "#1B6BCC"),
            ("color-danger", "#FF0000"),
        ]
    )
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="X",
        spec_text="primary is #1B6BCC and danger is #FF0000",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert "color-primary" in ctx.token_hints
    assert "color-danger" in ctx.token_hints


def test_reflection_context_uses_supplied_hex_colors_when_passed() -> None:
    """Explicit ``hex_colors`` override spec-text parsing."""
    searcher = _StubHybridSearcher([])
    graph = _graph(chunks=[])
    color_index = _color_index(
        [
            ("color-primary", "#1B6BCC"),
        ]
    )
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="X",
        spec_text="no hex here",
        hex_colors=["#1B6BCC"],
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert "color-primary" in ctx.token_hints


def test_reflection_context_carries_per_component_a11y_blocks() -> None:
    """``ComponentA11y`` for the queried component is surfaced."""
    searcher = _StubHybridSearcher([])
    graph = _graph(chunks=[])
    color_index = _color_index([])
    a11y = _a11y_rules(
        per_component=[
            ComponentA11y(
                component_name="Modal",
                titles=["Accessibility"],
                blocks=["return focus on close"],
            ),
        ]
    )

    ctx = build_reflection_context(
        component_name="Modal",
        spec_text="",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert ctx.a11y_blocks == ["return focus on close"]


def test_reflection_context_enumerates_two_candidate_decompositions() -> None:
    """Per the slice-12 trim: enumerate exactly 2 candidates.

    Each is a short string like ``"Modal + Button"`` derived from
    the top-2 graph neighbours.
    """
    searcher = _StubHybridSearcher([])
    graph = _graph(
        chunks=[
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "StackingLayout"]),
        ]
    )
    color_index = _color_index([])
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="Modal",
        spec_text="",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert len(ctx.candidate_decompositions) == 2
    assert "Modal + Button" in ctx.candidate_decompositions


def test_reflection_context_handles_isolated_component_gracefully() -> None:
    """Components with no related neighbours produce empty lists, not errors."""
    searcher = _StubHybridSearcher([])
    graph = _graph(chunks=[_chunk("Loader", ["Loader"])])
    color_index = _color_index([])
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="Loader",
        spec_text="",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert ctx.related == []
    assert ctx.candidate_decompositions == []


def test_reflection_context_returns_empty_for_unknown_component() -> None:
    """Unknown component names short-circuit gracefully.

    The scaffold should never raise on unknown names — the LLM
    should hear "no related found" and adjust its query, not
    crash the workflow.
    """
    searcher = _StubHybridSearcher([])
    graph = _graph(chunks=[_chunk("Modal", ["Modal", "Button"])])
    color_index = _color_index([])
    a11y = _a11y_rules()

    ctx = build_reflection_context(
        component_name="GhostComponent",
        spec_text="",
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert isinstance(ctx, ReflectionContext)
    assert ctx.related == []
    assert ctx.candidate_decompositions == []
