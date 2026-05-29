"""Tests for the pattern detectors (Phase 4).

Each detector gets a positive (matches) and a negative (doesn't
match) case using the patterns described in design doc §4.5 and the
worked examples in §8.

The walker-integration cases live in ``test_figma_walker_filter.py``
and the soon-to-be ``test_figma_walker.py`` golden tests.
"""

from __future__ import annotations

import pytest

from prism_mcp.figma.patterns import (
    PATTERNS,
    match_button_group,
    match_column_of_cells,
    match_icon,
    match_kpi_tile,
    match_stat_list,
    match_tab_strip,
)


def _bbox(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


# --------------------------------------------------------------------------
# Pattern registry ordering — design doc §4.3 Pass 5 + §4.5.
# --------------------------------------------------------------------------


def test_patterns_priority_order_is_icon_first() -> None:
    """``match_icon`` runs before any cluster pattern — icons
    collapse leaf subtrees and shouldn't share the cluster path."""
    assert PATTERNS[0] is match_icon


def test_patterns_priority_order_table_column_before_buttons() -> None:
    """``column-of-cells`` is name-anchored (``Table/Column``) and
    runs before name-loose patterns to avoid false-positive folds."""
    cluster_order = PATTERNS[1:]
    column_idx = cluster_order.index(match_column_of_cells)
    button_idx = cluster_order.index(match_button_group)
    tabs_idx = cluster_order.index(match_tab_strip)
    statlist_idx = cluster_order.index(match_stat_list)
    kpi_idx = cluster_order.index(match_kpi_tile)
    assert column_idx < button_idx < tabs_idx < statlist_idx < kpi_idx


# --------------------------------------------------------------------------
# match_icon.
# --------------------------------------------------------------------------


def test_match_icon_positive_vector_node() -> None:
    """A bare VECTOR node is always an icon."""
    match = match_icon(
        {
            "id": "1:1",
            "name": "vector-fragment",
            "type": "VECTOR",
            "absoluteBoundingBox": _bbox(0, 0, 16, 16),
        }
    )
    assert match is not None
    assert match.kind == "icon"
    assert match.absorbed_reason == "icon_internal"


def test_match_icon_positive_boolean_operation() -> None:
    match = match_icon(
        {
            "id": "1:1",
            "name": "bo",
            "type": "BOOLEAN_OPERATION",
            "absoluteBoundingBox": _bbox(0, 0, 24, 24),
            "children": [
                {
                    "id": "1:2",
                    "type": "RECTANGLE",
                    "absoluteBoundingBox": _bbox(0, 0, 12, 4),
                },
                {
                    "id": "1:3",
                    "type": "RECTANGLE",
                    "absoluteBoundingBox": _bbox(0, 8, 12, 4),
                },
                {
                    "id": "1:4",
                    "type": "RECTANGLE",
                    "absoluteBoundingBox": _bbox(0, 16, 12, 4),
                },
            ],
        }
    )
    assert match is not None
    assert match.kind == "icon"


def test_match_icon_positive_name_prefix() -> None:
    """A FRAME named ``icon/foo`` is an icon regardless of size."""
    match = match_icon(
        {
            "id": "1:1",
            "name": "icon/external-link",
            "type": "FRAME",
            "absoluteBoundingBox": _bbox(0, 0, 64, 64),
        }
    )
    assert match is not None
    assert match.kind == "icon"


def test_match_icon_positive_small_with_icon_children() -> None:
    """≤24px container whose descendants are all icon-internal
    types collapses into one icon."""
    match = match_icon(
        {
            "id": "1:1",
            "name": "wrapper",
            "type": "FRAME",
            "absoluteBoundingBox": _bbox(0, 0, 16, 16),
            "children": [
                {"id": "1:2", "type": "VECTOR"},
                {"id": "1:3", "type": "RECTANGLE"},
            ],
        }
    )
    assert match is not None
    assert match.kind == "icon"


def test_match_icon_negative_large_frame_with_text() -> None:
    """A 320x40 FRAME with TEXT children is NOT an icon."""
    match = match_icon(
        {
            "id": "1:1",
            "name": "Header",
            "type": "FRAME",
            "absoluteBoundingBox": _bbox(0, 0, 320, 40),
            "children": [{"id": "1:2", "type": "TEXT", "characters": "Title"}],
        }
    )
    assert match is None


# --------------------------------------------------------------------------
# match_stat_list — design doc §4.5.1, §8.1.
# --------------------------------------------------------------------------


def _row_frame(node_id: str, name: str, text: str, n_rects: int = 0) -> dict:
    children: list[dict] = []
    for i in range(n_rects):
        children.append(
            {
                "id": f"{node_id}:rect{i}",
                "type": "RECTANGLE",
                "name": "Row",
                "absoluteBoundingBox": _bbox(0, 0, 320, 40),
            }
        )
    children.append(
        {
            "id": f"{node_id}:text",
            "type": "TEXT",
            "name": "label",
            "characters": text,
        }
    )
    return {
        "id": node_id,
        "name": name,
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 320, 40),
        "children": children,
    }


def test_match_stat_list_positive_three_rows() -> None:
    """The §8.1 Cluster Details GROUP shape."""
    node = {
        "id": "626:988",
        "name": "Cluster Details",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 320, 120),
        "children": [
            _row_frame("626:989", "Row", "CBTest", 1),
            _row_frame("626:992", "Row", "Intern19Share", 2),
            _row_frame("626:996", "Row", "SyslogShare", 1),
        ],
    }
    match = match_stat_list(node)
    assert match is not None
    assert match.kind == "stat-list"
    assert match.content_slots["items"] == [
        "CBTest",
        "Intern19Share",
        "SyslogShare",
    ]


def test_match_stat_list_negative_only_one_row() -> None:
    """A single row isn't a list."""
    node = {
        "id": "x",
        "name": "Cluster Details",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 320, 40),
        "children": [_row_frame("x:1", "Row", "OnlyOne")],
    }
    assert match_stat_list(node) is None


def test_match_stat_list_negative_non_row_children() -> None:
    """Children that aren't FRAMEs named Row/Item fail the check."""
    node = {
        "id": "x",
        "name": "Cluster Details",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 320, 80),
        "children": [
            {"id": "x:1", "type": "INSTANCE", "name": "Tile"},
            {"id": "x:2", "type": "INSTANCE", "name": "Tile"},
        ],
    }
    assert match_stat_list(node) is None


def test_match_stat_list_negative_text_count_wrong() -> None:
    """A row with 2 TEXT children isn't the stat-list shape."""
    node = {
        "id": "x",
        "name": "Rows",
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 320, 80),
        "children": [
            {
                "id": "x:1",
                "type": "FRAME",
                "name": "Row",
                "children": [
                    {"id": "x:1a", "type": "TEXT", "characters": "Label"},
                    {"id": "x:1b", "type": "TEXT", "characters": "Value"},
                ],
            },
            {
                "id": "x:2",
                "type": "FRAME",
                "name": "Row",
                "children": [
                    {"id": "x:2a", "type": "TEXT", "characters": "Label"},
                    {"id": "x:2b", "type": "TEXT", "characters": "Value"},
                ],
            },
        ],
    }
    assert match_stat_list(node) is None


# --------------------------------------------------------------------------
# match_column_of_cells — design doc §4.5.2.
# --------------------------------------------------------------------------


def _cell_instance(node_id: str, text: str) -> dict:
    return {
        "id": node_id,
        "type": "INSTANCE",
        "name": "Table/Table Cell",
        "absoluteBoundingBox": _bbox(0, 0, 200, 40),
        "children": [
            {"id": f"{node_id}:t", "type": "TEXT", "characters": text},
        ],
    }


def test_match_column_of_cells_positive() -> None:
    node = {
        "id": "1:1",
        "name": "Table/Column",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 600),
        "children": [
            {
                "id": "1:2",
                "type": "INSTANCE",
                "name": "Table/Table Title",
                "children": [
                    {"id": "1:2a", "type": "TEXT", "characters": "Stage"},
                ],
            },
            _cell_instance("1:3", "Prospect"),
            _cell_instance("1:4", "Qualified"),
            _cell_instance("1:5", "Proposed"),
            _cell_instance("1:6", "Won"),
            _cell_instance("1:7", "Lost"),
        ],
    }
    match = match_column_of_cells(node)
    assert match is not None
    assert match.kind == "table-column"
    assert match.content_slots["header"] == "Stage"
    assert match.content_slots["cell_count"] == 5


def test_match_column_of_cells_negative_wrong_frame_name() -> None:
    node = {
        "id": "1:1",
        "name": "Random Group",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 600),
        "children": [
            {
                "id": "1:2",
                "type": "INSTANCE",
                "name": "Table/Table Title",
                "children": [
                    {"id": "1:2a", "type": "TEXT", "characters": "Stage"},
                ],
            },
            _cell_instance("1:3", "Prospect"),
        ],
    }
    assert match_column_of_cells(node) is None


def test_match_column_of_cells_negative_no_title() -> None:
    node = {
        "id": "1:1",
        "name": "Table/Column",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 600),
        "children": [
            _cell_instance("1:3", "Prospect"),
            _cell_instance("1:4", "Qualified"),
        ],
    }
    assert match_column_of_cells(node) is None


# --------------------------------------------------------------------------
# match_tab_strip — design doc §4.5.3.
# --------------------------------------------------------------------------


def _tab_instance(node_id: str, name: str, label: str) -> dict:
    return {
        "id": node_id,
        "type": "INSTANCE",
        "name": name,
        "absoluteBoundingBox": _bbox(0, 0, 80, 32),
        "children": [
            {"id": f"{node_id}:t", "type": "TEXT", "characters": label}
        ],
    }


def test_match_tab_strip_positive_three_tabs() -> None:
    node = {
        "id": "1:1",
        "name": "Subheader/Tabs",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 240, 32),
        "children": [
            _tab_instance("1:2", "Tab/Active", "Overview"),
            _tab_instance("1:3", "Tab/Inactive", "Details"),
            _tab_instance("1:4", "Tab/Inactive", "History"),
        ],
    }
    match = match_tab_strip(node)
    assert match is not None
    assert match.kind == "tab-strip"
    assert match.content_slots["items"] == ["Overview", "Details", "History"]


def test_match_tab_strip_negative_one_tab() -> None:
    node = {
        "id": "1:1",
        "name": "Subheader/Tabs",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 80, 32),
        "children": [
            _tab_instance("1:2", "Tab/Active", "Overview"),
        ],
    }
    assert match_tab_strip(node) is None


# --------------------------------------------------------------------------
# match_button_group — design doc §4.5.4.
# --------------------------------------------------------------------------


def _button_instance(node_id: str, label: str) -> dict:
    return {
        "id": node_id,
        "type": "INSTANCE",
        "name": "Action/Button/Primary",
        "absoluteBoundingBox": _bbox(0, 0, 80, 32),
        "children": [
            {"id": f"{node_id}:t", "type": "TEXT", "characters": label}
        ],
    }


def test_match_button_group_positive_two_buttons() -> None:
    node = {
        "id": "1:1",
        "name": "Actions",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 180, 40),
        "children": [
            _button_instance("1:2", "Save"),
            _button_instance("1:3", "Cancel"),
        ],
    }
    match = match_button_group(node)
    assert match is not None
    assert match.kind == "button-group"


def test_match_button_group_negative_one_button() -> None:
    node = {
        "id": "1:1",
        "name": "Actions",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 80, 32),
        "children": [_button_instance("1:2", "Save")],
    }
    assert match_button_group(node) is None


def test_match_button_group_negative_loose_layout() -> None:
    """A modal with 2 buttons but huge surrounding area doesn't
    qualify — the bbox tightness predicate fails."""
    node = {
        "id": "1:1",
        "name": "Modal",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 800, 600),
        "children": [
            _button_instance("1:2", "Save"),
            _button_instance("1:3", "Cancel"),
        ],
    }
    assert match_button_group(node) is None


# --------------------------------------------------------------------------
# match_kpi_tile — design doc §4.5.5.
# --------------------------------------------------------------------------


def test_match_kpi_tile_positive_big_value_small_label() -> None:
    node = {
        "id": "1:1",
        "name": "Active Clusters",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 120),
        "children": [
            {
                "id": "1:2",
                "type": "TEXT",
                "characters": "12",
                "style": {"fontSize": 32},
            },
            {
                "id": "1:3",
                "type": "TEXT",
                "characters": "Active Clusters",
                "style": {"fontSize": 12},
            },
        ],
    }
    match = match_kpi_tile(node)
    assert match is not None
    assert match.content_slots["value"] == "12"
    assert match.content_slots["label"] == "Active Clusters"


def test_match_kpi_tile_negative_wrong_aspect_ratio() -> None:
    """Very wide bbox (more than 3:1) isn't a tile."""
    node = {
        "id": "1:1",
        "name": "Wide Banner",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 1000, 80),
        "children": [
            {
                "id": "1:2",
                "type": "TEXT",
                "characters": "12",
                "style": {"fontSize": 32},
            },
            {
                "id": "1:3",
                "type": "TEXT",
                "characters": "label",
                "style": {"fontSize": 12},
            },
        ],
    }
    assert match_kpi_tile(node) is None


def test_match_kpi_tile_negative_no_big_text() -> None:
    node = {
        "id": "1:1",
        "name": "Tile",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 120),
        "children": [
            {
                "id": "1:2",
                "type": "TEXT",
                "characters": "small",
                "style": {"fontSize": 12},
            },
            {
                "id": "1:3",
                "type": "TEXT",
                "characters": "smaller",
                "style": {"fontSize": 10},
            },
        ],
    }
    assert match_kpi_tile(node) is None


def test_match_kpi_tile_negative_page_scale_with_one_h1() -> None:
    """A 1280x800 page-scale FRAME with one H1 + one label is NOT a
    kpi-tile.

    Regression for the catastrophic over-match observed against the
    real "Active Cluster Page" (node 624:6826) and "E01 - Share
    Summary" (node 667:211) — both 1280-wide pages where a single
    page-title TEXT at ~26pt + body labels at ~14pt caused the
    walker to fold the entire 400+ node tree into one kpi-tile
    agenda row. The size cap on
    :data:`prism_mcp.figma.patterns._KPI_TILE_MAX_EDGE` (400 px) is
    the gate that catches this.
    """
    node = {
        "id": "624:6826",
        "name": "Active Cluster Page",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 1280, 800),
        "children": [
            {
                "id": "624:6827",
                "type": "TEXT",
                "name": "Title",
                "characters": "Licenses",
                "style": {"fontSize": 26},
            },
            {
                "id": "624:6828",
                "type": "TEXT",
                "name": "Page",
                "characters": "Page 1",
                "style": {"fontSize": 14},
            },
        ],
    }
    assert match_kpi_tile(node) is None


def test_match_kpi_tile_negative_too_many_descendants() -> None:
    """A small-ish FRAME with one H1 + one label is NOT a kpi-tile
    when the subtree exceeds the descendant cap.

    Defends against the case where a card or panel happens to share
    the kpi-tile size + text signature but actually contains many
    sub-clusters (icons, dividers, etc.) the walker should keep
    visible as their own regions.
    """
    children: list[dict] = [
        {
            "id": "1:2",
            "type": "TEXT",
            "name": "Value",
            "characters": "42",
            "style": {"fontSize": 32},
        },
        {
            "id": "1:3",
            "type": "TEXT",
            "name": "Label",
            "characters": "Active Clusters",
            "style": {"fontSize": 12},
        },
    ]
    for i in range(35):
        children.append(
            {
                "id": f"1:{100 + i}",
                "type": "RECTANGLE",
                "name": "Decoration",
                "absoluteBoundingBox": _bbox(0, 0, 50, 50),
            }
        )
    node = {
        "id": "1:1",
        "name": "Big card pretending to be a tile",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 300, 200),
        "children": children,
    }
    assert match_kpi_tile(node) is None


def test_match_kpi_tile_negative_h1_buried_too_deep() -> None:
    """When the H1 is buried below the depth cap it doesn't count.

    A real kpi-tile's value text is direct or 1-2 wrappers deep.
    If the only ≥24pt TEXT sits 5 levels deep, the candidate node is
    almost certainly a container hosting another component, not a
    tile.
    """
    deep_h1 = {
        "id": "1:99",
        "type": "TEXT",
        "characters": "Buried Heading",
        "style": {"fontSize": 28},
    }
    nested = deep_h1
    for i in range(5):
        nested = {
            "id": f"1:{50 + i}",
            "type": "FRAME",
            "name": f"wrapper-{i}",
            "absoluteBoundingBox": _bbox(0, 0, 200, 120),
            "children": [nested],
        }
    node = {
        "id": "1:1",
        "name": "Card",
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 200, 120),
        "children": [
            nested,
            {
                "id": "1:2",
                "type": "TEXT",
                "characters": "label",
                "style": {"fontSize": 12},
            },
        ],
    }
    assert match_kpi_tile(node) is None


# --------------------------------------------------------------------------
# Cross-pattern guarantee: nothing matches an empty node.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", PATTERNS)
def test_every_pattern_returns_none_on_empty_node(pattern) -> None:
    assert pattern({"id": "1:1", "type": "GROUP"}) is None
