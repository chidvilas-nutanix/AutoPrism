"""Tests for the slice-12 Figma-to-Prism composite mapper.

The mapper is a pure fan-out over the slice-3..11 indices, just
like :mod:`prism_mcp.workflow.reflection`. We exercise it the same
way: real :class:`Index` / :class:`CompositionGraph` /
:class:`ColorTokenIndex` / :class:`A11yRules` fixtures, plus a
deterministic stub for the slice-9 :class:`HybridSearcher` so the
fastembed encoder never has to spin up.
"""

from __future__ import annotations

from prism_mcp.a11y import A11yRules, ComponentA11y
from prism_mcp.embeddings import ExampleHit
from prism_mcp.entities import Entity, Member
from prism_mcp.graph import build_composition_graph
from prism_mcp.indexer import Index
from prism_mcp.parsers.examples_md_code import ExampleChunk
from prism_mcp.tokens_index import (
    ColorTokenIndex,
    build_color_token_index,
)
from prism_mcp.workflow.figma_mapping import (
    FigmaNodeMapping,
    map_figma_node,
)

# --------------------------------------------------------------------------
# Shared fixtures — deliberately mirror tests/test_workflow_reflection.py
# so the two test files can be read side-by-side.
# --------------------------------------------------------------------------


class _StubHybridSearcher:
    """Records inbound queries and returns preset hits.

    We let each hit's ``score`` differ so the RRF fusion still has
    a well-defined order — the production hybrid searcher always
    returns hits in score-descending order.
    """

    def __init__(self, hits: list[ExampleHit]) -> None:
        self._hits = hits
        self.calls: list[dict] = []

    def search(self, **kwargs: object) -> list[ExampleHit]:
        self.calls.append(dict(kwargs))
        top_k = int(kwargs.get("top_k", 3))
        return self._hits[:top_k]


def _hit(code: str, component_name: str = "Modal") -> ExampleHit:
    """Build a small ExampleHit for the stub searcher."""
    return ExampleHit(
        component_name=component_name,
        title="Example",
        code=code,
        imports=[component_name],
        score=1.0,
    )


def _chunk(component: str, imports: list[str]) -> ExampleChunk:
    """Minimal ExampleChunk fixture."""
    return ExampleChunk(
        component_name=component,
        title="t",
        code="<x/>",
        language_tag="jsx",
        imports=imports,
    )


def _component_entity(name: str, summary: str = "") -> Entity:
    """Factory: component entity that the BM25 index can score."""
    return Entity(
        name=name,
        type="component",
        version="t",
        summary=summary or f"{name} component",
        import_path=f"@nutanix-ui/prism-reactjs/{name}",
        signature=[Member(name="x", kind="prop", type="string")],
    )


def _color_index(tokens: list[tuple[str, str]]) -> ColorTokenIndex:
    """Build a real ColorTokenIndex from ``(name, hex)`` pairs."""
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
    """Minimal A11yRules fixture."""
    return A11yRules(
        version="t",
        title=None,
        global_rules=[],
        per_component=per_component or [],
    )


# --------------------------------------------------------------------------
# Return shape — sanity check first.
# --------------------------------------------------------------------------


def test_map_figma_node_returns_full_mapping_shape() -> None:
    """Every field of :class:`FigmaNodeMapping` is present on the result.

    Even when sub-indices are empty, the return contract holds —
    the LLM sees a well-typed bundle and doesn't have to guard
    every accessor.
    """
    searcher = _StubHybridSearcher([])
    index = Index(entities=[], version="t")
    graph = build_composition_graph(chunks=[], version="t")
    color_index = _color_index([])
    a11y = _a11y_rules()

    mapping = map_figma_node(
        node_name="Empty Frame",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    assert isinstance(mapping, FigmaNodeMapping)
    assert mapping.node_name == "Empty Frame"
    assert mapping.suggested_component_name is None
    assert mapping.candidates == []
    assert mapping.related == []
    assert mapping.a11y_blocks == []
    assert mapping.token_mappings == []
    assert mapping.examples == []
    assert mapping.candidate_decompositions == []


# --------------------------------------------------------------------------
# Candidate fusion — BM25 + hybrid combine via RRF, source label is set.
# --------------------------------------------------------------------------


def test_map_figma_node_surfaces_bm25_candidate_from_layer_name() -> None:
    """A layer name that lexically matches a component appears in candidates."""
    searcher = _StubHybridSearcher([])
    index = Index(
        entities=[
            _component_entity("Modal", summary="modal dialog wrapper"),
            _component_entity("Button"),
        ],
        version="t",
    )
    graph = build_composition_graph(chunks=[], version="t")
    color_index = _color_index([])
    a11y = _a11y_rules()

    mapping = map_figma_node(
        node_name="Confirm Modal",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=color_index,
        a11y_rules=a11y,
    )

    names = [c.name for c in mapping.candidates]
    assert "Modal" in names
    assert mapping.suggested_component_name == "Modal"
    modal = next(c for c in mapping.candidates if c.name == "Modal")
    assert modal.source == "bm25"


def test_map_figma_node_extracts_jsx_tags_from_reference_code() -> None:
    """JSX tags in ``reference_code`` reinforce the BM25 query.

    Figma's ``get_design_context`` returns a React+Tailwind
    reference snippet whose component identifiers are the
    strongest possible lexical signal. We want those mixed into
    the BM25 query so a node literally containing ``<Button>``
    in its reference produces a strong ``Button`` candidate.
    """
    searcher = _StubHybridSearcher([])
    index = Index(
        entities=[
            _component_entity("Button"),
            _component_entity("StackingLayout"),
        ],
        version="t",
    )
    graph = build_composition_graph(chunks=[], version="t")

    mapping = map_figma_node(
        node_name="cta region",
        reference_code='<StackingLayout><Button variant="primary"/></StackingLayout>',
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    names = {c.name for c in mapping.candidates}
    assert {"Button", "StackingLayout"} <= names


def test_map_figma_node_marks_both_when_lexical_and_semantic_agree() -> None:
    """A component returned by *both* rankers gets ``source="both"``.

    "both" is the strongest signal in design-to-code matching —
    BM25 caught the exact name and the dense ranker caught the
    semantic intent independently.
    """
    searcher = _StubHybridSearcher(
        [_hit("<Modal/>", component_name="Modal")]
    )
    index = Index(
        entities=[_component_entity("Modal")],
        version="t",
    )

    mapping = map_figma_node(
        node_name="Modal",
        reference_code="<Modal/>",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    modal = next(c for c in mapping.candidates if c.name == "Modal")
    assert modal.source == "both"


def test_map_figma_node_marks_hybrid_only_when_lexical_misses() -> None:
    """Hybrid-only candidates exist when the layer name is generic."""
    searcher = _StubHybridSearcher(
        [_hit("<Carousel/>", component_name="Carousel")]
    )
    index = Index(entities=[_component_entity("Other")], version="t")

    mapping = map_figma_node(
        node_name="rotating images",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    by_name = {c.name: c for c in mapping.candidates}
    assert by_name["Carousel"].source == "hybrid"


# --------------------------------------------------------------------------
# Top-candidate anchoring — related, a11y, decompositions.
# --------------------------------------------------------------------------


def test_map_figma_node_anchors_related_to_top_candidate() -> None:
    """``related`` is the graph neighbours of the *top* candidate."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[_component_entity("Modal")], version="t")
    graph = build_composition_graph(
        chunks=[
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "StackingLayout"]),
        ],
        version="t",
    )

    mapping = map_figma_node(
        node_name="Modal",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    assert mapping.suggested_component_name == "Modal"
    assert "Button" in mapping.related
    assert "StackingLayout" in mapping.related


def test_map_figma_node_anchors_a11y_blocks_to_top_candidate() -> None:
    """Top candidate's a11y guidance is attached to the bundle."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[_component_entity("Modal")], version="t")
    a11y = _a11y_rules(
        per_component=[
            ComponentA11y(
                component_name="Modal",
                titles=["Accessibility"],
                blocks=["return focus on close"],
            ),
        ]
    )

    mapping = map_figma_node(
        node_name="Modal",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=a11y,
    )

    assert mapping.a11y_blocks == ["return focus on close"]


def test_map_figma_node_enumerates_two_candidate_decompositions() -> None:
    """``candidate_decompositions`` mirrors the reflection scaffold's shape."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[_component_entity("Modal")], version="t")
    graph = build_composition_graph(
        chunks=[
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "Button"]),
            _chunk("Modal", ["Modal", "StackingLayout"]),
        ],
        version="t",
    )

    mapping = map_figma_node(
        node_name="Modal",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    assert len(mapping.candidate_decompositions) == 2
    assert "Modal + Button" in mapping.candidate_decompositions


# --------------------------------------------------------------------------
# Token mappings — explicit hex_colors override extraction.
# --------------------------------------------------------------------------


def test_map_figma_node_extracts_hex_from_reference_code_by_default() -> None:
    """When ``hex_colors`` is omitted, parse them from ``reference_code``."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[], version="t")
    color_index = _color_index([("color-primary", "#1B6BCC")])

    mapping = map_figma_node(
        node_name="card",
        reference_code='<div className="bg-[#1B6BCC]">x</div>',
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=color_index,
        a11y_rules=_a11y_rules(),
    )

    assert len(mapping.token_mappings) == 1
    assert mapping.token_mappings[0].hex == "#1B6BCC"
    assert mapping.token_mappings[0].token_name == "color-primary"


def test_map_figma_node_uses_explicit_hex_colors_over_reference_code() -> None:
    """When both are supplied, ``hex_colors`` wins (matches reflection)."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[], version="t")
    color_index = _color_index([("color-primary", "#1B6BCC")])

    mapping = map_figma_node(
        node_name="card",
        reference_code='<div className="bg-[#000000]"/>',
        hex_colors=["#1B6BCC"],
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=color_index,
        a11y_rules=_a11y_rules(),
    )

    hexes = [tm.hex for tm in mapping.token_mappings]
    assert hexes == ["#1B6BCC"]
    assert mapping.token_mappings[0].token_name == "color-primary"


def test_map_figma_node_emits_no_match_for_far_off_hex() -> None:
    """When no token is close enough, the bucket is ``no-match``."""
    searcher = _StubHybridSearcher([])
    index = Index(entities=[], version="t")
    color_index = _color_index([])

    mapping = map_figma_node(
        node_name="card",
        hex_colors=["#1B6BCC"],
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=color_index,
        a11y_rules=_a11y_rules(),
    )

    assert mapping.token_mappings[0].bucket == "no-match"
    assert mapping.token_mappings[0].token_name is None


# --------------------------------------------------------------------------
# Examples — top-3 JSX bodies from the hybrid searcher.
# --------------------------------------------------------------------------


def test_map_figma_node_carries_top_three_example_bodies() -> None:
    """``examples`` are the top-3 hybrid hit code bodies."""
    searcher = _StubHybridSearcher(
        [
            _hit("<A/>"),
            _hit("<B/>"),
            _hit("<C/>"),
            _hit("<D/>"),
        ]
    )
    index = Index(entities=[_component_entity("Modal")], version="t")

    mapping = map_figma_node(
        node_name="Modal",
        reference_code="// some code",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    assert mapping.examples == ["<A/>", "<B/>", "<C/>"]


# --------------------------------------------------------------------------
# Defensive: never raise on unknown components.
# --------------------------------------------------------------------------


def test_map_figma_node_returns_empty_related_for_unknown_top_candidate() -> None:
    """Top candidate not in the graph → ``related`` empty, no exception.

    This matters because BM25 will surface entities the
    composition graph never saw an example for (e.g. a brand-new
    component without any ``examples.md`` content). The mapper
    must not raise — the LLM should just see "no related" and
    fall back to other signals.
    """
    searcher = _StubHybridSearcher([])
    index = Index(entities=[_component_entity("BrandNew")], version="t")
    # Graph is built from a corpus that does not mention "BrandNew".
    graph = build_composition_graph(
        chunks=[_chunk("Modal", ["Modal", "Button"])],
        version="t",
    )

    mapping = map_figma_node(
        node_name="BrandNew",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    assert mapping.suggested_component_name == "BrandNew"
    assert mapping.related == []
    assert mapping.candidate_decompositions == []
