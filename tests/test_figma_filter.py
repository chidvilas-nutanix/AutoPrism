"""Tests for the noise-filter passes (Phase 2).

Each pass is exercised with a positive case (node survives) and a
negative case (node drops) using inline dicts. The integration tests
that wire all passes through :func:`walk_tree` live in
``test_figma_walker_filter.py``.

See ``docs/figma-page-to-prism-plan.md`` §4.3.
"""

from __future__ import annotations

from prism_mcp.figma.filter import (
    DropReason,
    pass_1_visible,
    pass_2_invisible_decoration,
    pass_3_mappable_type,
    pass_4_collapse_passthrough,
    pass_6_tiny_decorative,
)

# --------------------------------------------------------------------------
# DropReason: stable string-valued enum used by the audit trail.
# --------------------------------------------------------------------------


def test_drop_reasons_have_stable_string_values() -> None:
    """The Cursor skill keys off these strings — renaming any of
    them is a breaking change to the MCP contract."""
    expected = {
        "explicit_hidden",
        "invisible_decoration",
        "non_design_type",
        "same_bbox_passthrough_collapsed",
        "icon_internal",
        "redundant_inner_instance",
        "tiny_decorative",
        "captured_as_content_slot",
        "folded_into_pattern",
        "unknown_type_fallback",
        # Added when the walker grew the universal pattern absorb-
        # ratio safety rail (see walker.py — rejects any pattern
        # match that would swallow > 50% of the input tree). The
        # skill's audit step keys off this string; if you rename it
        # update the skill in lockstep.
        "pattern_oversized_reject",
        # Fix C — added when ``max_agenda`` became a hard cap; the
        # post-DFS importance-ranking pass uses this reason for
        # regions it truncates. See
        # ``docs/x-ray-walker-investigation.md`` §8 + §12 "Fix C".
        "agenda_truncated",
        # Fix D — added for documentation-style variant stacks
        # (e.g. ``Modal/Empty`` next to ``Modal/Filled`` next to
        # ``Modal/Error``). The walker keeps one variant and drops
        # the rest with this reason. See
        # ``docs/x-ray-walker-investigation.md`` §11.5 + §12 "Fix D".
        "variant_alternative",
    }
    actual = {str(reason) for reason in DropReason}
    assert actual == expected


# --------------------------------------------------------------------------
# Pass 1 — visible flag.
# --------------------------------------------------------------------------


def test_pass_1_visible_default_is_visible() -> None:
    """Missing ``visible`` means visible (Figma default)."""
    assert pass_1_visible({"type": "FRAME", "name": "x"}) is True


def test_pass_1_visible_true_passes() -> None:
    assert pass_1_visible({"visible": True, "type": "FRAME"}) is True


def test_pass_1_visible_false_drops() -> None:
    assert pass_1_visible({"visible": False, "type": "FRAME"}) is False


# --------------------------------------------------------------------------
# Pass 2 — invisible decoration.
# --------------------------------------------------------------------------


def test_pass_2_container_with_children_always_passes() -> None:
    """Containers aggregate visibility; even with no fills they pass."""
    node = {
        "type": "FRAME",
        "children": [{"type": "TEXT", "characters": "x"}],
        "fills": [],
    }
    assert pass_2_invisible_decoration(node) is True


def test_pass_2_text_with_characters_passes_without_fill() -> None:
    """TEXT nodes are never decoration when they have characters."""
    node = {"type": "TEXT", "characters": "Hello", "fills": []}
    assert pass_2_invisible_decoration(node) is True


def test_pass_2_rect_no_fill_drops() -> None:
    """A leaf with no fill / stroke is pure decoration noise."""
    node = {"type": "RECTANGLE", "fills": []}
    assert pass_2_invisible_decoration(node) is False


def test_pass_2_rect_with_invisible_fill_drops() -> None:
    """Opacity=0.0001 is the spacer-rectangle pattern."""
    node = {
        "type": "RECTANGLE",
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 1, "g": 0, "b": 0},
                "opacity": 0.0001,
            }
        ],
    }
    assert pass_2_invisible_decoration(node) is False


def test_pass_2_rect_with_visible_fill_passes() -> None:
    node = {
        "type": "RECTANGLE",
        "fills": [
            {
                "type": "SOLID",
                "color": {"r": 0.5, "g": 0.5, "b": 0.5},
                "opacity": 1.0,
            }
        ],
    }
    assert pass_2_invisible_decoration(node) is True


def test_pass_2_rect_visible_stroke_passes() -> None:
    """A divider is often just a stroke, no fill."""
    node = {
        "type": "LINE",
        "fills": [],
        "strokes": [{"type": "SOLID", "color": {"r": 0.5, "g": 0.5, "b": 0.5}}],
    }
    assert pass_2_invisible_decoration(node) is True


# --------------------------------------------------------------------------
# Pass 3 — mappable type.
# --------------------------------------------------------------------------


def test_pass_3_design_type_passes() -> None:
    assert pass_3_mappable_type({"type": "FRAME"}) is True
    assert pass_3_mappable_type({"type": "INSTANCE"}) is True
    assert pass_3_mappable_type({"type": "RECTANGLE"}) is True


def test_pass_3_slice_drops() -> None:
    assert pass_3_mappable_type({"type": "SLICE"}) is False


def test_pass_3_figjam_type_drops() -> None:
    assert pass_3_mappable_type({"type": "STICKY"}) is False
    assert pass_3_mappable_type({"type": "CONNECTOR"}) is False


def test_pass_3_slides_type_drops() -> None:
    assert pass_3_mappable_type({"type": "SLIDE"}) is False


def test_pass_3_unknown_type_still_passes() -> None:
    """Unknown types must fall through pass 3 — they're handled by
    the routing layer's unknown_type_fallback path."""
    assert pass_3_mappable_type({"type": "FUTURE_SHAPE"}) is True


# --------------------------------------------------------------------------
# Pass 4 — passthrough collapse.
# --------------------------------------------------------------------------


def _bbox(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def test_pass_4_single_child_same_bbox_collapses() -> None:
    """The canonical pattern from §8.1: GROUP wrapping a single
    INSTANCE that fills it."""
    parent = {
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(940, 521, 320, 309),
        "children": [],
    }
    child = {
        "type": "INSTANCE",
        "absoluteBoundingBox": _bbox(940, 521, 320, 309),
    }
    assert pass_4_collapse_passthrough(parent, [child]) is child


def test_pass_4_two_children_does_not_collapse() -> None:
    parent = {
        "type": "GROUP",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    a = {"type": "RECTANGLE", "absoluteBoundingBox": _bbox(0, 0, 100, 100)}
    b = {"type": "RECTANGLE", "absoluteBoundingBox": _bbox(0, 0, 50, 50)}
    assert pass_4_collapse_passthrough(parent, [a, b]) is None


def test_pass_4_different_bbox_does_not_collapse() -> None:
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    child = {
        "type": "RECTANGLE",
        "absoluteBoundingBox": _bbox(0, 0, 50, 50),
    }
    assert pass_4_collapse_passthrough(parent, [child]) is None


def test_pass_4_subpixel_diff_within_tolerance_collapses() -> None:
    """Half-pixel sub-pixel diffs are within tol=0.5."""
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0.0, 0.0, 100.0, 100.0),
    }
    child = {
        "type": "INSTANCE",
        "absoluteBoundingBox": _bbox(0.3, 0.0, 99.8, 100.2),
    }
    assert pass_4_collapse_passthrough(parent, [child]) is child


def test_pass_4_instance_parent_does_not_collapse() -> None:
    """INSTANCE / COMPONENT parents carry semantic meaning we
    don't want to discard, even with a same-bbox single child."""
    parent = {
        "type": "INSTANCE",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    child = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    assert pass_4_collapse_passthrough(parent, [child]) is None


# --------------------------------------------------------------------------
# Pass 6 — tiny decorative.
# --------------------------------------------------------------------------


def test_pass_6_large_node_passes() -> None:
    """100x100 is well above the 50px^2 floor."""
    node = {
        "type": "RECTANGLE",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    assert pass_6_tiny_decorative(node) is True


def test_pass_6_tiny_no_text_no_children_drops() -> None:
    """A 4x4 rectangle with no children is decoration noise."""
    node = {
        "type": "RECTANGLE",
        "absoluteBoundingBox": _bbox(0, 0, 4, 4),
    }
    assert pass_6_tiny_decorative(node) is False


def test_pass_6_tiny_text_still_passes() -> None:
    """Text is meaningful even at small sizes."""
    node = {
        "type": "TEXT",
        "characters": "x",
        "absoluteBoundingBox": _bbox(0, 0, 5, 5),
    }
    assert pass_6_tiny_decorative(node) is True


def test_pass_6_tiny_container_with_children_passes() -> None:
    """A small container is still a container."""
    node = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 4, 4),
        "children": [{"type": "TEXT", "characters": "x"}],
    }
    assert pass_6_tiny_decorative(node) is True


def test_pass_6_empty_container_type_passes_without_children() -> None:
    """Containers without descendants are still exempt — pass 2
    handles their actual deletion if they're decoration-empty."""
    node = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 4, 4),
    }
    assert pass_6_tiny_decorative(node) is True
