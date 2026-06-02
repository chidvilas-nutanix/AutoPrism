"""Unit tests for the box-style + visual-presence helpers.

Covers:

* :func:`prism_mcp.figma.utils.has_visual_presence` — the boolean
  promotion gate used by :mod:`prism_mcp.figma.routing` to classify
  visual containers.
* :func:`prism_mcp.figma.utils.infer_padding` — the auto-layout
  fast path AND the absolute-positioned bbox-difference fallback.
* :func:`prism_mcp.figma.utils.extract_box_style` — the
  CSS-aligned style dict the walker hands to
  :class:`prism_mcp.figma.models.BoxStyle`.

See design doc §4.4.1 (visual-container promotion) and
:mod:`prism_mcp.figma.utils` for full reasoning.
"""

from __future__ import annotations

from prism_mcp.figma.utils import (
    extract_box_style,
    has_visual_presence,
    infer_padding,
    shape_bucket,
)

# --------------------------------------------------------------------------
# Helper builders to keep the inline JSON readable.
# --------------------------------------------------------------------------


def _solid_fill(hex_rgb: tuple[int, int, int]) -> dict[str, object]:
    """Build a SOLID fill dict from an 8-bit ``(r, g, b)`` tuple."""
    r, g, b = hex_rgb
    return {
        "type": "SOLID",
        "color": {
            "r": r / 255,
            "g": g / 255,
            "b": b / 255,
            "a": 1.0,
        },
    }


def _bbox(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


# --------------------------------------------------------------------------
# has_visual_presence — the gate used by classify_frame_role.
# --------------------------------------------------------------------------


def test_has_visual_presence_visible_fill() -> None:
    assert has_visual_presence(
        {
            "type": "FRAME",
            "fills": [_solid_fill((237, 240, 242))],
        }
    )


def test_has_visual_presence_invisible_fill_returns_false() -> None:
    """A fill with ``visible: false`` is just a painted slot the
    designer turned off — the FRAME paints nothing."""
    invisible = _solid_fill((255, 255, 255))
    invisible["visible"] = False
    assert not has_visual_presence(
        {
            "type": "FRAME",
            "fills": [invisible],
        }
    )


def test_has_visual_presence_corner_radius_alone() -> None:
    """A FRAME with no paint but rounded corners is still a visual
    container (designers use these as clipping masks)."""
    assert has_visual_presence({"type": "FRAME", "cornerRadius": 4})


def test_has_visual_presence_zero_corner_radius_does_not_count() -> None:
    assert not has_visual_presence({"type": "FRAME", "cornerRadius": 0})


def test_has_visual_presence_rectangle_corner_radii_alone() -> None:
    """Mixed corners count too — designers use them for tabs / pills."""
    assert has_visual_presence(
        {
            "type": "FRAME",
            "rectangleCornerRadii": [4, 4, 0, 0],
        }
    )


def test_has_visual_presence_visible_drop_shadow() -> None:
    assert has_visual_presence(
        {
            "type": "FRAME",
            "effects": [
                {
                    "type": "DROP_SHADOW",
                    "color": {
                        "r": 0,
                        "g": 0,
                        "b": 0,
                        "a": 0.1,
                    },
                    "offset": {"x": 0, "y": 2},
                    "radius": 4,
                    "spread": 0,
                },
            ],
        }
    )


def test_has_visual_presence_invisible_shadow_returns_false() -> None:
    assert not has_visual_presence(
        {
            "type": "FRAME",
            "effects": [
                {
                    "type": "DROP_SHADOW",
                    "visible": False,
                    "color": {
                        "r": 0,
                        "g": 0,
                        "b": 0,
                        "a": 0.1,
                    },
                    "offset": {"x": 0, "y": 2},
                    "radius": 4,
                    "spread": 0,
                },
            ],
        }
    )


def test_has_visual_presence_bare_frame_returns_false() -> None:
    assert not has_visual_presence({"type": "FRAME", "name": "Empty"})


# --------------------------------------------------------------------------
# infer_padding — auto-layout fast path.
# --------------------------------------------------------------------------


def test_infer_padding_auto_layout_horizontal() -> None:
    """``layoutMode == "HORIZONTAL"`` -> trust Figma's own paddings."""
    node = {
        "type": "FRAME",
        "layoutMode": "HORIZONTAL",
        "paddingTop": 12,
        "paddingRight": 16,
        "paddingBottom": 12,
        "paddingLeft": 16,
    }
    assert infer_padding(node) == (12.0, 16.0, 12.0, 16.0)


def test_infer_padding_auto_layout_vertical_zero_returns_none() -> None:
    """Auto-layout with all-zero paddings should return ``None`` so
    the field stays out of the agenda."""
    node = {
        "type": "FRAME",
        "layoutMode": "VERTICAL",
        "paddingTop": 0,
        "paddingRight": 0,
        "paddingBottom": 0,
        "paddingLeft": 0,
    }
    assert infer_padding(node) is None


# --------------------------------------------------------------------------
# infer_padding — absolute-positioning bbox-diff fallback.
# --------------------------------------------------------------------------


def test_infer_padding_absolute_evenly_padded() -> None:
    """The Status/Alert Banner regression case — parent + child
    bboxes with 15px top/bottom and 20px left/right offsets."""
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(-230, 3.5, 460, 138),
    }
    child = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(-210, 18.5, 420, 108),
    }
    # padding = (top=15, right=20, bottom=15, left=20)
    assert infer_padding(parent, children=[child]) == (
        15.0,
        20.0,
        15.0,
        20.0,
    )


def test_infer_padding_absolute_asymmetric() -> None:
    """A child offset from one side but flush with the others should
    surface that asymmetry in the (T, R, B, L) tuple."""
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    child = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(10, 0, 90, 100),
    }
    # padding = (top=0, right=0, bottom=0, left=10)
    assert infer_padding(parent, children=[child]) == (0.0, 0.0, 0.0, 10.0)


def test_infer_padding_absolute_uses_min_top_max_bottom() -> None:
    """Multiple children: padding is the tightest fit on each side."""
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    children = [
        {
            "type": "FRAME",
            "absoluteBoundingBox": _bbox(10, 5, 30, 20),
        },
        {
            "type": "FRAME",
            "absoluteBoundingBox": _bbox(20, 50, 70, 40),  # right=90, bottom=90
        },
    ]
    # padding = (top=5, right=10, bottom=10, left=10)
    assert infer_padding(parent, children=children) == (
        5.0,
        10.0,
        10.0,
        10.0,
    )


def test_infer_padding_overflowing_child_clamps_to_zero() -> None:
    """Children that extend past the parent bbox are allowed in
    Figma — the inference clamps negative results to 0 rather than
    surfacing a phantom "negative padding"."""
    parent = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(0, 0, 100, 100),
    }
    child = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(-10, -5, 120, 110),
    }
    assert infer_padding(parent, children=[child]) is None


def test_infer_padding_no_children_returns_none() -> None:
    assert (
        infer_padding(
            {
                "type": "FRAME",
                "absoluteBoundingBox": _bbox(0, 0, 100, 100),
            }
        )
        is None
    )


# --------------------------------------------------------------------------
# extract_box_style — the full integration helper.
# --------------------------------------------------------------------------


def test_extract_box_style_full_visual_container() -> None:
    """The Status/Alert Banner case end-to-end: grey fill, 2px
    corner, inferred 15/20 padding from one child bbox."""
    parent = {
        "type": "FRAME",
        "fills": [_solid_fill((237, 240, 242))],
        "cornerRadius": 2,
        "absoluteBoundingBox": _bbox(-230, 3.5, 460, 138),
    }
    child = {
        "type": "FRAME",
        "absoluteBoundingBox": _bbox(-210, 18.5, 420, 108),
    }
    style = extract_box_style(parent, children=[child])
    assert style == {
        "background_color": "#EDF0F2",
        "corner_radius": 2.0,
        "padding": (15.0, 20.0, 15.0, 20.0),
    }


def test_extract_box_style_auto_layout_with_gap() -> None:
    """Auto-layout FRAMEs expose ``layout_mode`` and ``gap`` too."""
    node = {
        "type": "FRAME",
        "layoutMode": "HORIZONTAL",
        "itemSpacing": 8,
        "paddingTop": 4,
        "paddingRight": 4,
        "paddingBottom": 4,
        "paddingLeft": 4,
    }
    style = extract_box_style(node)
    assert style == {
        "layout_mode": "HORIZONTAL",
        "gap": 8.0,
        "padding": (4.0, 4.0, 4.0, 4.0),
    }


def test_extract_box_style_border_only() -> None:
    """A bordered FRAME with no fill / corner — only border_* keys."""
    style = extract_box_style(
        {
            "type": "FRAME",
            "strokes": [_solid_fill((40, 40, 40))],
            "strokeWeight": 1,
        }
    )
    assert style == {
        "border_color": "#282828",
        "border_width": 1.0,
    }


def test_extract_box_style_bare_frame_is_empty() -> None:
    """A FRAME with no visible paint, no border, no corner radius,
    no shadow, no auto-layout, no padding, no transparency yields
    an empty dict — keeps the agenda compact."""
    assert (
        extract_box_style({"type": "FRAME", "name": "Spacer"}) == {}
    )


def test_extract_box_style_mixed_rectangle_corners() -> None:
    """Different per-corner radii surface as a 4-list."""
    style = extract_box_style(
        {
            "type": "FRAME",
            "rectangleCornerRadii": [4, 4, 0, 0],
        }
    )
    assert style == {"corner_radius": [4.0, 4.0, 0.0, 0.0]}


def test_extract_box_style_opacity_below_one() -> None:
    """Node-level opacity < 1.0 (but not invisible) should surface."""
    style = extract_box_style(
        {
            "type": "FRAME",
            "fills": [_solid_fill((255, 255, 255))],
            "opacity": 0.6,
        }
    )
    assert style.get("opacity") == 0.6
    assert style.get("background_color") == "#FFFFFF"


def test_extract_box_style_opacity_one_is_omitted() -> None:
    """Default 1.0 opacity is the common case and stays out of the
    output — the agenda doesn't need to mention 'opacity: 1.0'."""
    style = extract_box_style(
        {
            "type": "FRAME",
            "fills": [_solid_fill((255, 255, 255))],
            "opacity": 1.0,
        }
    )
    assert "opacity" not in style


# --------------------------------------------------------------------------
# shape_bucket — boundary coverage.
#
# The plan called these out explicitly: a regression that quietly
# shifted any threshold would break the +0.05 shape-bucket bonus on
# the ranker. Every bucket gets one positive test at its boundary
# and one negative test on the wrong side.
# --------------------------------------------------------------------------


def test_shape_bucket_none_returns_empty_string() -> None:
    """``None`` bbox -> ``""`` so the ranker treats it as no signal
    instead of raising."""
    assert shape_bucket(None) == ""


def test_shape_bucket_zero_dimensions_returns_empty_string() -> None:
    """Zero-area bboxes don't classify — same rationale as ``None``."""
    assert shape_bucket((0.0, 0.0, 0.0, 50.0)) == ""
    assert shape_bucket((0.0, 0.0, 50.0, 0.0)) == ""


def test_shape_bucket_icon_at_area_threshold() -> None:
    """``area < 1024`` -> ``"icon"``. Exact area 1024 is NOT an icon
    (the threshold is strict less-than).
    """
    assert shape_bucket((0, 0, 31, 31)) == "icon"  # area = 961
    assert shape_bucket((0, 0, 32, 32)) != "icon"  # area = 1024


def test_shape_bucket_page_requires_both_dimensions() -> None:
    """Page = w >= 1000 AND h >= 600 — both gates."""
    assert shape_bucket((0, 0, 1280, 800)) == "page"
    assert shape_bucket((0, 0, 1280, 599)) != "page"
    assert shape_bucket((0, 0, 999, 800)) != "page"


def test_shape_bucket_banner_wide_short() -> None:
    """Banner = w >= 600 AND h <= 100, but not page-scale."""
    assert shape_bucket((0, 0, 800, 80)) == "banner"
    assert shape_bucket((0, 0, 800, 120)) != "banner"  # too tall


def test_shape_bucket_sidebar_tall_narrow() -> None:
    """Sidebar = h >= 400 AND w <= 300."""
    assert shape_bucket((0, 0, 240, 600)) == "sidebar"
    assert shape_bucket((0, 0, 320, 600)) != "sidebar"  # too wide
    assert shape_bucket((0, 0, 240, 399)) != "sidebar"  # too short


def test_shape_bucket_modal_balanced_geometry() -> None:
    """Modal = w >= 400 AND h >= 300 AND 0.5 <= aspect <= 2.0."""
    assert shape_bucket((0, 0, 500, 400)) == "modal"
    assert shape_bucket((0, 0, 500, 200)) != "modal"  # too short
    assert shape_bucket((0, 0, 399, 400)) != "modal"  # too narrow


def test_shape_bucket_tile_squarish_small() -> None:
    """Tile = 0.7 <= aspect <= 1.4 AND 50 <= w <= 400."""
    assert shape_bucket((0, 0, 200, 200)) == "tile"
    # aspect 200/280 ≈ 0.71 still satisfies the 0.7 floor
    assert shape_bucket((0, 0, 200, 280)) == "tile"
    # aspect 200/290 ≈ 0.69 drops below the floor -> not a tile
    assert shape_bucket((0, 0, 200, 290)) != "tile"
    # w < 50 cannot be a tile; tiny squares are icons by area
    assert shape_bucket((0, 0, 31, 31)) == "icon"  # area 961 -> icon
    assert shape_bucket((0, 0, 500, 500)) != "tile"  # too wide


def test_shape_bucket_card_wider_than_tall() -> None:
    """Card = aspect > 1.4 AND area < 200_000."""
    assert shape_bucket((0, 0, 300, 100)) == "card"
    assert shape_bucket((0, 0, 700, 400)) != "card"  # area too large
