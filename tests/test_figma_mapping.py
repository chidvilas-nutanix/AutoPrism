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
    _build_lexical_query,
    _build_semantic_query,
    _normalise_component_name,
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
    searcher = _StubHybridSearcher([_hit("<Modal/>", component_name="Modal")])
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


def test_map_figma_node_returns_empty_related_for_unknown_top_candidate() -> (
    None
):
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


# --------------------------------------------------------------------------
# Phase 6: query-builder enrichment regression tests.
#
# The four new optional kwargs (``text_content``, ``children_summary``,
# ``structural_hints``, ``parent_chain``) are additive — passing ``None``
# for all of them must reproduce the v1 query byte-for-byte. The walker
# in :mod:`prism_mcp.figma.walker` relies on the v1 behaviour to keep
# the slice-12 reflection loop's prompt cache warm.
# --------------------------------------------------------------------------


def test_build_lexical_query_v1_baseline_unchanged_without_enrichment() -> None:
    """Bare-name → ``"<node_name>"``."""
    assert (
        _build_lexical_query(
            node_name="Tile",
            node_type=None,
            reference_code=None,
        )
        == "Tile"
    )


def test_build_lexical_query_v1_baseline_with_type_unchanged() -> None:
    assert (
        _build_lexical_query(
            node_name="Tile",
            node_type="INSTANCE",
            reference_code=None,
        )
        == "Tile INSTANCE"
    )


def test_build_lexical_query_v1_baseline_with_ref_code_unchanged() -> None:
    """The reference-code JSX tag extraction is the v1 contract."""
    result = _build_lexical_query(
        node_name="Tile",
        node_type="INSTANCE",
        reference_code="<Tile><Header/></Tile>",
    )
    assert result == "Tile INSTANCE Tile Header"


def test_build_lexical_query_appends_text_content() -> None:
    result = _build_lexical_query(
        node_name="Tile",
        node_type="INSTANCE",
        reference_code=None,
        text_content="Top 5 Shares by Connections",
    )
    assert result.startswith("Tile INSTANCE")
    assert "Top 5 Shares by Connections" in result


def test_build_lexical_query_appends_children_summary_and_hints() -> None:
    result = _build_lexical_query(
        node_name="Tile",
        node_type="INSTANCE",
        reference_code=None,
        children_summary="FRAME Header(1 TEXT)",
        structural_hints=["320x309 ~square", "3-row vertical stack"],
    )
    assert "FRAME Header(1 TEXT)" in result
    assert "320x309 ~square" in result
    assert "3-row vertical stack" in result


def test_build_lexical_query_only_appends_last_two_parents() -> None:
    """Closer ancestors carry stronger context; deeper ones add noise."""
    result = _build_lexical_query(
        node_name="Tile",
        node_type="INSTANCE",
        reference_code=None,
        parent_chain=["Page", "Workspace", "Body", "Cluster Details"],
    )
    parts = result.split(" ")
    assert "Body" in parts
    assert "Cluster" in parts and "Details" in parts
    # First two ancestors should NOT appear.
    assert "Page" not in parts
    assert "Workspace" not in parts


def test_build_semantic_query_v1_baseline_bare_name() -> None:
    """Bare-name should yield just the name."""
    assert (
        _build_semantic_query(node_name="Tile", reference_code=None) == "Tile"
    )


def test_build_semantic_query_v1_baseline_with_ref_code_unchanged() -> None:
    assert (
        _build_semantic_query(node_name="Tile", reference_code="<Tile/>")
        == "Tile\n\n<Tile/>"
    )


def test_build_semantic_query_prepends_text_content_when_no_ref_code() -> None:
    result = _build_semantic_query(
        node_name="Tile",
        reference_code=None,
        text_content="Top 5 Shares by Connections",
    )
    assert result == "Tile\n\nTop 5 Shares by Connections"


def test_build_semantic_query_text_content_before_reference_code() -> None:
    """Both prepend cleanly: name → text_content → reference_code."""
    result = _build_semantic_query(
        node_name="Tile",
        reference_code="<Tile/>",
        text_content="Top 5 Shares by Connections",
    )
    assert result == "Tile\n\nTop 5 Shares by Connections\n\n<Tile/>"


# --------------------------------------------------------------------------
# Fix E — ``Domain/Type`` Figma naming rewriting (§11.6 + §12 "Fix E").
# --------------------------------------------------------------------------


def test_fix_e_lexical_query_splits_slash_namespaced_name() -> None:
    """``"Action/Link"`` MUST contribute both the literal and the
    space-split tokens so BM25 has matchable individual tokens. See
    ``docs/x-ray-walker-investigation.md`` §11.6 + §12 "Fix E".
    """
    result = _build_lexical_query(
        node_name="Action/Link",
        node_type="INSTANCE",
        reference_code=None,
    )
    assert "Action/Link" in result, (
        "literal must be preserved so descriptions that contain the "
        "literal still match"
    )
    assert "action" in result and "link" in result, (
        "split tokens must be added so BM25 has matchable individual "
        f"terms; got: {result!r}"
    )


def test_fix_e_lexical_query_applies_alias_table_for_link() -> None:
    """``"Action/Link"`` is in the alias table → ``"Link Action"`` hint
    must be appended so the Prism ``Link`` entity surfaces in the
    BM25 results."""
    result = _build_lexical_query(
        node_name="Action/Link",
        node_type="INSTANCE",
        reference_code=None,
    )
    assert "Link" in result and "Action" in result.split(), (
        f"alias hint missing; got: {result!r}"
    )


def test_fix_e_lexical_query_applies_alias_for_table_cell() -> None:
    """``"Table/Table Cell"`` → adds ``"TableCell Cell"`` so the
    Prism ``TableCell`` entity is reachable from the literal name."""
    result = _build_lexical_query(
        node_name="Table/Table Cell",
        node_type="INSTANCE",
        reference_code=None,
    )
    assert "TableCell" in result, (
        f"alias 'TableCell Cell' missing; got: {result!r}"
    )


def test_fix_e_lexical_query_preserves_unnamespaced_names() -> None:
    """A name without ``/`` MUST be left alone — Fix E is a no-op for
    regular product-page layer names and must not double-count
    tokens for them."""
    result = _build_lexical_query(
        node_name="Header",
        node_type="INSTANCE",
        reference_code=None,
    )
    assert result == "Header INSTANCE", (
        f"Fix E altered a non-namespaced name; got: {result!r}"
    )


def test_fix_e_lexical_query_rewrites_parent_chain_too() -> None:
    """Ancestors are also rewritten so a ``Modal/Empty`` parent
    contributes ``"modal empty"`` tokens to the child's query."""
    result = _build_lexical_query(
        node_name="Body Copy",
        node_type="TEXT",
        reference_code=None,
        parent_chain=["Page", "Workspace", "Modal/Empty", "Card/Normal"],
    )
    assert "modal empty" in result, (
        f"parent chain rewrite missing; got: {result!r}"
    )
    assert "card normal" in result, (
        f"parent chain rewrite missing; got: {result!r}"
    )


def test_fix_e_semantic_query_rewrites_name_too() -> None:
    """The dense / hybrid semantic query also benefits from the
    rewrite — same logic, same alias table."""
    result = _build_semantic_query(
        node_name="Modal/Fullpage", reference_code=None
    )
    assert "Modal/Fullpage" in result, (
        "literal must be preserved in semantic query"
    )
    assert "FullPageModal" in result, (
        f"alias hint missing in semantic query; got: {result!r}"
    )


# --------------------------------------------------------------------------
# Positive: enrichment surfaces a better candidate.
# --------------------------------------------------------------------------


def test_map_figma_node_text_content_improves_candidate_score() -> None:
    """With ``text_content``, the BM25 query matches more tokens of
    the Paragraph component's summary than the bare layer name
    alone — so Paragraph rises in the candidates."""
    searcher = _StubHybridSearcher([])
    index = Index(
        entities=[
            _component_entity(
                "Paragraph",
                summary="paragraph component for displaying lines of text content",
            ),
            _component_entity(
                "Tile",
                summary="generic tile container",
            ),
        ],
        version="t",
    )

    bare = map_figma_node(
        node_name="Frame 2540",
        node_type="FRAME",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    enriched = map_figma_node(
        node_name="Frame 2540",
        node_type="FRAME",
        text_content="paragraph text content lines",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    bare_top = bare.suggested_component_name
    enriched_names = [c.name for c in enriched.candidates]

    # With enrichment, Paragraph should appear at all (it shouldn't
    # in the bare case — "Frame 2540" has no tokens in common with
    # Paragraph's summary).
    assert "Paragraph" in enriched_names, (
        f"expected Paragraph in enriched candidates, got {enriched_names} "
        f"(bare top was {bare_top})"
    )


# --------------------------------------------------------------------------
# Layer D — role-synonym and shape-bucket bonuses.
#
# These tests pin the +0.15 / +0.05 boost mechanics from
# ``docs/handoff-spatial-and-ranker.md`` §3. Each test isolates one
# branch so a future tweak to either bonus constant can't silently
# regress the other.
# --------------------------------------------------------------------------


def _two_candidate_setup(
    role_winner: str, generic_runner_up: str
) -> tuple[Index, _StubHybridSearcher]:
    """Construct an index + hybrid stub where ``role_winner`` arrives
    via the hybrid ranker and ``generic_runner_up`` via BM25 with
    identical RRF rank.

    The fused scores tie at ``1/61`` before any bonus, so the
    sort tie-break runs alphabetically. Applying a role or
    shape-bucket boost to ``role_winner`` is the only way it can
    win — which makes the boost the variable under test.
    """
    index = Index(
        entities=[
            _component_entity(role_winner),
            _component_entity(generic_runner_up),
        ],
        version="t",
    )
    searcher = _StubHybridSearcher(
        [_hit("<X/>", component_name=role_winner)]
    )
    return index, searcher


def test_region_role_kpi_tile_boost_lifts_tile_above_card() -> None:
    """``region_role='kpi-tile'`` adds +0.15 to Tile, beating Card.

    Construction guarantees a fused tie before the boost so the
    role bonus is the only thing that can flip the order. If the
    bonus regresses to zero (or the synonyms entry vanishes) this
    test flips first.
    """
    # node_name "Card" so BM25 finds the "Card" entity; hybrid
    # contributes "Tile". Without the role boost they tie.
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    kwargs = dict(
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    no_role = map_figma_node(node_name="Card", **kwargs)
    with_role = map_figma_node(
        node_name="Card", region_role="kpi-tile", **kwargs
    )

    no_names = [c.name for c in no_role.candidates]
    with_names = [c.name for c in with_role.candidates]
    assert no_names.index("Card") < no_names.index("Tile")
    assert with_names.index("Tile") < with_names.index("Card")
    assert with_role.suggested_component_name == "Tile"


def test_region_role_table_column_boost_lifts_table_above_panel() -> None:
    """``region_role='table-column'`` lifts ``Table`` above an
    equally-scored ``Panel`` candidate **in the candidates list**.

    Covers a second entry in :data:`ROLE_TO_COMPONENT_SYNONYMS` so
    a future shrink to the synonym table (e.g. dropping
    ``"table"``) fails this case too.

    Note: ``suggested_component_name`` is asserted to be
    ``TableColumn`` (the deterministic
    :data:`PATTERN_TO_PRIMARY` pick) rather than the
    role-boosted ``Table`` candidate — that's the headline-wiring
    fix introduced after the b213fac1 / 753:27069 trace, where
    every ``Table/Column`` agenda row shipped ``Table`` in the
    headline despite ``primary_recommendation='TableColumn'`` at
    confidence 1.0. The role-bonus mechanism is still verified
    via ``candidates[0]``.
    """
    index, searcher = _two_candidate_setup("Table", "Panel")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Panel",
        region_role="table-column",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.candidates[0].name == "Table"
    assert mapping.suggested_component_name == "TableColumn"
    assert mapping.primary_recommendation == "TableColumn"
    assert mapping.primary_recommendation_confidence == 1.0


def test_hybrid_only_candidate_carries_semantic_example_breadcrumb_in_why_matched() -> None:
    """Hybrid-only candidates ship a ``semantic-example: <title>``
    breadcrumb in ``why_matched`` so the LLM can triage the
    semantic match.

    Pre-fix the hybrid path populated ``sources`` but left
    ``why_matched`` empty (BM25 contributes per-token overlap,
    hybrid doesn't), so any candidate that came in only through
    the embedding ranker looked indistinguishable from "no
    signal". The b213fac1 / 753:27069 trace produced rows like
    ``Navigation/Subheader`` → ``NavigationIcon`` with
    ``why_matched=[]`` and the agent discarded the suggestion.

    Post-fix the breadcrumb names the example title that anchored
    the hybrid hit, so the LLM has a verifiable handle.
    """
    # Distinct title so the assertion is unambiguous about which
    # example produced the breadcrumb.
    custom_hit = ExampleHit(
        component_name="NavBar",
        title="Sticky header with logo and primary tabs",
        code="<NavBar/>",
        imports=["NavBar"],
        score=1.0,
    )
    searcher = _StubHybridSearcher([custom_hit])
    # BM25 corpus has NavBar so the candidate exists, but the
    # query won't match any of NavBar's tokens — guaranteeing the
    # candidate enters fused ranking only via the hybrid path.
    index = Index(
        entities=[_component_entity("NavBar", summary="nav bar")],
        version="t",
    )
    graph = build_composition_graph(chunks=[], version="t")

    mapping = map_figma_node(
        node_name="Frame 632963",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )

    nav_bar = next(
        (c for c in mapping.candidates if c.name == "NavBar"), None
    )
    assert nav_bar is not None, (
        f"NavBar should appear in candidates via hybrid; "
        f"got {[c.name for c in mapping.candidates]!r}"
    )
    assert nav_bar.source == "hybrid"
    assert (
        "semantic-example: Sticky header with logo and primary tabs"
        in nav_bar.why_matched
    ), (
        f"hybrid-only candidate should carry a semantic-example "
        f"breadcrumb; got why_matched={nav_bar.why_matched!r}"
    )


def test_suggested_component_name_prefers_primary_recommendation_at_full_confidence() -> None:
    """When ``primary_recommendation`` is set at confidence 1.0,
    ``suggested_component_name`` reports the deterministic pick
    rather than the RRF-fused ``candidates[0].name``.

    The b213fac1 / 753:27069 trace surfaced this gap: every
    ``Table/Column`` agenda row had
    ``primary_recommendation='TableColumn'`` at confidence 1.0
    AND ``candidates[0].name='Table'`` (because BM25 surfaces
    ``Table`` for any query containing the token ``"table"``).
    Pre-fix the headline shipped ``Table``, masking the
    higher-confidence pick. Post-fix the headline ships the
    primary pick so the LLM's first-glance read of
    :attr:`FigmaNodeMapping.suggested_component_name` matches
    the deterministic ground truth.
    """
    index, searcher = _two_candidate_setup("Table", "Panel")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Panel",
        region_role="table-column",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.suggested_component_name == "TableColumn"
    assert mapping.candidates[0].name == "Table"


def test_suggested_component_name_falls_back_to_top_candidate_when_no_primary() -> None:
    """When no pattern role is supplied (``region_role=None``),
    ``primary_recommendation`` stays ``None`` and the headline
    falls back to ``candidates[0].name`` — the v1 behaviour for
    non-pattern regions like ``frame`` / ``composed-region`` /
    ``layout-container``.
    """
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.primary_recommendation is None
    assert mapping.suggested_component_name == mapping.candidates[0].name


def test_region_role_with_unknown_role_does_not_boost() -> None:
    """Roles outside :data:`ROLE_TO_COMPONENT_SYNONYMS` (``"frame"``
    / ``"text"`` / random strings) leave the fused ranking
    untouched.

    This is the guard against drive-by changes to the synonym map
    that accidentally widen the boost to noisy roles.
    """
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        region_role="frame",  # not in the synonyms map
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    names = [c.name for c in mapping.candidates]
    assert names.index("Card") < names.index("Tile")


def test_region_role_composed_region_boosts_layout_components_only() -> None:
    """``region_role='composed-region'`` boosts layout-family
    components (``ContainerLayout``/``StackingLayout``/
    ``FlexLayout``) but NOT non-layout components like
    ``Tile``/``Card``/``Modal`` — those live in
    :data:`SHAPE_BUCKET_TO_COMPONENT_SYNONYMS` instead so we
    don't double-boost when both signals point the same way.

    Without this boost a ``composed-region`` whose name suggests
    a layout (e.g. ``"Frame 632963"`` containing a horizontal
    flex strip) ranked ``FrameLogoIcon`` first because the BM25
    token ``"frame"`` outweighed the layout signal — see the
    b213fac1 / 753:27069 trace's ``Subpage``/``Frame 632934``
    rows.
    """
    # Tie BM25 ("Card") with hybrid ("FlexLayout") so the role
    # bonus is the only thing that can flip the order.
    index, searcher = _two_candidate_setup("FlexLayout", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        region_role="composed-region",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    names = [c.name for c in mapping.candidates]
    assert names.index("FlexLayout") < names.index("Card"), (
        f"composed-region should boost FlexLayout above Card; "
        f"got order={names!r}"
    )


def test_region_role_layout_container_boosts_layout_components() -> None:
    """``region_role='layout-container'`` lifts layout-family
    Prism components above an equally-scored generic candidate.
    """
    index, searcher = _two_candidate_setup("StackingLayout", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        region_role="layout-container",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    names = [c.name for c in mapping.candidates]
    assert names.index("StackingLayout") < names.index("Card"), (
        f"layout-container should boost StackingLayout above Card; "
        f"got order={names!r}"
    )


def test_region_role_none_matches_v1_byte_for_byte() -> None:
    """Passing ``region_role=None`` reproduces v1's pure-RRF
    candidates list bit-for-bit — the additive-only guarantee that
    Slice 12 promised when this kwarg was introduced.
    """
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    kwargs = dict(
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    v1 = map_figma_node(node_name="Card", **kwargs)
    explicit_none = map_figma_node(
        node_name="Card", region_role=None, **kwargs
    )
    assert [
        (c.name, c.score, c.source) for c in v1.candidates
    ] == [
        (c.name, c.score, c.source) for c in explicit_none.candidates
    ]


def test_region_shape_bucket_modal_lifts_dialog_above_card() -> None:
    """``region_shape_bucket="modal"`` adds +0.05 to ``Dialog``, just
    enough to overtake a fused-tie ``Card`` (smaller boost than
    role's, but still decisive on a clean tie).
    """
    index, searcher = _two_candidate_setup("Dialog", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    with_bucket = map_figma_node(
        node_name="Card",
        region_shape_bucket="modal",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert with_bucket.suggested_component_name == "Dialog"


def test_role_and_shape_bucket_bonuses_stack() -> None:
    """When the role AND shape-bucket synonym sets BOTH contain the
    same candidate, the bonuses stack (+0.15 + +0.05 = +0.20) and
    that candidate wins over an even-stronger BM25-only runner-up.

    Constructs a runner-up with a TWO-hit BM25 head start (≈ +0.16
    on its own) so that only the *stacked* bonus can overtake it.
    """
    # Make "Other" win on BM25 alone by repeating its lexical hit.
    index = Index(
        entities=[
            _component_entity("Tile"),
            _component_entity(
                "OtherTileWidget", summary="other component widget thing"
            ),
        ],
        version="t",
    )
    searcher = _StubHybridSearcher(
        [_hit("<X/>", component_name="Tile")]
    )
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        # The lexical query will only match the runner-up's summary
        # tokens; the hybrid hit covers Tile.
        node_name="other widget thing",
        region_role="kpi-tile",  # +0.15 to "Tile"
        region_shape_bucket="tile",  # +0.05 to "Tile"
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.suggested_component_name == "Tile"


def test_normalise_component_name_strips_punct_and_lowercases() -> None:
    """The synonym lookup uses normalised names; verify the helper
    handles slashes, hyphens, and mixed case the same way the
    walker emits component names.
    """
    assert _normalise_component_name("Action/Button") == "actionbutton"
    assert _normalise_component_name("Stat-Card") == "statcard"
    assert _normalise_component_name("KPI Tile v2") == "kpitilev2"
    assert _normalise_component_name("") == ""


# --------------------------------------------------------------------------
# Layer B — primary_recommendation derived from PATTERN_TO_PRIMARY.
# --------------------------------------------------------------------------


def test_primary_recommendation_set_for_pattern_role() -> None:
    """``region_role='kpi-tile'`` populates
    ``primary_recommendation='Tile'`` with confidence 1.0 and a
    descriptive rationale.
    """
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        region_role="kpi-tile",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.primary_recommendation == "Tile"
    assert mapping.primary_recommendation_confidence == 1.0
    assert "kpi-tile" in mapping.primary_recommendation_rationale


def test_primary_recommendation_none_for_unmapped_role() -> None:
    """Roles outside :data:`PATTERN_TO_PRIMARY` leave
    ``primary_recommendation`` as ``None`` with empty rationale and
    zero confidence (the v1 default).
    """
    index, searcher = _two_candidate_setup("Tile", "Card")
    graph = build_composition_graph(chunks=[], version="t")
    mapping = map_figma_node(
        node_name="Card",
        region_role="composed-region",
        index=index,
        hybrid_searcher=searcher,
        composition_graph=graph,
        color_token_index=_color_index([]),
        a11y_rules=_a11y_rules(),
    )
    assert mapping.primary_recommendation is None
    assert mapping.primary_recommendation_rationale == ""
    assert mapping.primary_recommendation_confidence == 0.0
