"""End-to-end tests: the walker attaches ``LayoutNode.prism_layout`` (P4).

Complements the pure-mapping suite in ``test_figma_layout.py`` by driving
the real :func:`prism_mcp.figma.walker.walk_tree` over small synthetic
trees and asserting:

* structural containers get a ``prism_layout`` (FlexLayout / StackingLayout),
* keyed component leaves do **not** (the role gate),
* the geometry fallback fires for non-auto-layout containers,
* the field surfaces through the lean response (``layout_tree`` verbatim).
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma.models import leanify_tree_mapping
from prism_mcp.figma.walker import walk_tree

_CONTAINER_ROLES = {"layout-container", "composed-region"}


def _instance(id_: str, name: str, x: float, w: float) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "type": "INSTANCE",
        "componentId": "10:1",
        "absoluteBoundingBox": {"x": x, "y": 10, "width": w, "height": 40},
        "visible": True,
    }


def _toolbar_tree() -> dict[str, Any]:
    """A HORIZONTAL auto-layout toolbar with two heterogeneous instances.

    Heterogeneous child names dodge any homogeneous cluster pattern
    (button-group / stat-list) so the root stays a plain container.
    """
    return {
        "id": "1:1",
        "name": "Toolbar",
        "type": "FRAME",
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 10,
        "primaryAxisAlignItems": "SPACE_BETWEEN",
        "counterAxisAlignItems": "CENTER",
        "paddingTop": 20,
        "paddingRight": 20,
        "paddingBottom": 20,
        "paddingLeft": 20,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 60},
        "visible": True,
        "children": [
            _instance("1:2", "Input/Select", 20, 120),
            _instance("1:3", "Action/Button", 290, 90),
        ],
    }


def _walk(tree: dict[str, Any]) -> Any:
    return walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )


def test_auto_layout_container_gets_prism_layout() -> None:
    mapping = _walk(_toolbar_tree())
    root = next(n for n in mapping.layout_tree if n.id == "1:1")
    assert root.role in _CONTAINER_ROLES
    assert root.prism_layout is not None
    pl = root.prism_layout
    assert pl.component == "FlexLayout"
    assert pl.source == "figma_auto_layout"
    assert pl.props.get("justifyContent") == "space-between"
    assert pl.props.get("alignItems") == "center"
    assert pl.props.get("itemGap") == "S"  # itemSpacing 10 -> S
    assert pl.props.get("padding") == "20px"


def test_component_instance_leaf_gets_no_prism_layout() -> None:
    """A keyed INSTANCE with its OWN auto-layout is a component, not a
    ``<div>`` — the role gate must keep ``prism_layout`` off it."""
    tree = {
        "id": "100:1",
        "name": "Action/Button",
        "type": "INSTANCE",
        "componentId": "10:1",
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 8,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
        "visible": True,
        "children": [
            {
                "id": f"I100:1;master:{i}",
                "name": f"part-{i}",
                "type": "TEXT",
                "characters": f"x{i}",
                "absoluteBoundingBox": {
                    "x": i * 40,
                    "y": 0,
                    "width": 40,
                    "height": 40,
                },
                "visible": True,
            }
            for i in range(3)
        ],
    }
    mapping = _walk(tree)
    node = next(n for n in mapping.layout_tree if n.id == "100:1")
    assert node.role not in _CONTAINER_ROLES
    assert node.prism_layout is None


def test_geometry_fallback_container_gets_prism_layout() -> None:
    """A non-auto-layout FRAME laid out as a row by geometry still
    resolves — with ``source="geometry"``."""
    tree = {
        "id": "2:1",
        "name": "Row",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 60},
        "visible": True,
        "children": [
            _instance("2:2", "Input/Select", 0, 100),
            _instance("2:3", "Action/Button", 120, 100),
        ],
    }
    mapping = _walk(tree)
    root = next(n for n in mapping.layout_tree if n.id == "2:1")
    assert root.role in _CONTAINER_ROLES
    assert root.prism_layout is not None
    assert root.prism_layout.component == "FlexLayout"
    assert root.prism_layout.source == "geometry"


def test_prism_layout_surfaces_in_lean_response() -> None:
    mapping = _walk(_toolbar_tree())
    lean = leanify_tree_mapping(mapping, "lean")
    root_lean = next(n for n in lean["layout_tree"] if n["id"] == "1:1")
    assert root_lean["prism_layout"] is not None
    assert root_lean["prism_layout"]["component"] == "FlexLayout"
    assert (
        root_lean["prism_layout"]["props"]["justifyContent"] == "space-between"
    )


def test_lean_layout_tree_matches_full_dump() -> None:
    """The lean response passes ``layout_tree`` through verbatim, so the
    new field round-trips identically on both sides."""
    mapping = _walk(_toolbar_tree())
    lean = leanify_tree_mapping(mapping, "lean")
    assert lean["layout_tree"] == mapping.model_dump()["layout_tree"]


# ----------------------------------------------------------------------
# P4 follow-up #1 — page shell on the route-anchoring page-scale frame.
# ----------------------------------------------------------------------


def _shell_child(id_: str, name: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    """A FRAME region that survives the walker (visible fill) with one text."""
    return {
        "id": id_,
        "name": name,
        "type": "FRAME",
        "fills": [{"type": "SOLID", "color": {"r": 0.93, "g": 0.94, "b": 0.95}}],
        "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h},
        "visible": True,
        "children": [
            {
                "id": f"{id_}:t",
                "name": f"{name} text",
                "type": "TEXT",
                "characters": name,
                "absoluteBoundingBox": {
                    "x": x + 8,
                    "y": y + 8,
                    "width": 80,
                    "height": 16,
                },
                "visible": True,
            }
        ],
    }


def test_page_shell_main_page_layout_on_root() -> None:
    """A 1440x900 page with header + left-nav + body -> MainPageLayout,
    and the root gets ``prism_shell`` (not a redundant ``prism_layout``)."""
    tree = {
        "id": "9:1",
        "name": "Cluster Detail Page",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 1440, "height": 900},
        "visible": True,
        "children": [
            _shell_child("9:2", "Header", 0, 0, 1440, 64),
            _shell_child("9:3", "Nav", 0, 64, 240, 836),
            _shell_child("9:4", "Body", 240, 64, 1200, 836),
        ],
    }
    mapping = _walk(tree)
    root = next(n for n in mapping.layout_tree if n.id == "9:1")
    assert root.prism_shell is not None
    assert root.prism_shell.component == "MainPageLayout"
    assert root.prism_shell.slots == {
        "header": "9:2",
        "leftPanel": "9:3",
        "body": "9:4",
    }
    # Shell takes precedence — no redundant flex wrapper on the same node.
    assert root.prism_layout is None
    # And it surfaces in the lean response (layout_tree passes verbatim).
    lean = leanify_tree_mapping(mapping, "lean")
    root_lean = next(n for n in lean["layout_tree"] if n["id"] == "9:1")
    assert root_lean["prism_shell"]["component"] == "MainPageLayout"


def test_fill_child_ids_use_region_ids() -> None:
    """A row whose middle child has ``layoutGrow=1`` surfaces that child's
    region id in ``fill_child_ids`` (FlexItem flexGrow)."""
    tree = {
        "id": "3:1",
        "name": "Page Body Row",
        "type": "FRAME",
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 10,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 600, "height": 400},
        "visible": True,
        "children": [
            _instance("3:2", "Menu/List", 0, 120),
            {
                **_instance("3:3", "Data/Table", 130, 340),
                "layoutGrow": 1,
            },
            _instance("3:4", "Filter/Group", 480, 110),
        ],
    }
    mapping = _walk(tree)
    root = next(n for n in mapping.layout_tree if n.id == "3:1")
    assert root.prism_layout is not None
    assert root.prism_layout.fill_child_ids == ["3:3"]
