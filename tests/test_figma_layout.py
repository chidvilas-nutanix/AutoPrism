"""Unit tests for :mod:`prism_mcp.figma.layout` (roadmap P4).

Pins the CSS ``LayoutAnalysis`` -> Prism Layout primitive mapping and the
two token snappers (``itemGap`` T-shirt ladder, ``padding`` token set).
``LayoutAnalysis`` is constructed directly so each test isolates the
*mapping* from the upstream geometry inference (which has its own suite in
``test_figma_layout_inference.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from prism_mcp.figma.layout import (
    detect_fill_children,
    detect_page_shell,
    layout_for_container,
    resolve_prism_layout,
    snap_item_gap,
    snap_padding,
)
from prism_mcp.figma.models import LayoutAnalysis, PrismLayout, PrismPageShell


def _auto(direction: str, **kw: Any) -> LayoutAnalysis:
    """A 1.0-confidence auto-layout analysis (rationale = fast-path tag)."""
    return LayoutAnalysis(
        direction=direction, confidence=1.0, rationale="figma_auto_layout", **kw
    )


# ----------------------------------------------------------------------
# snap_item_gap — the itemGap T-shirt ladder (Variables.less:119-124).
# ----------------------------------------------------------------------


def test_snap_item_gap_none_passes_through() -> None:
    assert snap_item_gap(None) is None


def test_snap_item_gap_zero_is_none_token() -> None:
    """0px -> ``"none"`` (must be explicit to override FlexLayout's 20px
    ``itemSpacing`` default)."""
    assert snap_item_gap(0) == "none"


@pytest.mark.parametrize(
    ("px", "token"),
    [(5, "XS"), (10, "S"), (15, "M"), (20, "L"), (30, "XL"), (40, "XXL")],
)
def test_snap_item_gap_exact_ladder(px: float, token: str) -> None:
    assert snap_item_gap(px) == token


@pytest.mark.parametrize(
    ("px", "token"),
    [(4, "XS"), (8, "S"), (12, "S"), (18, "L"), (26, "XL"), (50, "XXL")],
)
def test_snap_item_gap_nearest(px: float, token: str) -> None:
    assert snap_item_gap(px) == token


# ----------------------------------------------------------------------
# snap_padding — uniform single token / V-H pair / drop+note.
# ----------------------------------------------------------------------


def test_snap_padding_uniform() -> None:
    assert snap_padding((20, 20, 20, 20)) == ("20px", None)


def test_snap_padding_uniform_zero_is_omitted() -> None:
    assert snap_padding((0, 0, 0, 0)) == (None, None)


def test_snap_padding_symmetric_pair() -> None:
    # (top=0, right=10, bottom=0, left=10) -> vertical 0, horizontal 10.
    assert snap_padding((0, 10, 0, 10)) == ("0px-10px", None)


def test_snap_padding_unsupported_pair_drops_with_note() -> None:
    # (10 vertical, 20 horizontal) is not in the supported pair set.
    token, note = snap_padding((10, 20, 10, 20))
    assert token is None
    assert note is not None and "padding" in note


def test_snap_padding_irregular_drops_with_note() -> None:
    token, note = snap_padding((5, 10, 5, 30))
    assert token is None
    assert note is not None


def test_snap_padding_none() -> None:
    assert snap_padding(None) == (None, None)


# ----------------------------------------------------------------------
# resolve_prism_layout — primitive choice.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("direction", [None, "single", "stack"])
def test_resolve_non_flow_returns_none(direction: str | None) -> None:
    """Single-child / overlap-stack containers warrant no flex wrapper."""
    analysis = LayoutAnalysis(direction=direction)
    assert resolve_prism_layout({}, analysis, []) is None


def test_resolve_row_is_flexlayout_without_flexdirection() -> None:
    """Row is FlexLayout's default direction, so it is omitted."""
    out = resolve_prism_layout({}, _auto("row", gap=10.0), [])
    assert out is not None
    assert out.component == "FlexLayout"
    assert "flexDirection" not in out.props
    assert out.props["itemGap"] == "S"
    assert out.source == "figma_auto_layout"
    assert out.confidence == 1.0


def test_resolve_column_pure_stack_is_stackinglayout() -> None:
    """A vertical stack with no align/justify -> StackingLayout (which
    has no flexDirection/alignItems/justifyContent props)."""
    out = resolve_prism_layout({}, _auto("column", gap=15.0), [])
    assert out is not None
    assert out.component == "StackingLayout"
    assert out.props == {"itemGap": "M"}


def test_resolve_column_with_center_align_is_flexlayout() -> None:
    out = resolve_prism_layout(
        {}, _auto("column", align_items="center", gap=5.0), []
    )
    assert out is not None
    assert out.component == "FlexLayout"
    assert out.props["flexDirection"] == "column"
    assert out.props["alignItems"] == "center"


def test_resolve_column_with_justify_is_flexlayout() -> None:
    out = resolve_prism_layout(
        {}, _auto("column", justify_content="space-between"), []
    )
    assert out is not None
    assert out.component == "FlexLayout"
    assert out.props["flexDirection"] == "column"
    assert out.props["justifyContent"] == "space-between"


def test_resolve_grid_is_flexlayout_with_wrap_and_note() -> None:
    out = resolve_prism_layout({}, _auto("grid", gap=20.0), [])
    assert out is not None
    assert out.component == "FlexLayout"
    assert out.props["flexWrap"] == "wrap"
    assert any("GRID" in n for n in out.notes)


# ----------------------------------------------------------------------
# resolve_prism_layout — prop mapping + default omission.
# ----------------------------------------------------------------------


def test_resolve_align_justify_css_to_prism_remap() -> None:
    out = resolve_prism_layout(
        {}, _auto("row", align_items="start", justify_content="end"), []
    )
    assert out is not None
    assert out.props["alignItems"] == "flex-start"
    assert out.props["justifyContent"] == "flex-end"


def test_resolve_omits_default_stretch_and_start() -> None:
    """``alignItems=stretch`` and ``justifyContent=start`` are the CSS
    flex defaults -> omitted to keep the spec minimal."""
    out = resolve_prism_layout(
        {}, _auto("row", align_items="stretch", justify_content="start"), []
    )
    assert out is not None
    assert "alignItems" not in out.props
    assert "justifyContent" not in out.props


def test_resolve_zero_gap_emits_none_token() -> None:
    out = resolve_prism_layout({}, _auto("row", gap=0.0), [])
    assert out is not None
    assert out.props["itemGap"] == "none"


def test_resolve_gap_none_omits_itemgap() -> None:
    out = resolve_prism_layout({}, _auto("row", gap=None), [])
    assert out is not None
    assert "itemGap" not in out.props


def test_resolve_padding_from_auto_layout_node() -> None:
    node = {
        "layoutMode": "HORIZONTAL",
        "paddingTop": 20,
        "paddingRight": 20,
        "paddingBottom": 20,
        "paddingLeft": 20,
    }
    out = resolve_prism_layout(node, _auto("row", gap=10.0), [])
    assert out is not None
    assert out.props["padding"] == "20px"


def test_resolve_layout_wrap_sets_flexwrap() -> None:
    out = resolve_prism_layout({"layoutWrap": "WRAP"}, _auto("row"), [])
    assert out is not None
    assert out.props["flexWrap"] == "wrap"


def test_resolve_geometry_source_when_not_auto_layout() -> None:
    analysis = LayoutAnalysis(
        direction="row", gap=8.0, confidence=0.78, rationale="row score=0.78"
    )
    out = resolve_prism_layout({}, analysis, [])
    assert out is not None
    assert out.source == "geometry"
    assert out.confidence == 0.78


# ----------------------------------------------------------------------
# layout_for_container — analyze_layout + resolve in one call.
# ----------------------------------------------------------------------


def test_layout_for_container_end_to_end() -> None:
    node = {
        "layoutMode": "VERTICAL",
        "itemSpacing": 20,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 300, "height": 200},
    }
    children = [
        {"id": "a", "absoluteBoundingBox": {"x": 0, "y": 0, "width": 300, "height": 40}},
        {"id": "b", "absoluteBoundingBox": {"x": 0, "y": 60, "width": 300, "height": 40}},
    ]
    out = layout_for_container(node, children)
    assert out is not None
    assert out.component == "StackingLayout"
    assert out.props["itemGap"] == "L"


# ----------------------------------------------------------------------
# Model.
# ----------------------------------------------------------------------


def test_prism_layout_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        PrismLayout(component="FlexLayout", source="geometry", bogus=1)  # type: ignore[call-arg]


# ----------------------------------------------------------------------
# P4 follow-up #4 — component-aware padding (StackingLayout wide set,
# ContainerLayout set, asymmetric "use style" escape).
# ----------------------------------------------------------------------


def test_snap_padding_flexlayout_rejects_wide_pair() -> None:
    # (10 vertical, 20 horizontal) is NOT in the narrow FlexLayout set.
    assert snap_padding((10, 20, 10, 20), "FlexLayout")[0] is None


def test_snap_padding_stackinglayout_accepts_wide_pair() -> None:
    # The same pair IS valid for StackingLayout's far wider union.
    assert snap_padding((10, 20, 10, 20), "StackingLayout") == ("10px-20px", None)


def test_snap_padding_stackinglayout_accepts_30_40_pair() -> None:
    assert snap_padding((30, 40, 30, 40), "StackingLayout") == ("30px-40px", None)


def test_snap_padding_container_uniform() -> None:
    assert snap_padding((20, 20, 20, 20), "ContainerLayout") == ("20px", None)


def test_snap_padding_container_supported_pair() -> None:
    assert snap_padding((0, 20, 0, 20), "ContainerLayout") == ("0px-20px", None)


def test_snap_padding_asymmetric_emits_style_escape() -> None:
    # top != bottom -> no token can express it; the note tells the caller
    # to fall back to a structured style prop (not silently dropped).
    token, note = snap_padding((20, 20, 0, 20), "StackingLayout")
    assert token is None
    assert note is not None and "use style" in note


# ----------------------------------------------------------------------
# P4 follow-up #2 — detect_fill_children (FlexItem flexGrow signal).
# ----------------------------------------------------------------------


def test_detect_fill_children_layout_grow() -> None:
    children = [
        {"id": "a"},
        {"id": "b", "layoutGrow": 1},
        {"id": "c", "layoutGrow": 0},
    ]
    assert detect_fill_children(children, "row") == ["b"]


def test_detect_fill_children_row_uses_horizontal_fill() -> None:
    children = [
        {"id": "menu", "layoutSizingHorizontal": "HUG"},
        {"id": "table", "layoutSizingHorizontal": "FILL"},
    ]
    assert detect_fill_children(children, "row") == ["table"]


def test_detect_fill_children_column_ignores_horizontal_fill() -> None:
    # In a column, a child filling its *width* is cross-axis, NOT flexGrow.
    children = [{"id": "x", "layoutSizingHorizontal": "FILL"}]
    assert detect_fill_children(children, "column") == []
    children2 = [{"id": "x", "layoutSizingVertical": "FILL"}]
    assert detect_fill_children(children2, "column") == ["x"]


def test_detect_fill_children_none() -> None:
    assert detect_fill_children([{"id": "a"}, {"id": "b"}], "row") == []


def test_resolve_row_with_fill_child_sets_fill_ids() -> None:
    children = [
        {"id": "menu"},
        {"id": "table", "layoutGrow": 1},
        {"id": "filters"},
    ]
    out = resolve_prism_layout({}, _auto("row", gap=10.0), children)
    assert out is not None
    assert out.component == "FlexLayout"
    assert out.fill_child_ids == ["table"]


def test_resolve_column_stack_with_fill_upgrades_to_flexlayout() -> None:
    # A pure vertical stack that has a filling child can't stay a
    # StackingLayout (no FlexItem there) -> upgrade to FlexLayout column.
    children = [{"id": "head"}, {"id": "body", "layoutGrow": 1}]
    out = resolve_prism_layout({}, _auto("column", gap=15.0), children)
    assert out is not None
    assert out.component == "FlexLayout"
    assert out.props["flexDirection"] == "column"
    assert out.fill_child_ids == ["body"]


def test_resolve_pure_stack_without_fill_has_no_fill_ids() -> None:
    out = resolve_prism_layout({}, _auto("column", gap=15.0), [{"id": "a"}])
    assert out is not None
    assert out.component == "StackingLayout"
    assert out.fill_child_ids == []


# ----------------------------------------------------------------------
# P4 follow-up #3 — ContainerLayout for styled non-flow boxes.
# ----------------------------------------------------------------------


def _white_box(**extra: Any) -> dict[str, Any]:
    node = {"fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]}
    node.update(extra)
    return node


def test_container_layout_white_single_child() -> None:
    # direction "single" is non-flow; a white styled box -> ContainerLayout.
    out = resolve_prism_layout(
        _white_box(), LayoutAnalysis(direction="single"), []
    )
    assert out is not None
    assert out.component == "ContainerLayout"
    assert out.props["backgroundColor"] == "white"


def test_container_layout_dark_bg() -> None:
    node = {"fills": [{"type": "SOLID", "color": {"r": 0.05, "g": 0.05, "b": 0.07}}]}
    out = resolve_prism_layout(node, LayoutAnalysis(direction="stack"), [])
    assert out is not None
    assert out.props["backgroundColor"] == "dark"


def test_container_layout_transparent_when_bordered_no_fill() -> None:
    node = {"strokes": [{"type": "SOLID", "color": {"r": 0.8, "g": 0.8, "b": 0.8}}]}
    out = resolve_prism_layout(node, LayoutAnalysis(direction="single"), [])
    assert out is not None
    assert out.props["backgroundColor"] == "transparent"
    assert out.props["border"] == "true"


def test_container_layout_colored_bg_is_not_emitted() -> None:
    # A grey surface (#EDF0F2-ish) is NOT ContainerLayout-white; leave it on
    # box_style for the P5 token pass.
    node = {
        "fills": [{"type": "SOLID", "color": {"r": 0.93, "g": 0.94, "b": 0.95}}]
    }
    assert resolve_prism_layout(node, LayoutAnalysis(direction="single"), []) is None


def test_container_layout_border_flag_on_white_box() -> None:
    node = _white_box(
        strokes=[{"type": "SOLID", "color": {"r": 0.8, "g": 0.8, "b": 0.8}}]
    )
    out = resolve_prism_layout(node, LayoutAnalysis(direction="single"), [])
    assert out is not None
    assert out.props["backgroundColor"] == "white"
    assert out.props["border"] == "true"


def test_unstyled_non_flow_still_returns_none() -> None:
    assert resolve_prism_layout({}, LayoutAnalysis(direction="single"), []) is None


# ----------------------------------------------------------------------
# P4 follow-up #1 — detect_page_shell (conservative geometric classifier).
# ----------------------------------------------------------------------


def _page(**extra: Any) -> dict[str, Any]:
    node = {"absoluteBoundingBox": {"x": 0, "y": 0, "width": 1440, "height": 900}}
    node.update(extra)
    return node


def _bb(x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {"absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h}}


def test_detect_shell_header_body_is_header_footer_layout() -> None:
    children = [
        {"id": "hdr", **_bb(0, 0, 1440, 64)},
        {"id": "main", **_bb(0, 64, 1440, 836)},
    ]
    shell = detect_page_shell(_page(), children)
    assert isinstance(shell, PrismPageShell)
    assert shell.component == "HeaderFooterLayout"
    assert shell.slots == {"header": "hdr", "bodyContent": "main"}


def test_detect_shell_header_body_footer() -> None:
    children = [
        {"id": "hdr", **_bb(0, 0, 1440, 64)},
        {"id": "main", **_bb(0, 64, 1440, 776)},
        {"id": "ftr", **_bb(0, 860, 1440, 40)},
    ]
    shell = detect_page_shell(_page(), children)
    assert shell is not None
    assert shell.component == "HeaderFooterLayout"
    assert shell.slots["footer"] == "ftr"


def test_detect_shell_nav_body_is_left_nav_layout() -> None:
    children = [
        {"id": "nav", **_bb(0, 0, 240, 900)},
        {"id": "body", **_bb(240, 0, 1200, 900)},
    ]
    shell = detect_page_shell(_page(), children)
    assert shell is not None
    assert shell.component == "LeftNavLayout"
    assert shell.slots == {"leftPanel": "nav", "rightBodyContent": "body"}


def test_detect_shell_header_nav_body_is_main_page_layout() -> None:
    children = [
        {"id": "hdr", **_bb(0, 0, 1440, 64)},
        {"id": "nav", **_bb(0, 64, 240, 836)},
        {"id": "body", **_bb(240, 64, 1200, 836)},
    ]
    shell = detect_page_shell(_page(), children)
    assert shell is not None
    assert shell.component == "MainPageLayout"
    assert shell.slots == {"header": "hdr", "leftPanel": "nav", "body": "body"}


def test_detect_shell_too_small_returns_none() -> None:
    small = {"absoluteBoundingBox": {"x": 0, "y": 0, "width": 500, "height": 400}}
    children = [
        {"id": "hdr", **_bb(0, 0, 500, 40)},
        {"id": "main", **_bb(0, 40, 500, 360)},
    ]
    assert detect_page_shell(small, children) is None


def test_detect_shell_single_child_returns_none() -> None:
    children = [{"id": "only", **_bb(0, 0, 1440, 900)}]
    assert detect_page_shell(_page(), children) is None
