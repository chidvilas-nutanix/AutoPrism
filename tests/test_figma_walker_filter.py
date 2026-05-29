"""Integration tests for the noise filter wired through :func:`walk_tree`.

These tests exercise the full DFS — passes 1, 2, 3, 4, and 6 working
together — against the 19-node mini-fixture from
``docs/figma-page-to-prism-plan.md`` §8.1.

Phase 2 scope: we assert the *drops* are correct. Phase 3+ will add
agenda emission and role tagging.
"""

from __future__ import annotations

from prism_mcp.figma.filter import DropReason
from prism_mcp.figma.walker import walk_tree


def _bbox(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def _text(node_id: str, name: str, characters: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "TEXT",
        "characters": characters,
        "absoluteBoundingBox": _bbox(0, 0, 100, 20),
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 0.13, "g": 0.15, "b": 0.18},
                "opacity": 1.0,
            }
        ],
    }


def _invisible_rect(node_id: str, name: str) -> dict:
    """A spacer rectangle with opacity 0.0001 — the §8.1 pattern."""
    return {
        "id": node_id,
        "name": name,
        "type": "RECTANGLE",
        "absoluteBoundingBox": _bbox(0, 0, 320, 40),
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 1.0, "g": 1.0, "b": 1.0},
                "opacity": 0.0001,
            }
        ],
    }


def _row_frame(node_id: str, name: str, text: dict, rects: list[dict]) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 320, 40),
        "fills": [],
        "children": [*rects, text],
    }


def _build_active_cluster_mini_fixture() -> dict:
    """Reconstruct the §8.1 19-node Impacted Cluster Copy fixture.

    The exact ids match the worked example so failures point at
    the right line in the design doc.
    """
    # Innermost: the redundant inner INSTANCE with same bbox as A2.
    inner_instance = {
        "id": "I626:987;0:2729",
        "name": "Tile",
        "type": "INSTANCE",
        "absoluteBoundingBox": _bbox(940, 521, 320, 309),
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 1.0, "g": 1.0, "b": 1.0},
                "opacity": 1.0,
            }
        ],
    }
    header = {
        "id": "I626:987;0:2730",
        "name": "Header",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(940, 521, 320, 40),
        "fills": [],
        "children": [
            _text("I626:987;0:2732", "Title", "Top 5 Shares by Connections"),
        ],
    }
    a2_tile = {
        "id": "626:987",
        "name": "Tile",
        "type": "INSTANCE",
        "absoluteBoundingBox": _bbox(940, 521, 320, 309),
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 1.0, "g": 1.0, "b": 1.0},
                "opacity": 1.0,
            }
        ],
        "children": [inner_instance, header],
    }

    row1 = _row_frame(
        "626:989",
        "Row",
        _text("626:991", "CBTest", "CBTest"),
        [_invisible_rect("626:990", "Row")],
    )
    row2 = _row_frame(
        "626:992",
        "Row",
        _text("626:995", "Intern19Share", "Intern19Share"),
        [
            _invisible_rect("626:993", "Row"),
            _invisible_rect("626:994", "Row Copy 4"),
        ],
    )
    row3 = _row_frame(
        "626:996",
        "Row",
        _text("626:998", "SyslogShare", "SyslogShare"),
        [_invisible_rect("626:997", "Row")],
    )

    cluster_details = {
        "id": "626:988",
        "name": "Cluster Details",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(940, 561, 320, 120),
        "fills": [],
        "children": [row1, row2, row3],
    }

    row4 = _row_frame(
        "626:1000",
        "Row",
        _text("626:1003", "NTNX21Share", "NTNX21Share"),
        [
            _invisible_rect("626:1001", "Row"),
            _invisible_rect("626:1002", "Row Copy"),
        ],
    )
    row5 = _row_frame(
        "626:1004",
        "Row",
        _text("626:1005", "DemoTest", "DemoTest"),
        [],
    )

    cluster_details_copy = {
        "id": "626:999",
        "name": "Cluster Details Copy",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(940, 681, 320, 80),
        "fills": [],
        "children": [row4, row5],
    }

    root = {
        "id": "626:986",
        "name": "Impacted Cluster Copy",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(940, 521, 320, 309),
        "fills": [],
        "children": [a2_tile, cluster_details, cluster_details_copy],
    }
    return root


def test_walker_collapses_single_same_bbox_wrapper() -> None:
    """A GROUP wrapping ONLY an INSTANCE with the same bbox must
    collapse — this is the Pass 4 atomic case without competing
    sibling regions (the §8.1 root-collapse case lands in Phase 4
    once stat-list patterns fold A6/A17 first)."""
    fixture = {
        "id": "ROOT",
        "name": "Wrapper",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 320, 309),
        "fills": [],
        "children": [
            {
                "id": "CHILD",
                "name": "Tile",
                "type": "INSTANCE",
                "absoluteBoundingBox": _bbox(0, 0, 320, 309),
                "fills": [
                    {
                        "type": "SOLID",
                        "color": {"r": 1.0, "g": 1.0, "b": 1.0},
                        "opacity": 1.0,
                    }
                ],
            }
        ],
    }
    result = walk_tree(tree_json=fixture)

    collapsed = [
        d
        for d in result.dropped
        if d.reason == DropReason.same_bbox_passthrough_collapsed.value
    ]
    assert any(d.id == "ROOT" for d in collapsed), (
        "expected ROOT GROUP to collapse into CHILD INSTANCE, got drops: "
        f"{[(d.id, d.reason) for d in result.dropped]}"
    )


def test_walker_does_not_collapse_multi_child_root_in_phase_2() -> None:
    """Phase 2 invariant: the §8.1 root has 3 significant children
    (Tile + 2 stat-list GROUPs). Pass 4 cannot fire until Phase 4
    patterns collapse the stat-lists first. This test pins Phase 2
    behaviour explicitly so a future regression is visible."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    collapsed_ids = {
        d.id
        for d in result.dropped
        if d.reason == DropReason.same_bbox_passthrough_collapsed.value
    }
    assert "626:986" not in collapsed_ids, (
        "root GROUP should NOT collapse in Phase 2 — Phase 4 will "
        "fold the stat-list siblings first, then root collapses."
    )


def test_walker_drops_opacity_0_0001_rectangles() -> None:
    """The six spacer rectangles in §8.1 must NOT appear in the
    agenda — whether they drop as ``invisible_decoration`` or get
    absorbed into a stat-list ``folded_into_pattern`` is an
    implementation detail. The invariant is "no spacer rect
    survives to a MappedRegion"."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    removed_ids = {
        d.id
        for d in result.dropped
        if d.reason
        in {
            DropReason.invisible_decoration.value,
            DropReason.folded_into_pattern.value,
        }
    }
    assert {
        "626:990",
        "626:993",
        "626:994",
        "626:997",
        "626:1001",
        "626:1002",
    } <= removed_ids


def test_walker_emits_stat_list_for_cluster_details_groups() -> None:
    """The §8.1 ``Cluster Details`` and ``Cluster Details Copy`` GROUPs
    each match the stat-list pattern (≥2 Row FRAMEs with 1 TEXT + 0+
    invisible RECTANGLEs)."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    stat_list_regions = [r for r in result.agenda if r.role == "stat-list"]
    region_ids = {r.id for r in stat_list_regions}
    assert "626:988" in region_ids
    assert "626:999" in region_ids


def test_walker_input_nodes_matches_fixture_size() -> None:
    """The summary count is the total DFS node count including
    root — used for the dropped-vs-kept sanity check."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    # Fixture: root(1) + Tile(1) + inner_instance(1) + header(1) +
    # header.text(1) + cluster_details(1) + 3 rows(3) +
    # 4 rects in those rows + 3 row texts = 16 below root +
    # cluster_details_copy(1) + 2 rows(2) + 2 rects + 2 texts = 23
    # Let's count concretely.
    expected = 23
    assert result.summary["input_nodes"] == expected


def test_walker_drops_have_machine_readable_reasons() -> None:
    """Every drop must carry one of the enumerated reason codes."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    allowed = {str(r) for r in DropReason}
    actual = {d.reason for d in result.dropped}
    assert actual <= allowed, f"unknown reasons: {actual - allowed}"


def test_walker_summary_includes_dropped_by_reason_histogram() -> None:
    """``summary.dropped_<reason>`` keys feed the Cursor skill's
    "sanity check" gate per §7.2 D2 — at least one drop reason
    must carry a non-zero histogram bucket on the §8.1 fixture."""
    fixture = _build_active_cluster_mini_fixture()
    result = walk_tree(tree_json=fixture)
    histogram_keys = [k for k in result.summary if k.startswith("dropped_")]
    assert histogram_keys, "expected at least one dropped_<reason> bucket"
    # The §8.1 fixture is dominated by folded_into_pattern (rows
    # absorbed into stat-list) plus captured_as_content_slot (text).
    folded = result.summary.get("dropped_folded_into_pattern", 0)
    captured = result.summary.get("dropped_captured_as_content_slot", 0)
    assert folded + captured >= 6, (
        f"expected at least 6 folded_into_pattern + captured_as_content_slot, "
        f"got {folded} + {captured}"
    )


def test_walker_handles_explicit_hidden_subtree() -> None:
    """A whole subtree with ``visible=False`` drops as
    ``explicit_hidden``."""
    fixture = {
        "id": "1:1",
        "name": "Root",
        "type": "FRAME",
        "visible": False,
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
        "children": [
            {
                "id": "1:2",
                "name": "Inner",
                "type": "FRAME",
                "absoluteBoundingBox": _bbox(0, 0, 50, 50),
                "fills": [],
            }
        ],
    }
    result = walk_tree(tree_json=fixture)
    reasons = {d.id: d.reason for d in result.dropped}
    assert reasons["1:1"] == DropReason.explicit_hidden.value
    assert reasons["1:2"] == DropReason.explicit_hidden.value


def test_walker_drops_non_design_types_with_subtree() -> None:
    fixture = {
        "id": "1:1",
        "name": "Sticky",
        "type": "STICKY",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
        "children": [{"id": "1:2", "name": "x", "type": "RECTANGLE"}],
    }
    result = walk_tree(tree_json=fixture)
    reasons = {d.id: d.reason for d in result.dropped}
    assert reasons["1:1"] == DropReason.non_design_type.value
    assert reasons["1:2"] == DropReason.non_design_type.value
