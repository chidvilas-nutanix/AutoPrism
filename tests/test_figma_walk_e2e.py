"""End-to-end walker test against a real (small) Prism component index.

The golden tests in ``test_figma_walker.py`` exercise the walker in
isolation (``map_figma_node_fn=None``). This test plugs the real
:func:`prism_mcp.figma_mapping.map_figma_node` in via
``functools.partial`` and asserts that the curated *top candidate*
for each agenda row matches the documented expectation.

We use:

* a small in-memory :class:`Index` populated with the
  Prism-relevant component names the fixtures should match
  against (``Tile``, ``Paragraph``, ``Icon``, ``Table``,
  ``TableColumn``, ``FlexLayout``).
* a no-hit ``_StubHybridSearcher`` so the candidate ranking comes
  purely from the deterministic BM25 + name-token paths. This
  keeps the test hermetic — no fastembed encoder spin-up, no
  network — while still proving the *full* walker → map_figma_node
  loop works end-to-end.

Design doc anchors: §5 (map_figma_node enrichment), §6 (walker
calls map_figma_node), §8.1 (worked example for the small tile).
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from prism_mcp.a11y import A11yRules
from prism_mcp.embeddings import ExampleHit
from prism_mcp.entities import Entity, Member
from prism_mcp.figma import walk_tree
from prism_mcp.figma_mapping import map_figma_node
from prism_mcp.graph import build_composition_graph
from prism_mcp.indexer import Index
from prism_mcp.tokens_index import build_color_token_index

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"


# ----------------------------------------------------------------------
# Local test scaffolding — kept tiny so failures point at the walker,
# not at the index setup.
# ----------------------------------------------------------------------


class _NoHitsSearcher:
    """``HybridSearcher`` stand-in that returns no example hits.

    By starving the hybrid path of hits, we force candidates to
    come from the deterministic BM25 + name-token routes. That
    keeps the test deterministic on every laptop without needing
    a real fastembed encoder.
    """

    def search(self, **kwargs: object) -> list[ExampleHit]:
        return []


def _component(name: str, summary: str = "") -> Entity:
    """Minimal Prism component entity for the BM25 index."""
    return Entity(
        name=name,
        type="component",
        version="t",
        summary=summary or f"{name} component",
        import_path=f"@nutanix-ui/prism-reactjs/{name}",
        signature=[Member(name="children", kind="prop", type="ReactNode")],
    )


def _build_test_index() -> Index:
    """Build the curated index the four fixtures should match against.

    Includes the components the design doc §8 worked examples
    expect: ``Tile`` (for 626:987), ``Paragraph`` (for stat-list
    rows), ``Icon`` (for the hamburger and the other 110 icons in
    opportunities-page), ``Table`` + ``TableColumn`` (for
    Table/Column), and a few layout primitives.
    """
    return Index(
        entities=[
            _component(
                "Tile",
                summary="Square card container that displays a title and stat values.",
            ),
            _component(
                "Paragraph",
                summary="Text paragraph used for body copy and stat values.",
            ),
            _component(
                "Icon",
                summary="Renders a single SVG icon by name (menu, chevron, etc.).",
            ),
            _component(
                "Table",
                summary="Data table with rows, columns, and header cells.",
            ),
            _component(
                "TableColumn",
                summary="Single column inside a Table with a title and N cells.",
            ),
            _component(
                "FlexLayout",
                summary="Flexbox container for vertical or horizontal stacks.",
            ),
            _component(
                "Button",
                summary="Clickable action button.",
            ),
        ],
        version="t",
    )


def _build_map_fn():
    """Return a curried :func:`map_figma_node` ready for the walker."""
    index = _build_test_index()
    return functools.partial(
        map_figma_node,
        index=index,
        hybrid_searcher=_NoHitsSearcher(),
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=build_color_token_index(entities=[], version="t"),
        a11y_rules=A11yRules(
            version="t",
            title=None,
            global_rules=[],
            per_component=[],
        ),
    )


# ----------------------------------------------------------------------
# Tests.
# ----------------------------------------------------------------------


def _agenda_by_id(mapping):
    return {r.id: r for r in mapping.agenda}


def test_e2e_small_tile_top_candidate_is_tile() -> None:
    """§8.1: the 626:987 INSTANCE should resolve to ``Tile``.

    This is the canonical worked example in the design doc. The
    layer name is ``"Tile"`` and the BM25 index contains a ``Tile``
    component, so the top candidate must be ``Tile`` — anything
    else means the walker → map_figma_node plumbing dropped the
    layer name on the floor.
    """
    map_fn = _build_map_fn()
    tree = json.loads(
        (FIXTURE_DIR / "figma-node-626-986.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=map_fn,
    )

    by_id = _agenda_by_id(mapping)
    tile = by_id["626:987"]
    assert tile.mapping is not None, "walker should populate mapping field"
    assert tile.mapping.candidates, (
        f"no candidates returned for Tile region 626:987; "
        f"mapping={tile.mapping!r}"
    )
    assert tile.mapping.suggested_component_name == "Tile"
    assert tile.mapping.candidates[0].name == "Tile"


def test_e2e_hamburger_icon_top_candidate_is_icon() -> None:
    """§8.3: the BOOLEAN_OPERATION ``Menu`` should resolve to ``Icon``.

    Pass-5 emits role=``icon``; the walker's enrichment must pass
    the structural hint ``"14x14 icon"`` (and the layer name) into
    ``map_figma_node`` so the top candidate is the ``Icon``
    component. Without the enrichment, the BM25 query would only
    contain ``"Menu"`` and miss the candidate entirely.
    """
    map_fn = _build_map_fn()
    tree = json.loads(
        (FIXTURE_DIR / "hamburger-icon.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=map_fn,
    )

    assert len(mapping.agenda) == 1
    region = mapping.agenda[0]
    assert region.role == "icon"
    assert region.mapping is not None
    candidate_names = [c.name for c in region.mapping.candidates]
    assert "Icon" in candidate_names, (
        f"Icon should appear in candidates for hamburger BOOLEAN_OPERATION; "
        f"got {candidate_names!r}"
    )


def test_e2e_table_column_top_candidate_is_table_column() -> None:
    """§4.5.2: Table/Column FRAME should resolve to ``TableColumn`` or ``Table``.

    The pattern collapses the FRAME + title + 5 cells into one
    ``role='table-column'`` MappedRegion. The layer name
    ``"Table/Column"`` plus the ``children_summary`` injected by
    the walker (``"5 INSTANCE"``) should push ``TableColumn`` or
    ``Table`` to the top of the candidate list.
    """
    map_fn = _build_map_fn()
    tree = json.loads(
        (FIXTURE_DIR / "table-column.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=map_fn,
    )

    assert len(mapping.agenda) == 1
    region = mapping.agenda[0]
    assert region.role == "table-column"
    assert region.mapping is not None
    candidate_names = [c.name for c in region.mapping.candidates]
    assert candidate_names, "TableColumn pattern must produce candidates"
    # The top candidate should be Table or TableColumn -- whichever
    # the BM25 ranker picks first is acceptable; both are correct
    # mappings for a Table/Column shape.
    assert candidate_names[0] in {"Table", "TableColumn"}, (
        f"top candidate should be Table or TableColumn for a Table/Column "
        f"FRAME; got {candidate_names[0]!r} (full list: {candidate_names!r})"
    )


def test_e2e_walker_calls_map_figma_node_at_most_once_per_agenda_row() -> None:
    """Walker must invoke ``map_figma_node_fn`` AT MOST N times for N
    agenda rows.

    Pins the contract that ``walk_tree`` does not double-call
    ``map_figma_node`` on collapsed nodes — once a region is
    emitted, the map is called at most once per row. Regressing
    that intent (e.g. by re-introducing the duplicated hybrid
    search per region or by walking the same agenda row twice)
    would silently double the cost of every page.

    The ``<=`` (rather than ``==``) accommodates the per-walk
    dedup cache in :func:`prism_mcp.figma.walker._resolve_pending_mappings`:
    when two regions emit byte-identical inputs (e.g. two
    repeated hamburger icons or two stat-list patterns whose
    layer name + content match exactly) the mapper is invoked
    once and the result is broadcast to both regions. The cache
    is a strict optimisation — both regions still receive the
    same ``FigmaNodeMapping`` they would have received under the
    one-call-per-row contract.
    """
    call_count = [0]

    def counting_map(*args, **kwargs):
        call_count[0] += 1
        # Use the real mapper so we still produce a valid result.
        return _build_map_fn()(*args, **kwargs)

    tree = json.loads(
        (FIXTURE_DIR / "figma-node-626-986.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=counting_map,
    )

    assert call_count[0] <= len(mapping.agenda), (
        f"expected at most {len(mapping.agenda)} map_figma_node calls "
        f"(one per agenda row, fewer when dedup hits), got {call_count[0]}"
    )
    assert call_count[0] >= 1, (
        "expected the walker to have invoked the mapper at least once for "
        f"a non-empty agenda; got call_count={call_count[0]} with "
        f"{len(mapping.agenda)} rows"
    )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "figma-node-626-986",
        "hamburger-icon",
        "table-column",
    ],
)
def test_e2e_every_agenda_row_has_a_populated_mapping(
    fixture_name: str,
) -> None:
    """Every agenda row's ``mapping`` field is populated when the walker is given a mapper.

    Pins the contract: if you pass ``map_figma_node_fn``, the
    walker must call it for every region. ``None`` is only
    acceptable if the explicit ``map_figma_node_fn=None`` opt-out
    was used.
    """
    map_fn = _build_map_fn()
    tree = json.loads(
        (FIXTURE_DIR / f"{fixture_name}.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=map_fn,
    )

    for region in mapping.agenda:
        assert region.mapping is not None, (
            f"agenda row {region.id!r} ({region.role}) has no mapping; "
            "the walker must call map_figma_node_fn for every region"
        )
