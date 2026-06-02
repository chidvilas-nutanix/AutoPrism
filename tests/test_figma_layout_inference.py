"""Unit tests for :mod:`prism_mcp.figma.layout_inference`.

Each test pins one branch of :func:`analyze_layout` (and the
companion :func:`compute_absolute_pos`) so a future tweak to any
one constant — IoU threshold, score weights, gap-consistency ratio
— can't silently regress another pass.

The tests intentionally use tiny hand-crafted node dicts rather than
real Figma JSON: the algorithm only reads
``absoluteBoundingBox`` / ``layoutMode`` / ``itemSpacing`` /
``primaryAxisAlignItems`` / ``counterAxisAlignItems`` /
``counterAxisSpacing`` / ``counterAxisSizingMode`` /
``layoutPositioning``, so each fixture is one or two lines of dict
literal. Real-world fixture spot-checks live in
``test_figma_walker.py``.
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma.layout_inference import (
    analyze_layout,
    compute_absolute_pos,
)


def _bbox(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def _child(id_: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {"id": id_, "absoluteBoundingBox": _bbox(x, y, w, h)}


# ----------------------------------------------------------------------
# Pass 1: Auto-layout fast path.
# ----------------------------------------------------------------------


def test_auto_layout_horizontal_translates_axes_and_emits_gap() -> None:
    """HORIZONTAL + MIN/CENTER + itemSpacing -> row/start/center/gap.

    Pins the full translation table for the most common shape
    (horizontal auto-layout, top-aligned, centered on the main
    axis) plus the ``confidence=1.0`` / rationale invariant for the
    fast path.
    """
    parent = {
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 12,
        "primaryAxisAlignItems": "CENTER",
        "counterAxisAlignItems": "MIN",
        "absoluteBoundingBox": _bbox(0, 0, 300, 40),
    }
    children = [_child("a", 0, 0, 100, 40), _child("b", 100, 0, 100, 40)]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.justify_content == "center"
    assert out.align_items == "start"
    assert out.gap == 12.0
    assert out.confidence == 1.0
    assert out.rationale == "figma_auto_layout"
    assert out.flow_children == ["a", "b"]
    assert out.absolute_children == []


def test_auto_layout_vertical_with_baseline_counter_axis() -> None:
    """VERTICAL + BASELINE -> column / baseline (the rare-but-real
    alignment for typography rows in vertical stacks).
    """
    parent = {
        "layoutMode": "VERTICAL",
        "itemSpacing": 8,
        "primaryAxisAlignItems": "MAX",
        "counterAxisAlignItems": "BASELINE",
        "absoluteBoundingBox": _bbox(0, 0, 100, 200),
    }
    children = [_child("a", 0, 0, 60, 20), _child("b", 0, 28, 60, 20)]
    out = analyze_layout(parent, children)
    assert out.direction == "column"
    assert out.justify_content == "end"
    assert out.align_items == "baseline"
    assert out.gap == 8.0


def test_auto_layout_grid_with_equal_spacings_collapses_to_one_gap() -> None:
    """GRID + itemSpacing == counterAxisSpacing -> single gap value.

    Designers usually want the same row/column spacing; the
    algorithm reports one ``gap`` so the generator can emit
    ``gap: 16px`` instead of two separate row/column gaps.
    """
    parent = {
        "layoutMode": "GRID",
        "itemSpacing": 16,
        "counterAxisSpacing": 16,
        "absoluteBoundingBox": _bbox(0, 0, 200, 200),
    }
    children = [_child("a", 0, 0, 50, 50), _child("b", 60, 0, 50, 50)]
    out = analyze_layout(parent, children)
    assert out.direction == "grid"
    assert out.gap == 16.0


def test_auto_layout_grid_with_unequal_spacings_emits_no_gap() -> None:
    """GRID with row-gap != column-gap -> ``gap=None`` (caller must
    fall back to ``rowGap`` / ``columnGap`` per-child).
    """
    parent = {
        "layoutMode": "GRID",
        "itemSpacing": 16,
        "counterAxisSpacing": 24,
        "absoluteBoundingBox": _bbox(0, 0, 200, 200),
    }
    children = [_child("a", 0, 0, 50, 50), _child("b", 60, 0, 50, 50)]
    out = analyze_layout(parent, children)
    assert out.direction == "grid"
    assert out.gap is None


def test_auto_layout_layout_positioning_absolute_splits_child() -> None:
    """``layoutPositioning="ABSOLUTE"`` escapes the auto-layout flow.

    The escaped child must show up in ``absolute_children`` while
    the rest stay in ``flow_children``. This is Figma's mechanism
    for floating badges inside an auto-layout container.
    """
    parent = {
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 8,
        "absoluteBoundingBox": _bbox(0, 0, 200, 40),
    }
    children = [
        _child("flow", 0, 0, 60, 40),
        {**_child("float", 50, -10, 16, 16), "layoutPositioning": "ABSOLUTE"},
    ]
    out = analyze_layout(parent, children)
    assert out.flow_children == ["flow"]
    assert out.absolute_children == ["float"]


def test_auto_layout_unknown_primary_value_records_in_rationale() -> None:
    """An unmapped ``primaryAxisAlignItems`` value (e.g. a future
    Figma enum we don't know yet) leaves ``justify_content=None``
    and surfaces the original token in the rationale for debug.
    """
    parent = {
        "layoutMode": "HORIZONTAL",
        "primaryAxisAlignItems": "MYSTERY_VALUE",
        "absoluteBoundingBox": _bbox(0, 0, 100, 40),
    }
    children = [_child("a", 0, 0, 40, 40)]
    out = analyze_layout(parent, children)
    assert out.justify_content is None
    assert "unmapped_primary=MYSTERY_VALUE" in out.rationale


def test_auto_layout_counter_axis_sizing_auto_implies_stretch() -> None:
    """When ``counterAxisAlignItems`` is missing and the cross axis
    sizes itself to fit children, infer ``align_items=stretch``.

    This matches Figma's UI behaviour: with sizing AUTO on the
    cross axis, every child fills the available space.
    """
    parent = {
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 4,
        "counterAxisSizingMode": "AUTO",
        "absoluteBoundingBox": _bbox(0, 0, 200, 40),
    }
    children = [_child("a", 0, 0, 60, 40), _child("b", 64, 0, 60, 40)]
    out = analyze_layout(parent, children)
    assert out.align_items == "stretch"


# ----------------------------------------------------------------------
# Pass 2: Trivial cases.
# ----------------------------------------------------------------------


def test_zero_children_returns_direction_none() -> None:
    """Empty containers have no flow — ``direction=None``."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 100, 100)}
    out = analyze_layout(parent, [])
    assert out.direction is None
    assert out.flow_children == []
    assert out.absolute_children == []


def test_single_child_returns_single_with_confidence_one() -> None:
    """A 1-child container has no flow to detect but is structurally
    valid — ``direction="single"`` is unambiguous."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 100, 100)}
    out = analyze_layout(parent, [_child("a", 10, 10, 40, 40)])
    assert out.direction == "single"
    assert out.flow_children == ["a"]
    assert out.confidence == 1.0


# ----------------------------------------------------------------------
# Pass 3: Overlap detection (IoU).
# ----------------------------------------------------------------------


def test_overlap_above_threshold_puts_smaller_child_absolute() -> None:
    """A badge fully inside a button covers ~100% of its own area
    by min-area IoU -> badge joins ``absolute_children``.

    Threshold is 0.1; this test sits well above to guarantee the
    pass triggers even if the constant moves slightly.
    """
    parent = {"absoluteBoundingBox": _bbox(0, 0, 200, 100)}
    children = [
        _child("button", 0, 0, 120, 40),
        _child("badge", 100, 5, 16, 16),
    ]
    out = analyze_layout(parent, children)
    assert "badge" in out.absolute_children
    assert "button" not in out.absolute_children


def test_disjoint_children_stay_in_flow() -> None:
    """Children that don't overlap stay in the flow list — the IoU
    pass must not over-fire on tightly-packed siblings."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 300, 40)}
    children = [
        _child("a", 0, 0, 60, 40),
        _child("b", 80, 0, 60, 40),
        _child("c", 160, 0, 60, 40),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.absolute_children == []
    assert set(out.flow_children) == {"a", "b", "c"}


# ----------------------------------------------------------------------
# Pass 4: Direction scoring.
# ----------------------------------------------------------------------


def test_horizontal_arrangement_scores_row() -> None:
    """Three children at the same top, evenly spaced left-to-right,
    must score row >> column (top alignment = 1.0, gap range
    matches, distribution = 1.0)."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 300, 50)}
    children = [
        _child("a", 0, 10, 60, 30),
        _child("b", 80, 10, 60, 30),
        _child("c", 160, 10, 60, 30),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.gap == 20.0
    assert out.align_items == "start"


def test_vertical_arrangement_scores_column() -> None:
    """Children stacked top-to-bottom at the same left score column.

    16-px gap chosen so the 4-px-grid snap is exact (16/4=4) and
    the assertion is independent of any future change to the snap
    rounding policy.
    """
    parent = {"absoluteBoundingBox": _bbox(0, 0, 100, 200)}
    children = [
        _child("a", 0, 0, 80, 30),
        _child("b", 0, 46, 80, 30),
        _child("c", 0, 92, 80, 30),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "column"
    assert out.gap == 16.0


def test_scattered_children_collapse_to_stack() -> None:
    """Children with no row/column structure and no overlap should
    still collapse to ``direction="stack"`` because neither score
    crosses the 0.4 winner threshold.

    Constructed with 4 children placed at corners with large gaps
    (> 50 px) so the distribution score is 0 on both axes.
    """
    parent = {"absoluteBoundingBox": _bbox(0, 0, 400, 400)}
    children = [
        _child("tl", 0, 0, 60, 60),
        _child("tr", 340, 0, 60, 60),
        _child("bl", 0, 340, 60, 60),
        _child("br", 340, 340, 60, 60),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "stack"
    assert set(out.absolute_children) == {"tl", "tr", "bl", "br"}


# ----------------------------------------------------------------------
# Pass 5: Gap analysis.
# ----------------------------------------------------------------------


def test_inconsistent_gaps_emit_none_and_flag_consistent_false() -> None:
    """Three children spaced 5px, 50px, 5px -> std/mean > 0.2 ->
    ``gap=None`` and ``gap_consistent=False``."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 400, 50)}
    children = [
        _child("a", 0, 10, 60, 30),
        _child("b", 65, 10, 60, 30),  # gap = 5
        _child("c", 175, 10, 60, 30),  # gap = 50
        _child("d", 240, 10, 60, 30),  # gap = 5
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.gap is None
    assert out.gap_consistent is False


# ----------------------------------------------------------------------
# Pass 6: justify-content / align-items inference.
# ----------------------------------------------------------------------


def test_justify_space_between_when_flush_both_ends_with_consistent_gap() -> None:
    """Children flush against the left AND right of the parent with
    a uniform gap between them -> ``justify_content="space-between"``.

    Geometry: parent 240 wide, 3 children of width 60 with 30-px
    gaps. Gap is within the [0, 50] range so the row distribution
    score is 1.0 and the algorithm commits to row before deciding
    justify-content.
    """
    parent = {"absoluteBoundingBox": _bbox(0, 0, 240, 40)}
    children = [
        _child("a", 0, 5, 60, 30),
        _child("b", 90, 5, 60, 30),
        _child("c", 180, 5, 60, 30),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.justify_content == "space-between"


def test_align_items_center_when_centerlines_match() -> None:
    """Row children with different heights but identical vertical
    centerlines -> ``align_items="center"``."""
    parent = {"absoluteBoundingBox": _bbox(0, 0, 300, 100)}
    children = [
        _child("tall", 0, 30, 60, 40),
        _child("short", 80, 40, 60, 20),
        _child("medium", 160, 35, 60, 30),
    ]
    out = analyze_layout(parent, children)
    assert out.direction == "row"
    assert out.align_items == "center"


# ----------------------------------------------------------------------
# compute_absolute_pos.
# ----------------------------------------------------------------------


def test_compute_absolute_pos_subtracts_parent_origin() -> None:
    """Child top/left must be relative to the parent's bbox origin —
    not the absolute Figma coordinate."""
    parent = {"absoluteBoundingBox": _bbox(100, 200, 400, 300)}
    child = _child("c", 150, 230, 80, 40)
    ap = compute_absolute_pos(parent, child, z_order=2)
    assert ap is not None
    assert ap.top == 30.0
    assert ap.left == 50.0
    assert ap.width == 80.0
    assert ap.height == 40.0
    assert ap.z_order == 2


def test_compute_absolute_pos_returns_none_when_missing_bbox() -> None:
    """No bbox on either side -> ``None`` rather than zeros that
    look like real coordinates."""
    assert compute_absolute_pos({}, _child("c", 0, 0, 10, 10), 0) is None
    assert compute_absolute_pos({"absoluteBoundingBox": _bbox(0, 0, 1, 1)}, {}, 0) is None
