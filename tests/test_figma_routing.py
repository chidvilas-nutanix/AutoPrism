"""Tests for the per-type router and FRAME-role classifier (Phase 3).

The router decides *what kind of work* each surviving node gets;
the role classifier specialises FRAMEs into one of four behaviour
buckets.

See ``docs/figma-page-to-prism-plan.md`` §4.4 and §4.4.1.
"""

from __future__ import annotations

import pytest

from prism_mcp.figma.routing import (
    FrameRole,
    RouterDecision,
    classify_frame_role,
    route_node,
)

# --------------------------------------------------------------------------
# RouterDecision per type — matches §4.4 table.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_type, expected",
    [
        ("DOCUMENT", RouterDecision.recurse),
        ("PAGE", RouterDecision.recurse),
        ("CANVAS", RouterDecision.recurse),
        ("INSTANCE", RouterDecision.map_and_stop),
        ("COMPONENT", RouterDecision.map_and_stop),
        ("COMPONENT_SET", RouterDecision.recurse),
        ("FRAME", RouterDecision.pattern_candidate),
        ("GROUP", RouterDecision.pattern_candidate),
        ("TRANSFORM_GROUP", RouterDecision.pattern_candidate),
        ("SECTION", RouterDecision.pattern_candidate),
        ("TEXT", RouterDecision.capture_as_slot),
        ("TEXT_PATH", RouterDecision.capture_as_slot),
        ("RECTANGLE", RouterDecision.capture_as_slot),
        ("ELLIPSE", RouterDecision.capture_as_slot),
        ("LINE", RouterDecision.capture_as_slot),
        ("VECTOR", RouterDecision.pattern_candidate),
        ("BOOLEAN_OPERATION", RouterDecision.pattern_candidate),
        ("STAR", RouterDecision.pattern_candidate),
        ("POLYGON", RouterDecision.pattern_candidate),
        ("REGULAR_POLYGON", RouterDecision.pattern_candidate),
        ("TABLE", RouterDecision.recurse),
        ("TABLE_CELL", RouterDecision.recurse),
    ],
)
def test_route_node_per_type(node_type: str, expected: RouterDecision) -> None:
    assert route_node({"type": node_type}) is expected


def test_route_node_unknown_type_falls_back_to_recurse() -> None:
    """Unknown / future SceneNode types must not crash the walker;
    they're treated as GROUP-equivalents and the walker logs them
    with reason ``unknown_type_fallback``."""
    assert route_node({"type": "FUTURE_SHAPE_2030"}) is RouterDecision.recurse


def test_route_node_missing_type_falls_back_to_recurse() -> None:
    """Robustness against malformed REST payloads — missing
    ``type`` should never bubble up as a KeyError."""
    assert route_node({"name": "Mystery"}) is RouterDecision.recurse


# --------------------------------------------------------------------------
# FrameRole — the four behaviour buckets for FRAMEs.
# --------------------------------------------------------------------------


def test_frame_role_slash_name_is_component_instance_equivalent() -> None:
    """Figma's ``Tile/Header`` convention mirrors a Prism component
    name and should map directly."""
    role = classify_frame_role(
        {"type": "FRAME", "name": "Tile/Header", "children": []}
    )
    assert role is FrameRole.component_instance_equivalent


def test_frame_role_deep_slash_name_is_component_instance_equivalent() -> None:
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Action/Button/Primary",
            "children": [],
        }
    )
    assert role is FrameRole.component_instance_equivalent


def test_frame_role_only_layout_children_is_layout_container() -> None:
    """A FRAME with FRAMEs and GROUPs (no INSTANCEs, no TEXT) is
    a pure layout container."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Workspace",
            "children": [
                {"type": "FRAME", "name": "Header"},
                {"type": "GROUP", "name": "Body"},
            ],
        }
    )
    assert role is FrameRole.layout_container


def test_frame_role_mixed_with_instance_is_composed_region() -> None:
    """A frame containing a mix that includes at least one
    INSTANCE is a composed region — map it AND recurse."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Card",
            "children": [
                {"type": "INSTANCE", "name": "Button"},
                {"type": "TEXT", "characters": "Hello"},
            ],
        }
    )
    assert role is FrameRole.composed_region


def test_frame_role_with_text_only_is_composed_region() -> None:
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Header",
            "children": [
                {"type": "TEXT", "characters": "Title"},
                {"type": "TEXT", "characters": "Subtitle"},
            ],
        }
    )
    assert role is FrameRole.composed_region


def test_frame_role_many_children_is_pattern_cluster() -> None:
    """A FRAME with 11+ children defers to pattern detection —
    long lists / table columns live here."""
    children = [
        {"type": "INSTANCE", "name": f"Table/Table Cell {i}"} for i in range(15)
    ]
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Long Column",
            "children": children,
        }
    )
    assert role is FrameRole.pattern_cluster


def test_frame_role_empty_frame_is_pattern_cluster() -> None:
    """An empty FRAME isn't decisive — pattern detection gets the
    final say (icon fallback / unknown wrapper)."""
    role = classify_frame_role({"type": "FRAME", "name": "Empty"})
    assert role is FrameRole.pattern_cluster


# --------------------------------------------------------------------------
# Visual-container promotion (the Status/Alert Banner failure mode).
#
# A FRAME with a visible fill / stroke / corner radius / shadow MUST
# classify as a composed-region so the walker emits a MappedRegion for
# it (carrying the box_style). Without this rule the grey rounded
# banner in the d02 share-summary page was silently dropped, which the
# user observed in their generated React as "plain text where the
# Cloud Connect Access info panel should be".
# --------------------------------------------------------------------------


def test_frame_role_visible_fill_promotes_to_composed_region() -> None:
    """A FRAME with one FRAME child that would otherwise classify as
    ``layout_container`` is promoted to ``composed_region`` when it
    has a visible SOLID fill — the canonical card / panel case."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Status/Alert Banner",
            "fills": [
                {
                    "type": "SOLID",
                    "color": {
                        "r": 0.929,
                        "g": 0.941,
                        "b": 0.949,
                        "a": 1.0,
                    },
                },
            ],
            "children": [
                {"type": "FRAME", "name": "Content"},
            ],
        }
    )
    assert role is FrameRole.composed_region


def test_frame_role_corner_radius_alone_promotes_to_composed_region() -> None:
    """A FRAME with no fill / stroke but a non-zero corner radius is
    still a visual container — designers occasionally use a rounded
    transparent FRAME as a clipping mask, which the LLM should be
    aware of."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Rounded Mask",
            "cornerRadius": 8,
            "children": [
                {"type": "FRAME", "name": "Inner"},
            ],
        }
    )
    assert role is FrameRole.composed_region


def test_frame_role_visible_stroke_promotes_to_composed_region() -> None:
    """A FRAME bordered with a visible SOLID stroke is a card / panel
    even without a fill — the border is the visual identity."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Bordered Panel",
            "strokes": [
                {
                    "type": "SOLID",
                    "color": {
                        "r": 0.1,
                        "g": 0.1,
                        "b": 0.1,
                        "a": 1.0,
                    },
                },
            ],
            "children": [
                {"type": "FRAME", "name": "Inner"},
            ],
        }
    )
    assert role is FrameRole.composed_region


def test_frame_role_no_visual_presence_stays_layout_container() -> None:
    """Negative case: a truly bare FRAME (no fill, no stroke, no
    corner radius, no shadow) with only layout children stays a
    pure layout container. This prevents the visual-container
    promotion from accidentally swallowing pure spacers."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Spacer Stack",
            "fills": [
                # An *invisible* fill MUST NOT promote the FRAME —
                # a fill with visible=false is just a paint slot
                # the designer turned off; the FRAME itself paints
                # nothing.
                {
                    "type": "SOLID",
                    "visible": False,
                    "color": {
                        "r": 1.0,
                        "g": 1.0,
                        "b": 1.0,
                        "a": 1.0,
                    },
                },
            ],
            "children": [
                {"type": "FRAME", "name": "Inner"},
            ],
        }
    )
    assert role is FrameRole.layout_container


def test_frame_role_visible_shadow_promotes_to_composed_region() -> None:
    """A FRAME with a visible drop shadow is an elevated surface
    (card, modal, popover) — visual presence even without fill."""
    role = classify_frame_role(
        {
            "type": "FRAME",
            "name": "Elevated Card",
            "effects": [
                {
                    "type": "DROP_SHADOW",
                    "color": {
                        "r": 0.0,
                        "g": 0.0,
                        "b": 0.0,
                        "a": 0.1,
                    },
                    "offset": {"x": 0, "y": 4},
                    "radius": 8,
                    "spread": 0,
                },
            ],
            "children": [
                {"type": "FRAME", "name": "Inner"},
            ],
        }
    )
    assert role is FrameRole.composed_region
