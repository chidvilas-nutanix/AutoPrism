"""Walker integration tests against curated golden fixtures.

Each fixture ``tests/fixtures/figma/<name>.json`` is paired with
a ``<name>.expected.json`` that captures the stable shape of the
walker's output (summary, agenda IDs+roles+names, layout topology,
and the drop-by-reason histogram). The walker is exercised with
``map_figma_node_fn=None`` so the test is independent of the live
Prism library index — the e2e candidate-quality test lives in
``test_figma_walk_e2e.py``.

Regenerating goldens
---------------------

If the walker changes intentionally, regenerate the goldens with::

    pytest tests/test_figma_walker.py --update-figma-golden

and review the resulting diff before committing.

Design doc anchors: §4.6 (agenda + layout-tree split), §8.1-8.3
(worked examples), §11.3 (golden fixtures).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from prism_mcp.figma import FigmaTreeMapping, walk_tree

# Anchored relative to this test file so the suite stays portable.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"


# ----------------------------------------------------------------------
# Fixture discovery.
# ----------------------------------------------------------------------


def _discover_fixtures() -> list[Path]:
    """Return all ``*.json`` Figma fixtures excluding the goldens."""
    return sorted(
        p
        for p in FIXTURE_DIR.glob("*.json")
        if not p.name.endswith(".expected.json")
    )


# ----------------------------------------------------------------------
# Golden representation.
# ----------------------------------------------------------------------


def _to_golden(mapping: FigmaTreeMapping) -> dict[str, Any]:
    """Project a :class:`FigmaTreeMapping` down to the golden shape.

    The ``layout_tree`` projection includes
    ``direction`` from :class:`LayoutAnalysis` so future
    regressions in :mod:`prism_mcp.figma.layout_inference` show up
    as concrete byte-level diffs (rather than only triggering the
    spot-check assertions). All other layout-analysis fields stay
    behind the spot-checks to keep the golden small.
    """
    layout_rows: list[dict[str, Any]] = []
    for node in mapping.layout_tree:
        row: dict[str, Any] = {
            "id": node.id,
            "role": node.role,
            "name": node.name,
            "children_ids": node.children_ids,
        }
        if node.layout is not None and node.layout.direction is not None:
            row["layout_direction"] = node.layout.direction
        layout_rows.append(row)
    return {
        "summary": mapping.summary,
        "agenda": [
            {
                "id": region.id,
                "role": region.role,
                "name": region.name,
            }
            for region in mapping.agenda
        ],
        "layout_tree": layout_rows,
        "dropped_by_reason": dict(
            Counter(item.reason for item in mapping.dropped)
        ),
        "warnings": list(mapping.warnings),
    }


def _read_golden(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_golden(path: Path, golden: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(golden, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# Generic golden-comparison test (one parametrised case per fixture).
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_path",
    _discover_fixtures(),
    ids=lambda p: p.stem,
)
def test_walk_tree_matches_golden(
    fixture_path: Path,
    update_figma_golden: bool,
) -> None:
    """The walker output for each fixture matches the committed golden.

    The walker is deterministic given a fixed input — same JSON in,
    same agenda/layout/dropped histogram out. This test pins that
    invariant for every fixture in ``tests/fixtures/figma/``. When
    intentionally changing walker behaviour, regenerate the goldens
    with ``pytest --update-figma-golden``.
    """
    tree = json.loads(fixture_path.read_text(encoding="utf-8"))
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    actual = _to_golden(mapping)

    expected_path = fixture_path.with_suffix(".expected.json")

    if update_figma_golden:
        _write_golden(expected_path, actual)
        pytest.skip(
            f"--update-figma-golden: regenerated {expected_path.name}; "
            "review the diff before committing"
        )

    assert expected_path.exists(), (
        f"missing golden {expected_path}; regenerate with "
        "pytest --update-figma-golden"
    )

    expected = _read_golden(expected_path)
    assert actual == expected, (
        f"walker output diverged from golden for {fixture_path.name}; "
        "regenerate intentionally with pytest --update-figma-golden"
    )


# ----------------------------------------------------------------------
# Spot-check assertions per worked example (design doc §8.1-§8.3).
# Each test pins a *specific* claim made in the design doc so a
# golden regeneration accident can't silently break the documented
# behaviour.
# ----------------------------------------------------------------------


def test_small_tile_fixture_emits_tile_plus_two_stat_lists() -> None:
    """§8.1: 626:986 must produce one tile + two stat-list regions.

    This pins the qualitative claim of the worked example separately
    from the byte-for-byte golden comparison above. If the walker
    grows new patterns in the future, the byte-level golden will
    diff but this assertion holds.
    """
    tree = json.loads(
        (FIXTURE_DIR / "figma-node-626-986.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    roles = [r.role for r in mapping.agenda]
    assert "instance" in roles, "tile (626:987) should be mapped as instance"
    assert roles.count("stat-list") == 2, (
        "expected 2 stat-list rows for Cluster Details + Cluster Details Copy"
    )
    agenda_ids = {r.id for r in mapping.agenda}
    assert {"626:987", "626:988", "626:999"} <= agenda_ids


def test_hamburger_icon_fixture_collapses_to_single_icon_region() -> None:
    """§8.3: 3-stripe hamburger BOOLEAN_OPERATION collapses to 1 row.

    The pass-5 icon coalescer must absorb all three RECTANGLE
    children with reason ``icon_internal`` and produce a single
    MappedRegion with ``role='icon'``. This is a high-frequency
    case (every page has 50-100 icons); regressing it would blow
    up the agenda size.
    """
    tree = json.loads(
        (FIXTURE_DIR / "hamburger-icon.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert len(mapping.agenda) == 1
    assert mapping.agenda[0].role == "icon"
    assert mapping.agenda[0].name == "Menu"

    icon_drops = [d for d in mapping.dropped if d.reason == "icon_internal"]
    assert len(icon_drops) == 3, (
        "all 3 rectangle strokes should be folded into the icon"
    )


def test_table_column_fixture_collapses_to_single_table_column() -> None:
    """§4.5.2: Table/Column FRAME with N Cells collapses to 1 row.

    The column-of-cells pattern must produce exactly one
    MappedRegion with ``role='table-column'`` and absorb the
    header + every cell. Regressing this would force the LLM to
    instantiate N <TableColumn> rows separately — exactly the
    duplication the pattern was designed to prevent.
    """
    tree = json.loads(
        (FIXTURE_DIR / "table-column.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert len(mapping.agenda) == 1
    assert mapping.agenda[0].role == "table-column"
    assert mapping.agenda[0].id == "8000:1"


def test_x_ray_3_table_column_instances_absorbed_by_pattern_guard() -> None:
    """X-Ray-3 Fix A + Fix B spot-check on the X-Ray Master File's
    Results / Progress / Empty artboard.

    Empirically observed compression timeline for this 654-node page:

    * **Pre-Fix-A** (FRAME-only pattern guard, walker descends into
      every INSTANCE subtree): **297** agenda rows, **0** ``table-
      column`` matches. Every cell escaped into the agenda and rank-
      matched against generic ``Table``.
    * **Post-Fix-A** (INSTANCE-friendly pattern guard, walker still
      descends into INSTANCE subtrees): **51** agenda rows, **9**
      ``table-column`` matches.
    * **Post-Fix-A + Fix-B** (INSTANCE-friendly pattern guard + walker
      respects ``map_and_stop`` semantically): **26** agenda rows,
      **9** ``table-column`` matches, **zero** inherited-descendant
      ``I<inst>;<master>`` IDs leak through.

    See ``docs/x-ray-walker-investigation.md`` §8 ("Fix A — pattern
    guards") and §12 ("Fix B — walker respects map_and_stop").

    Bounds are conservative in the direction of the improvement so
    cosmetic walker tweaks that shuffle a handful of agenda rows
    don't trip the assertion.
    """
    mapping = _walk_real_fixture("x-ray-3-results-progress-empty.json")

    assert mapping.summary["input_nodes"] >= 600, (
        "x-ray-3 fixture should still be a large-scale page"
    )
    assert mapping.summary["agenda_size"] <= 50, (
        f"agenda_size={mapping.summary['agenda_size']}; Fix A + Fix B "
        "were expected to drop X-Ray-3 from 297 to ~26; if this is "
        "back above 50 either the pattern's INSTANCE acceptance or "
        "the map_and_stop short-circuit has regressed"
    )
    table_column_rows = [
        r for r in mapping.agenda if r.role == "table-column"
    ]
    assert len(table_column_rows) >= 5, (
        f"expected >=5 table-column rows post-Fix-A, got "
        f"{len(table_column_rows)}; the X-Ray Master File renders "
        "each column as a Table/Column INSTANCE and the pattern "
        "guard must accept INSTANCE alongside FRAME"
    )
    # Defect B regression guard: inherited-descendant IDs of the
    # Figma form ``I<instance>;<master>;...`` should never appear
    # in the agenda. They are the canonical signature of "walker
    # descended into an INSTANCE subtree it should have stopped at".
    inherited = [
        r.id for r in mapping.agenda if ";" in r.id and r.id.startswith("I")
    ]
    assert not inherited, (
        f"inherited-descendant IDs leaked into the agenda: {inherited[:3]} "
        "(showing first 3); Fix B (RouterDecision.map_and_stop short-"
        "circuit on INSTANCE in walker._visit) has regressed"
    )


def test_x_ray_4_table_column_instances_absorbed_by_pattern_guard() -> None:
    """X-Ray-4 Fix A + Fix B spot-check on the Gold Image Configuration
    artboard.

    Empirically observed compression timeline for this 2,254-node page:

    * **Pre-Fix-A**: **632** agenda rows, **2** ``table-column``
      matches (the one-off FRAME columns).
    * **Post-Fix-A**: **326** agenda rows, **14** ``table-column``
      matches.
    * **Post-Fix-A + Fix-B**: **43** agenda rows, **14** ``table-
      column`` matches, **zero** inherited-descendant IDs.

    Together the two fixes compress this page by **14.7×** without
    losing any of the structural anchors the LLM needs.
    """
    mapping = _walk_real_fixture("x-ray-4-gold-image-list.json")

    assert mapping.summary["input_nodes"] >= 2000, (
        "x-ray-4 fixture should still be a large-scale page"
    )
    assert mapping.summary["agenda_size"] <= 50, (
        f"agenda_size={mapping.summary['agenda_size']}; Fix A + Fix B "
        "were expected to drop X-Ray-4 from 632 to ~43; Fix C caps "
        "the result at max_agenda=50; if this is back above 50 then "
        "either pattern absorption, the map_and_stop short-circuit, "
        "or the agenda-truncation hard cap has regressed"
    )
    table_column_rows = [
        r for r in mapping.agenda if r.role == "table-column"
    ]
    assert len(table_column_rows) >= 10, (
        f"expected >=10 table-column rows post-Fix-A, got "
        f"{len(table_column_rows)}; the Gold Image Configuration "
        "page uses both INSTANCE and FRAME columns and the pattern "
        "guard must accept INSTANCE alongside FRAME"
    )
    inherited = [
        r.id for r in mapping.agenda if ";" in r.id and r.id.startswith("I")
    ]
    assert not inherited, (
        f"inherited-descendant IDs leaked into the agenda: {inherited[:3]} "
        "(showing first 3); Fix B (RouterDecision.map_and_stop short-"
        "circuit on INSTANCE in walker._visit) has regressed"
    )


def test_x_ray_3_agenda_truncated_to_max_agenda_hard_cap() -> None:
    """Fix C: ``max_agenda`` is now a HARD cap.

    Pre-Fix-C the cap was a warning-only soft limit and the walker
    happily handed the LLM 297 / 632 agenda rows for the two
    X-Ray Master File pages. Fix C makes the trim mandatory and
    audits the dropped rows under
    :attr:`DropReason.agenda_truncated` so consumers see *why* the
    agenda was clipped. See ``docs/x-ray-walker-investigation.md``
    §8 "Fix C".
    """
    mapping = _walk_real_fixture("x-ray-4-gold-image-list.json")
    assert mapping.summary["agenda_size"] <= 50, (
        f"agenda_size={mapping.summary['agenda_size']}; Fix C "
        "guarantees the agenda is truncated to max_agenda=50 "
        "after the DFS regardless of how many regions the walker "
        "emitted"
    )


def test_fix_b_does_not_short_circuit_frame_with_configured_descendants() -> None:
    """Regression — a FRAME whose name uses a slash convention
    (``Modal/Fullpage``, ``Card/Normal``, etc.) but whose
    descendants are configured product-page content (regular Figma
    IDs, NOT the inherited ``I<inst>;<master>;<sub>`` library
    format) MUST be walked, not short-circuited.

    The pre-guard implementation broke real product pages that
    used slash names as an organisational convention — the
    Channel Insights "Elevate Data" page (Jun 2026 reproduction)
    had a ``Modal/Fullpage`` FRAME wrapping its certificate table
    + 1,078 configured descendants. Fix B's blanket short-circuit
    swallowed all 1,078 into ``content_slots`` and the LLM lost
    the table structure entirely.

    See ``docs/x-ray-walker-investigation.md`` §8 + §13
    ("Channel Insights regression").
    """
    tree = {
        "id": "563:36148",
        "name": "Modal/Fullpage",
        "type": "FRAME",
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 1024,
            "height": 800,
        },
        "visible": True,
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 1, "g": 1, "b": 1, "a": 1},
            }
        ],
        "children": [
            {
                "id": "563:36200",
                "name": "Table",
                "type": "FRAME",
                "absoluteBoundingBox": {
                    "x": 100,
                    "y": 100,
                    "width": 800,
                    "height": 600,
                },
                "visible": True,
                "fills": [
                    {
                        "type": "SOLID",
                        "color": {"r": 0.95, "g": 0.95, "b": 0.95, "a": 1},
                    }
                ],
                "children": [
                    {
                        "id": f"563:362{cell:02d}",
                        "name": f"Cell {cell}",
                        "type": "FRAME",
                        "absoluteBoundingBox": {
                            "x": 100 + cell * 80,
                            "y": 100,
                            "width": 80,
                            "height": 50,
                        },
                        "visible": True,
                        "fills": [
                            {
                                "type": "SOLID",
                                "color": {
                                    "r": 0.9,
                                    "g": 0.9,
                                    "b": 0.9,
                                    "a": 1,
                                },
                            }
                        ],
                        "children": [
                            {
                                "id": f"563:363{cell:02d}",
                                "name": "label",
                                "type": "TEXT",
                                "characters": f"Column {cell}",
                                "absoluteBoundingBox": {
                                    "x": 100 + cell * 80 + 10,
                                    "y": 110,
                                    "width": 60,
                                    "height": 30,
                                },
                                "visible": True,
                            },
                        ],
                    }
                    for cell in range(1, 10)
                ],
            },
        ],
    }
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    assert mapping.summary["agenda_size"] >= 3, (
        f"agenda_size={mapping.summary['agenda_size']}; the walker "
        "should have walked into the slash-named FRAME and emitted "
        "regions for its inner Table + cells, not collapsed to one "
        "row via Fix B's short-circuit"
    )
    table_rows = [
        r for r in mapping.agenda if "Table" in r.name or "Cell" in r.name
    ]
    assert table_rows, (
        f"expected at least one Table/Cell agenda row; "
        f"agenda names: {[r.name for r in mapping.agenda]!r}"
    )


def test_fix_b_still_short_circuits_when_descendants_are_inherited() -> None:
    """Sibling of the above — verify the guard does NOT disable
    Fix B for the case Fix B was originally built for: a FRAME
    whose slash name AND descendants both signal "library
    component instance" (descendants have ``I<inst>;<master>``
    inherited IDs).

    This pins Fix B's continued benefit on X-Ray Master Files.
    """
    tree = {
        "id": "100:1",
        "name": "Table/Column",
        "type": "INSTANCE",
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 200,
            "height": 400,
        },
        "visible": True,
        "children": [
            {
                "id": f"I100:{1 + i};master:{i}",
                "name": f"inner-{i}",
                "type": "TEXT",
                "characters": f"row {i}",
                "absoluteBoundingBox": {
                    "x": 0,
                    "y": i * 40,
                    "width": 200,
                    "height": 40,
                },
                "visible": True,
            }
            for i in range(20)
        ],
    }
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    assert mapping.summary["agenda_size"] == 1, (
        f"agenda_size={mapping.summary['agenda_size']}; an INSTANCE "
        "whose 20 descendants are all inherited (I<inst>;<master>) "
        "must still short-circuit so the library internals don't "
        "flood the agenda"
    )
    inherited_rows = [
        r for r in mapping.agenda if ";" in r.id and r.id.startswith("I")
    ]
    assert not inherited_rows, (
        f"inherited descendants must not appear in the agenda: "
        f"{inherited_rows!r}"
    )


def test_fix_d_drops_variant_alternatives_with_shared_slash_prefix() -> None:
    """Fix D: three sibling FRAMEs named ``Modal/Empty`` /
    ``Modal/Filled`` / ``Modal/Error`` laid out side-by-side under
    one parent should collapse to one agenda row plus two audit
    rows under :attr:`DropReason.variant_alternative`. See
    ``docs/x-ray-walker-investigation.md`` §11.5 + §12 "Fix D".
    """
    tree = {
        "id": "0:1",
        "name": "X-Ray Master",
        "type": "FRAME",
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 1200,
            "height": 400,
        },
        "visible": True,
        "children": [
            {
                "id": f"1:{i + 1}",
                "name": f"Modal/{state}",
                "type": "FRAME",
                "absoluteBoundingBox": {
                    "x": i * 400,
                    "y": 0,
                    "width": 380,
                    "height": 400,
                },
                "visible": True,
                "children": [],
            }
            for i, state in enumerate(["Empty", "Filled", "Error"])
        ],
    }
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    modal_rows = [
        r
        for r in mapping.agenda
        if r.name.startswith("Modal/") and r.role != "composed-region"
    ]
    assert len(modal_rows) <= 1, (
        f"expected at most one Modal/* agenda row post-Fix-D, got "
        f"{len(modal_rows)}: {[(r.id, r.name) for r in modal_rows]}"
    )
    variant_drops = [
        d
        for d in mapping.dropped
        if d.reason == "variant_alternative"
    ]
    assert variant_drops, (
        "expected at least one DroppedNode with reason "
        "'variant_alternative' for the 3-way Modal stack"
    )


def test_fix_d_keeps_state_overlay_siblings_that_share_a_bbox() -> None:
    """Fix D MUST NOT fold sibling FRAMEs that share a bbox — those
    are state overlays (hover / selected / pressed) layered on top
    of a base, not alternative artboards laid out side-by-side.
    """
    tree = {
        "id": "0:1",
        "name": "Button",
        "type": "FRAME",
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 40,
        },
        "visible": True,
        "children": [
            {
                "id": f"1:{i + 1}",
                "name": f"Button/{state}",
                "type": "FRAME",
                "absoluteBoundingBox": {
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 40,
                },
                "visible": True,
                "children": [],
            }
            for i, state in enumerate(["Default", "Hover", "Pressed"])
        ],
    }
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    variant_drops = [
        d
        for d in mapping.dropped
        if d.reason == "variant_alternative"
    ]
    assert not variant_drops, (
        "Fix D folded overlapping state-overlay siblings; that's "
        f"the documented failure mode: {variant_drops!r}"
    )


def test_fix_c_drop_reason_appears_in_histogram_when_truncation_fires() -> None:
    """Fix C: when truncation fires, the drop histogram MUST surface
    ``dropped_agenda_truncated`` so the LLM (and the
    ``audit_layer_b_agreement.py`` repro script) can detect the
    truncation. The d02 fixture is large enough to trip the cap.
    """
    mapping = _walk_real_fixture("figma-d02-share-summary.json")
    if mapping.summary["agenda_size"] >= 50:
        assert "dropped_agenda_truncated" in mapping.summary, (
            "Fix C must surface the agenda_truncated drop reason "
            "in the summary histogram whenever the agenda is "
            "clipped"
        )
        assert mapping.summary["dropped_agenda_truncated"] >= 1, (
            "expected at least one dropped_agenda_truncated entry "
            "in the histogram for the d02 fixture"
        )
        assert any(
            "truncated" in w for w in mapping.warnings
        ), "Fix C must also emit a human-readable warning"


def test_opportunities_page_collapses_278_nodes_to_under_50_regions() -> None:
    """§8.4: full page (278 nodes) compresses to a manageable agenda.

    The walker's whole reason for existing is to shrink raw Figma
    JSON down to an LLM-digestible plan. We assert the documented
    compression ratio holds: a 200-700 node real page produces
    at most ~50 agenda rows (the ``max_agenda`` soft cap).
    """
    tree = json.loads(
        (FIXTURE_DIR / "opportunities-page.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert mapping.summary["input_nodes"] >= 250, (
        "opportunities-page.json should be a realistic-scale fixture"
    )
    assert mapping.summary["agenda_size"] <= 50, (
        "agenda must stay under the max_agenda soft cap"
    )
    # The dropped histogram should be dominated by structural noise,
    # not by safety-rail bails (which would manifest as warnings).
    assert not mapping.warnings, (
        f"unexpected walker warnings: {mapping.warnings!r}"
    )


# ----------------------------------------------------------------------
# Real-world page regressions — pin the walker's behaviour against the
# three large Figma-basics fixtures so we never silently re-introduce
# the catastrophic "fold whole page into one kpi-tile" failure mode.
# ----------------------------------------------------------------------


def _walk_real_fixture(name: str):
    tree = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )


def _assert_not_collapsed_to_single_kpi_tile(mapping, fixture_name: str) -> None:
    """Shared sanity check for the three big page fixtures.

    The pre-fix failure mode was: a 400-node tree collapses to ONE
    agenda row with role ``kpi-tile`` because the
    :func:`prism_mcp.figma.patterns.match_kpi_tile` predicate matched
    the entire page. Any future regression that brings that mode back
    will trip one of these three asserts.
    """
    assert mapping.summary["agenda_size"] > 1, (
        f"{fixture_name}: agenda collapsed to {mapping.summary['agenda_size']} "
        "row(s); previously this was the kpi-tile over-match failure mode"
    )
    if mapping.agenda:
        roles = [r.role for r in mapping.agenda]
        kpi_root_match = (
            len(roles) == 1 and roles[0] == "kpi-tile"
        )
        assert not kpi_root_match, (
            f"{fixture_name}: page-root fell into a single kpi-tile; "
            "the leaf-pattern size cap was supposed to prevent this"
        )


def test_active_cluster_page_does_not_fold_to_single_kpi_tile() -> None:
    """The 1280x800 Active Cluster page (real Figma-basics fixture)
    must produce more than one agenda row.

    Pre-fix this fixture collapsed all 440 nodes into one
    ``kpi-tile`` row because the page contains exactly one ≥24pt TEXT
    ("Licenses") and a forest of ≤14pt body labels — the smoking-gun
    case for the predicate over-match. The Layer 1 size cap and
    Layer 3 page-scale gate together must keep this off.
    """
    mapping = _walk_real_fixture("figma-active-cluster-page.json")
    _assert_not_collapsed_to_single_kpi_tile(
        mapping, "figma-active-cluster-page.json"
    )
    # Sanity: we should see the table columns and the header pulled
    # out as their own agenda rows (they have strong name anchors).
    roles = [r.role for r in mapping.agenda]
    assert "table-column" in roles, (
        "active-cluster-page should pull out at least one "
        "Table/Column region"
    )


def test_e01_share_summary_does_not_fold_to_single_kpi_tile() -> None:
    """The 1280x890 E01 - Share Summary page (real Figma-basics
    fixture) must produce more than one agenda row.

    Same failure mode as the Active Cluster page — one ≥24pt TEXT
    ("Home" at 29pt) inside a page-scale FRAME was enough to swallow
    347 of 348 nodes into a single kpi-tile region pre-fix.
    """
    mapping = _walk_real_fixture("figma-e01-share-summary.json")
    _assert_not_collapsed_to_single_kpi_tile(
        mapping, "figma-e01-share-summary.json"
    )


def test_d02_share_summary_keeps_working() -> None:
    """The 1280x1179 D02 page is the case that already worked before
    the fix (no ≥24pt TEXT, so kpi-tile never fired); it should keep
    producing a non-trivial agenda after the fix lands.

    Pinning this fixture guards against the opposite regression:
    accidentally tightening match_kpi_tile so much that nothing
    matches anymore.
    """
    mapping = _walk_real_fixture("figma-d02-share-summary.json")
    _assert_not_collapsed_to_single_kpi_tile(
        mapping, "figma-d02-share-summary.json"
    )


def test_low_confidence_warning_surfaces_when_top_score_below_threshold() -> None:
    """A stubbed mapper that always returns ``score=0.1`` causes the
    walker to emit a ``low_confidence`` warning per emitted region.

    Pins the soft mitigation described in
    ``docs/handoff-spatial-and-ranker.md`` §3.4: low scores are
    surfaced as warnings (visible in
    :attr:`FigmaTreeMapping.warnings`) rather than as hard rail
    failures. The LLM consumes the warning string and can disclaim
    or fall back to atomic tools accordingly.
    """
    from prism_mcp.figma import walk_tree
    from prism_mcp.workflow.figma_mapping import (
        CandidateMatch,
        FigmaNodeMapping,
    )

    def _low_score_mapper(**kwargs: object) -> FigmaNodeMapping:
        return FigmaNodeMapping(
            node_name=str(kwargs.get("node_name", "")),
            suggested_component_name="Generic",
            candidates=[
                CandidateMatch(
                    name="Generic",
                    type="component",
                    score=0.1,
                    why_matched=[],
                    summary="",
                    source="bm25",
                )
            ],
        )

    tree = json.loads(
        (FIXTURE_DIR / "table-column.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=_low_score_mapper,
    )

    low_conf = [w for w in mapping.warnings if "low_confidence" in w]
    assert low_conf, (
        f"expected ≥1 low_confidence warning for a stubbed score=0.1 "
        f"mapper, got warnings={mapping.warnings!r}"
    )


def test_real_page_fixtures_do_not_trip_oversized_rail() -> None:
    """After the Layer 1 + Layer 3 fixes, no real-world fixture
    should reach Layer 2's absorb-ratio safety rail.

    The rail is a defence-in-depth guard; if it fires on
    well-behaved input that means a more specific predicate /
    page-scale gate failed and we need to tighten upstream. This
    test surfaces that early.
    """
    fixtures = [
        "figma-active-cluster-page.json",
        "figma-e01-share-summary.json",
        "figma-d02-share-summary.json",
    ]
    for fixture in fixtures:
        mapping = _walk_real_fixture(fixture)
        oversized = [
            d for d in mapping.dropped
            if d.reason == "pattern_oversized_reject"
        ]
        assert not oversized, (
            f"{fixture}: Layer 2 safety rail fired ({len(oversized)} "
            f"reject(s)); upstream predicate/page-scale gate should "
            f"have caught these earlier. First reject: {oversized[0]!r}"
        )


# ----------------------------------------------------------------------
# BoxStyle preservation — visual containers must keep their identity.
# ----------------------------------------------------------------------


def test_status_alert_banner_preserves_visual_identity() -> None:
    """The ``Status/Alert Banner`` FRAME (674:8200) in the
    ``d02-share-summary`` page is the canonical regression fixture
    for the "visual containers silently dropped" failure mode.

    Pre-fix: the FRAME had one FRAME child + a visible grey fill +
    ``cornerRadius=2``, so ``classify_frame_role`` returned
    ``layout-container`` and the walker emitted **nothing** for it
    — the grey rounded banner vanished from the agenda entirely.
    The user saw plain text in the generated React instead of the
    Cloud Connect Access info panel.

    Post-fix this test asserts:

    1. A region IS emitted with id ``674:8200``,
       role ``composed-region``.
    2. ``box_style.background_color`` carries the grey hex.
    3. ``box_style.corner_radius`` carries the 2px rounding.
    4. ``box_style.padding`` carries either the auto-layout
       ``paddingTop/Left/Right/Bottom`` (this banner happens to be
       VERTICAL auto-layout in the live design) OR the inferred
       15/20-pixel offsets between parent and child bboxes.
    """
    mapping = _walk_real_fixture("figma-d02-share-summary.json")
    banner_regions = [r for r in mapping.agenda if r.id == "674:8200"]
    assert banner_regions, (
        "Status/Alert Banner (674:8200) missing from agenda — the "
        "visual-container promotion in classify_frame_role regressed; "
        "FRAMEs with visible fills + cornerRadius must not classify "
        "as layout-container"
    )
    banner = banner_regions[0]
    assert banner.role == "composed-region", (
        f"Status/Alert Banner role={banner.role!r}, expected "
        "'composed-region' (visual-container promotion path)"
    )
    bs = banner.box_style
    assert bs.background_color == "#EDF0F2", (
        f"box_style.background_color={bs.background_color!r}, expected "
        "'#EDF0F2' (the visible grey fill on the banner)"
    )
    assert bs.corner_radius == 2.0, (
        f"box_style.corner_radius={bs.corner_radius!r}, expected 2.0 "
        "(the FRAME's cornerRadius field)"
    )
    assert bs.padding is not None, (
        "box_style.padding missing — auto-layout banner should expose "
        "paddingTop/Left/Right/Bottom; absolute-positioned ones should "
        "infer from parent-child bbox offsets"
    )
    t, r, b, _l = bs.padding
    assert 10 <= t <= 20 and 10 <= b <= 20, (
        f"box_style.padding top/bottom=({t},{b}) outside expected 10-20 "
        "px range — the banner's content sits 15px inside the parent"
    )
    assert 15 <= r <= 25 and 15 <= _l <= 25, (
        f"box_style.padding left/right=({_l},{r}) outside expected 15-25 "
        "px range — the banner's content sits 20px inside the parent"
    )
    # And the descriptive hints should mirror the structured fields
    # so BM25/dense rankers see the visual cues too.
    assert any("background" in h for h in banner.structural_hints), (
        f"structural_hints={banner.structural_hints} missing 'background' "
        "hint — _box_style_hints regressed"
    )
    assert any("rounded" in h for h in banner.structural_hints), (
        f"structural_hints={banner.structural_hints} missing 'rounded' "
        "hint — _box_style_hints regressed"
    )


def test_real_fixtures_carry_box_style_on_visual_containers() -> None:
    """Coarse health check across all three fixtures.

    The point of the visual-container fix is that any FRAME with
    visible fill / stroke / cornerRadius / shadow gets a
    :class:`MappedRegion` *and* surfaces its style in ``box_style``.
    A page-scale dashboard should have dozens of such regions; a
    near-zero count means the extraction silently broke.

    Threshold of ``>= 10`` is well below empirical counts (66 / 71 /
    317 across the three pages at time of writing) and well above
    "we lost all the box styles".
    """
    for fixture in (
        "figma-active-cluster-page.json",
        "figma-e01-share-summary.json",
        "figma-d02-share-summary.json",
    ):
        mapping = _walk_real_fixture(fixture)
        with_style = sum(
            1
            for r in mapping.agenda
            if r.box_style.model_dump(
                exclude_none=True, exclude_defaults=True
            )
        )
        assert with_style >= 10, (
            f"{fixture}: only {with_style} of {len(mapping.agenda)} "
            "agenda rows carry box_style; the visual-container "
            "promotion or extract_box_style helper regressed"
        )


# ----------------------------------------------------------------------
# Layer A — spatial layout inference real-world spot-checks.
# ----------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Spatial layout inference is temporarily disabled to keep "
        "the LLM-facing output compact while the X-Ray walker "
        "fixes land. See docs/x-ray-walker-investigation.md §13."
    )
)
def test_d02_banner_body_classifies_as_column() -> None:
    """The d02 Cloud Connect Settings banner body (674:8187) stacks
    three sub-regions top-to-bottom — the walker's
    :func:`analyze_layout` must identify this as a vertical flow.

    Pre-Layer-A the generator had to re-derive direction from
    bboxes; getting the classification right here is what lets the
    LLM emit ``flexDirection: column`` deterministically.
    """
    mapping = _walk_real_fixture("figma-d02-share-summary.json")
    node = next(
        (n for n in mapping.layout_tree if n.id == "674:8187"), None
    )
    assert node is not None, (
        "missing 674:8187 (Cloud Connect Settings); layout-inference "
        "spot-check needs this anchor"
    )
    assert node.layout is not None, (
        f"674:8187 has children_ids={node.children_ids} but no layout "
        "analysis; _attach_layout_analysis regressed"
    )
    assert node.layout.direction == "column", (
        f"674:8187 direction={node.layout.direction!r}, expected "
        "'column' — three children stacked top-to-bottom"
    )
    assert node.layout.confidence >= 0.7, (
        f"674:8187 confidence={node.layout.confidence}, expected >=0.7 "
        "for the canonical column case"
    )


@pytest.mark.skip(
    reason=(
        "Spatial layout inference is temporarily disabled to keep "
        "the LLM-facing output compact while the X-Ray walker "
        "fixes land. See docs/x-ray-walker-investigation.md §13."
    )
)
def test_active_cluster_dense_row_classifies_as_row() -> None:
    """The Active Cluster Status+Name row (624:7171) packs three
    children left-to-right at the same top edge — analyzeLayout
    must classify it as a row, not collapse to stack/single.
    """
    mapping = _walk_real_fixture("figma-active-cluster-page.json")
    node = next(
        (n for n in mapping.layout_tree if n.id == "624:7171"), None
    )
    assert node is not None, (
        "missing 624:7171 (Status + Name); the active-cluster "
        "fixture changed shape or the walker is dropping the region"
    )
    assert node.layout is not None
    assert node.layout.direction == "row", (
        f"624:7171 direction={node.layout.direction!r}, expected 'row'"
    )
    assert node.layout.confidence >= 0.7


@pytest.mark.skip(
    reason=(
        "Spatial layout inference is temporarily disabled to keep "
        "the LLM-facing output compact while the X-Ray walker "
        "fixes land. See docs/x-ray-walker-investigation.md §13."
    )
)
def test_e01_alerts_layout_container_classifies_as_row() -> None:
    """The e01 Alerts layout-container (667:221) pairs the alert
    count with its trailing icon at the same top — must classify
    as ``row`` even though e01 has many low-confidence single-child
    instances elsewhere.
    """
    mapping = _walk_real_fixture("figma-e01-share-summary.json")
    node = next(
        (n for n in mapping.layout_tree if n.id == "667:221"), None
    )
    assert node is not None, "missing 667:221 (Alerts container)"
    assert node.layout is not None
    assert node.layout.direction == "row"


@pytest.mark.skip(
    reason=(
        "Spatial layout inference is temporarily disabled to keep "
        "the LLM-facing output compact while the X-Ray walker "
        "fixes land. See docs/x-ray-walker-investigation.md §13."
    )
)
def test_real_fixtures_classify_every_multi_child_node() -> None:
    """Floor invariant: every layout-tree node with ≥ 2 children
    must receive a non-``None`` direction.

    The handoff doc's L1 goal was "≥ 30 % of layout-tree nodes
    carry a usable direction". Empirically the walker now hits 100
    % of multi-child nodes across all three big fixtures because
    even degenerate cases collapse to ``single`` / ``stack`` rather
    than ``None``. Asserting the 100 % floor here means future
    tweaks to :mod:`prism_mcp.figma.layout_inference` (e.g. raising
    the winner threshold) can't silently drop nodes back into the
    "no signal" bucket.
    """
    for fixture in (
        "figma-d02-share-summary.json",
        "figma-e01-share-summary.json",
        "figma-active-cluster-page.json",
    ):
        mapping = _walk_real_fixture(fixture)
        multi = [
            n for n in mapping.layout_tree if len(n.children_ids) >= 2
        ]
        missing = [
            n
            for n in multi
            if n.layout is None or n.layout.direction is None
        ]
        assert not missing, (
            f"{fixture}: {len(missing)} of {len(multi)} multi-child "
            f"layout-tree nodes have no direction; first offender: "
            f"id={missing[0].id} name={missing[0].name!r}"
        )


# Per-fixture row+column floor counts. Mirrors the plan's
# "defence in depth against silent regressions" gate. The d02 and
# active-cluster floors are the values from the plan; e01 is
# tuned down from the plan's 25 because most of e01's multi-child
# nodes (27 / 38) legitimately classify as ``single`` after IoU
# absorbs a sibling overlay — a Figma export quirk the plan author
# did not anticipate. The 100 % non-None invariant above still
# covers the regression we actually care about.
_FIXTURE_ROWCOL_FLOORS: dict[str, int] = {
    "figma-d02-share-summary.json": 30,
    "figma-active-cluster-page.json": 12,
    "figma-e01-share-summary.json": 8,
}


@pytest.mark.skip(
    reason=(
        "Spatial layout inference is temporarily disabled to keep "
        "the LLM-facing output compact while the X-Ray walker "
        "fixes land. See docs/x-ray-walker-investigation.md §13."
    )
)
def test_real_fixtures_meet_per_fixture_rowcol_floors() -> None:
    """Per-fixture floor on the number of layout-tree nodes that
    classify as ``row`` or ``column``.

    This complements
    :func:`test_real_fixtures_classify_every_multi_child_node`
    (100 % non-None floor) with an absolute lower bound so a
    future change that quietly degrades the row/column signal —
    pushing nodes into ``stack`` or ``single`` instead — can't
    sneak past the broader floor. Numbers are the plan's targets
    where the implementation meets them; the e01 floor is tuned
    down to reflect the legitimate ``single``-classification of
    list-heavy single-child wrappers.
    """
    for fixture, floor in _FIXTURE_ROWCOL_FLOORS.items():
        mapping = _walk_real_fixture(fixture)
        rowcol = [
            n
            for n in mapping.layout_tree
            if n.layout is not None
            and n.layout.direction in ("row", "column")
        ]
        assert len(rowcol) >= floor, (
            f"{fixture}: only {len(rowcol)} row/column layout-tree "
            f"nodes, expected at least {floor}. A regression here "
            f"means the inference is losing flex signals it used to "
            f"surface — likely a winner-threshold or IoU change."
        )


# ----------------------------------------------------------------------
# Mapping resolver: dedup + parallel + fault tolerance.
#
# These pin the contract of
# :func:`prism_mcp.figma.walker._resolve_pending_mappings` directly so
# a regression in either the dedup cache (Optimisation 2) or the
# parallel executor (Optimisation 7) fails its own test rather than
# manifesting as a perf surprise on real pages.
# ----------------------------------------------------------------------


def _hamburger_fixture_tree() -> dict[str, object]:
    """Helper: load the 3-stripe hamburger icon fixture as a dict.

    The fixture is small (single icon region after walker collapse)
    so the tests run fast even when ``map_figma_node_fn`` performs
    no real work.
    """
    return json.loads(
        (FIXTURE_DIR / "hamburger-icon.json").read_text(encoding="utf-8")
    )


def _stub_mapping_factory(component_name: str):
    """Build a stub mapper that returns a deterministic FigmaNodeMapping.

    Each call records its kwargs so tests can assert on the call
    log. The returned mapping echoes ``node_name`` per the real
    mapper contract and surfaces ``component_name`` as the top
    candidate so the walker's downstream consumers see a usable
    shape.
    """
    from prism_mcp.workflow.figma_mapping import (
        CandidateMatch,
        FigmaNodeMapping,
    )

    calls: list[dict[str, object]] = []

    def stub(**kwargs: object):
        calls.append(dict(kwargs))
        return FigmaNodeMapping(
            node_name=str(kwargs.get("node_name", "")),
            suggested_component_name=component_name,
            candidates=[
                CandidateMatch(
                    name=component_name,
                    type="component",
                    score=0.9,
                    why_matched=[],
                    summary="",
                    source="bm25",
                )
            ],
        )

    return stub, calls


def test_resolver_dedups_identical_mapping_inputs(monkeypatch) -> None:
    """Two agenda rows with byte-identical mapper kwargs share one call.

    Pins Optimisation 2 (dedup-by-query-hash). We synthesise a
    two-region tree where both regions land on identical kwargs
    (same name, same colors, no children, no parents) and assert
    the mapper was invoked once but both ``MappedRegion.mapping``
    objects carry the same component pick. The walker's call-site
    invariant (``call_count <= len(agenda)``) is the public surface
    of this dedup.
    """
    # Force serial execution so this test is isolated to the dedup
    # path — Optimisation 7's parallel layer has its own coverage
    # below.
    monkeypatch.setenv("PRISM_MCP_PARALLEL_MAPPING_WORKERS", "1")

    # Two sibling INSTANCEs with identical names, types, sizes, and
    # contents. Each carries a visible solid fill so the walker's
    # pass_2 invisible-decoration filter keeps them. The walker
    # emits one :class:`MappedRegion` per INSTANCE; both share
    # byte-identical mapper kwargs → cache-key collision → one
    # call.
    fills = [
        {
            "type": "SOLID",
            "visible": True,
            "opacity": 1.0,
            "color": {
                "r": 0.1,
                "g": 0.4,
                "b": 0.8,
                "a": 1.0,
            },
        }
    ]
    tree = {
        "id": "root",
        "type": "FRAME",
        "name": "Root",
        "visible": True,
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 800,
            "height": 600,
        },
        "children": [
            {
                "id": "1:1",
                "type": "INSTANCE",
                "name": "Repeated",
                "visible": True,
                "fills": fills,
                "absoluteBoundingBox": {
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                },
            },
            {
                "id": "1:2",
                "type": "INSTANCE",
                "name": "Repeated",
                "visible": True,
                "fills": fills,
                "absoluteBoundingBox": {
                    "x": 200,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                },
            },
        ],
    }
    stub, calls = _stub_mapping_factory("DedupComponent")

    from prism_mcp.figma import walk_tree

    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=stub,
    )

    # Three regions get emitted: one composed-region for the Root
    # FRAME plus two INSTANCEs. The mapper, however, must be called
    # only twice — once for Root, once for the two identical
    # ``Repeated`` INSTANCEs (deduplicated by cache key).
    repeated_regions = [r for r in mapping.agenda if r.name == "Repeated"]
    root_regions = [r for r in mapping.agenda if r.name == "Root"]
    assert len(repeated_regions) == 2, (
        f"expected two ``Repeated`` regions, got "
        f"{len(repeated_regions)} ({[r.name for r in mapping.agenda]!r})"
    )
    assert len(root_regions) == 1, (
        f"expected one ``Root`` composed-region, got "
        f"{len(root_regions)} ({[r.name for r in mapping.agenda]!r})"
    )
    repeated_calls = [c for c in calls if c.get("node_name") == "Repeated"]
    assert len(repeated_calls) == 1, (
        f"dedup should collapse two identical-input ``Repeated`` "
        f"regions to one mapper call; got {len(repeated_calls)} "
        f"call(s) for ``Repeated``: {repeated_calls!r}"
    )
    # And both ``Repeated`` regions still receive a real mapping
    # (no broken placeholder left from the dedup hit).
    for region in repeated_regions:
        assert region.mapping.suggested_component_name == "DedupComponent"
        assert region.mapping.node_name == "Repeated"


def test_resolver_serial_and_parallel_produce_identical_output(
    monkeypatch,
) -> None:
    """Output of the walker is independent of worker count.

    Pins Optimisation 7's correctness contract: the parallel path
    must produce the same :attr:`FigmaTreeMapping.agenda` (same
    ids, same mappings) as the serial path. We run the walker
    twice — once with ``PRISM_MCP_PARALLEL_MAPPING_WORKERS=1`` and
    once with ``=4`` — and compare structured fields.

    The mapper is a deterministic stub (no fastembed, no network)
    so any divergence isolates to the resolver, not the encoder.
    """
    from prism_mcp.figma import walk_tree

    tree = _hamburger_fixture_tree()

    monkeypatch.setenv("PRISM_MCP_PARALLEL_MAPPING_WORKERS", "1")
    serial_stub, _ = _stub_mapping_factory("Hamburger")
    serial_result = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=serial_stub,
    )

    monkeypatch.setenv("PRISM_MCP_PARALLEL_MAPPING_WORKERS", "4")
    parallel_stub, _ = _stub_mapping_factory("Hamburger")
    parallel_result = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=parallel_stub,
    )

    # Agenda order must be DFS-deterministic in both modes.
    serial_ids = [r.id for r in serial_result.agenda]
    parallel_ids = [r.id for r in parallel_result.agenda]
    assert serial_ids == parallel_ids, (
        f"agenda order diverged: serial={serial_ids!r} "
        f"parallel={parallel_ids!r}; parallel resolver must "
        "preserve the DFS-derived ordering"
    )

    for s, p in zip(serial_result.agenda, parallel_result.agenda, strict=True):
        assert (
            s.mapping.suggested_component_name
            == p.mapping.suggested_component_name
        ), (
            f"region {s.id!r}: mapping diverged between serial and "
            f"parallel runs (serial={s.mapping.suggested_component_name!r} "
            f"parallel={p.mapping.suggested_component_name!r})"
        )


def test_resolver_tolerates_mapper_exceptions(monkeypatch) -> None:
    """One mapper raising must not abort the whole walk.

    Pins the fault-tolerance contract of
    :func:`_resolve_pending_mappings`: when a single mapper call
    fails (production: malformed input, transient ONNX error,
    OOM-during-rerank, etc.), the resolver logs + records a
    warning per affected region but **leaves the placeholder
    mapping in place** so the agenda is still serializable. Other
    regions' mappings still reflect their real (successful)
    results.

    Regressing this would turn one malformed Figma node into a
    full-page failure, which is exactly the failure mode the
    resolver was designed to avoid.
    """
    monkeypatch.setenv("PRISM_MCP_PARALLEL_MAPPING_WORKERS", "1")

    from prism_mcp.figma import walk_tree
    from prism_mcp.workflow.figma_mapping import (
        CandidateMatch,
        FigmaNodeMapping,
    )

    def mixed_mapper(**kwargs):
        # Fail only when the layer name says "BadLayer"; succeed
        # for the others. Real production exceptions usually
        # trigger on specific inputs too (e.g. a malformed
        # reference_code string), so this stub mirrors the shape.
        if str(kwargs.get("node_name", "")) == "BadLayer":
            raise ValueError("synthetic mapper failure")
        return FigmaNodeMapping(
            node_name=str(kwargs.get("node_name", "")),
            suggested_component_name="Generic",
            candidates=[
                CandidateMatch(
                    name="Generic",
                    type="component",
                    score=0.9,
                    why_matched=[],
                    summary="",
                    source="bm25",
                )
            ],
        )

    fills = [
        {
            "type": "SOLID",
            "visible": True,
            "opacity": 1.0,
            "color": {
                "r": 0.1,
                "g": 0.4,
                "b": 0.8,
                "a": 1.0,
            },
        }
    ]
    tree = {
        "id": "root",
        "type": "FRAME",
        "name": "Root",
        "visible": True,
        "absoluteBoundingBox": {
            "x": 0,
            "y": 0,
            "width": 800,
            "height": 600,
        },
        "children": [
            {
                "id": "1:1",
                "type": "INSTANCE",
                "name": "GoodLayer",
                "visible": True,
                "fills": fills,
                "absoluteBoundingBox": {
                    "x": 0,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                },
            },
            {
                "id": "1:2",
                "type": "INSTANCE",
                "name": "BadLayer",
                "visible": True,
                "fills": fills,
                "absoluteBoundingBox": {
                    "x": 200,
                    "y": 0,
                    "width": 100,
                    "height": 100,
                },
            },
        ],
    }

    result = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=mixed_mapper,
    )

    # Walker emits one composed-region per parent FRAME + one
    # MappedRegion per surviving INSTANCE child, so the agenda
    # contains Root + GoodLayer + BadLayer (3 rows). The partial
    # mapper failure must NOT prune any of them — every region
    # still ships in the agenda.
    names = [r.name for r in result.agenda]
    assert {"GoodLayer", "BadLayer", "Root"} <= set(names), (
        f"expected all three regions on the agenda after a partial "
        f"mapper failure, got names={names!r}"
    )
    by_name = {r.name: r for r in result.agenda}
    good = by_name["GoodLayer"]
    bad = by_name["BadLayer"]
    assert good.mapping.suggested_component_name == "Generic", (
        "GoodLayer's mapping should still reflect the successful "
        "mapper call even when a sibling region failed"
    )
    assert bad.mapping.suggested_component_name is None, (
        "BadLayer should keep its placeholder mapping (None top "
        "candidate) after the mapper raised"
    )
    failure_warnings = [
        w for w in result.warnings if "map_figma_node failed" in w
    ]
    assert failure_warnings, (
        f"expected at least one 'map_figma_node failed' warning for "
        f"the failed BadLayer mapping; got warnings={result.warnings!r}"
    )
    # And the failure warning must reference the specific failed
    # region so a downstream operator can correlate it.
    assert any("BadLayer" in w for w in failure_warnings), (
        f"failure warning should mention 'BadLayer'; got "
        f"warnings={failure_warnings!r}"
    )
