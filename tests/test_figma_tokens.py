"""Phase-5 token & typography resolution tests.

Covers the three deterministic resolvers added in P5 plus their walker
wiring:

* :func:`prism_mcp.figma.tokens.resolve_color_token` — the trust cascade
  (designer ``variable_defs`` → perceptual :class:`ColorTokenIndex` →
  unresolved).
* :func:`prism_mcp.figma.tokens.resolve_typography` — the curated Prism
  type-ramp snap.
* :func:`prism_mcp.figma.utils.dominant_text_style` — the "largest TEXT
  in the subtree" picker that feeds typography.
* the walker integration: ``box_style.background_token`` / ``border_token``,
  the per-region ``typography`` field, the page ``tokens`` map enrichment,
  and the lean-response surfacing.

The walker assertions use a tiny in-memory :class:`ColorTokenIndex` and a
hand-built node tree so the test is hermetic (no tarball, no network) and
the perceptual buckets are predictable.
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma import leanify_tree_mapping, walk_tree
from prism_mcp.figma.models import Typography
from prism_mcp.figma.tokens import (
    resolve_color_token,
    resolve_typography,
)
from prism_mcp.figma.utils import dominant_text_style
from prism_mcp.tokens_index import ColorToken, ColorTokenIndex

# --------------------------------------------------------------------------
# Fixtures / builders.
# --------------------------------------------------------------------------


def _index() -> ColorTokenIndex:
    """A 4-token index with one role-keyworded surface token."""
    return ColorTokenIndex(
        [
            ColorToken(
                name="color-primary", hex="#1b6bcc", source_file="v.less"
            ),
            ColorToken(name="white-base", hex="#ffffff", source_file="v.less"),
            ColorToken(name="black-base", hex="#000000", source_file="v.less"),
            ColorToken(
                name="gray-surface", hex="#f4f6f8", source_file="v.less"
            ),
        ],
        version="t",
    )


def _solid(r: float, g: float, b: float) -> dict[str, Any]:
    return {
        "type": "SOLID",
        "visible": True,
        "opacity": 1.0,
        "color": {"r": r, "g": g, "b": b, "a": 1},
    }


def _card_tree() -> dict[str, Any]:
    """A surface-colored card FRAME with an h1 heading + body paragraph.

    The walker folds the two TEXT children into a single ``kpi-tile``
    region whose background is the surface gray and whose dominant text
    style is the 29px heading.
    """
    return {
        "id": "0:1",
        "type": "FRAME",
        "name": "Card",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 360, "height": 240},
        "fills": [_solid(0.957, 0.965, 0.973)],  # #F4F6F8 surface gray
        "layoutMode": "VERTICAL",
        "itemSpacing": 12,
        "paddingTop": 16,
        "paddingBottom": 16,
        "paddingLeft": 16,
        "paddingRight": 16,
        "children": [
            {
                "id": "1:2",
                "type": "TEXT",
                "name": "Heading",
                "characters": "Cluster Health",
                "absoluteBoundingBox": {
                    "x": 16,
                    "y": 16,
                    "width": 200,
                    "height": 36,
                },
                "style": {"fontSize": 29, "fontWeight": 300},
                "fills": [_solid(0.106, 0.42, 0.8)],  # #1B6BCC
            },
            {
                "id": "1:3",
                "type": "TEXT",
                "name": "Body",
                "characters": "All systems normal",
                "absoluteBoundingBox": {
                    "x": 16,
                    "y": 60,
                    "width": 200,
                    "height": 20,
                },
                "style": {"fontSize": 14, "fontWeight": 400},
                "fills": [_solid(0.13, 0.15, 0.18)],
            },
        ],
    }


# --------------------------------------------------------------------------
# resolve_color_token — the trust cascade.
# --------------------------------------------------------------------------


def test_color_designer_variable_wins_exact() -> None:
    res = resolve_color_token("#1B6BCC", {"#1B6BCC": "brand-primary"}, _index())
    assert res.token == "brand-primary"
    assert res.source == "figma_variable"
    assert res.bucket == "exact"


def test_color_variable_lookup_is_case_insensitive() -> None:
    # Designer map keyed lower, fill hex upper (the walker emits upper).
    res = resolve_color_token("#1B6BCC", {"#1b6bcc": "brand"}, None)
    assert res.token == "brand"
    # And the reverse: map keyed upper, query lower.
    res2 = resolve_color_token("#1b6bcc", {"#1B6BCC": "brand"}, None)
    assert res2.token == "brand"


def test_color_perceptual_exact_match() -> None:
    res = resolve_color_token("#1B6BCC", None, _index())
    assert res.token == "color-primary"
    assert res.bucket == "exact"
    assert res.source == "prism_token_index"


def test_color_perceptual_near_match() -> None:
    # A hair off #1b6bcc — inside the near band, outside exact.
    res = resolve_color_token("#1E6FD0", None, _index())
    assert res.token == "color-primary"
    assert res.bucket in ("exact", "near")
    assert res.source == "prism_token_index"


def test_color_far_color_is_unresolved_but_keeps_nearest() -> None:
    # Vivid magenta is far from every token in the index.
    res = resolve_color_token("#FF00AA", None, _index())
    assert res.token is None
    assert res.bucket in ("loose", "no-match")
    assert res.nearest is not None


def test_color_no_index_is_unresolved() -> None:
    res = resolve_color_token("#123456", None, None)
    assert res.token is None
    assert res.source == "none"


def test_color_empty_index_is_unresolved() -> None:
    res = resolve_color_token("#123456", None, ColorTokenIndex([], version="t"))
    assert res.token is None
    assert res.source == "none"


def test_color_malformed_hex_does_not_raise() -> None:
    # A gradient placeholder / junk value must never abort the walk.
    res = resolve_color_token("not-a-hex", None, _index())
    assert res.token is None


def test_color_role_surface_biases_to_surface_token() -> None:
    # A light gray near both white-base and gray-surface; the surface
    # role hint should pull it to the surface-named token.
    res = resolve_color_token("#F3F5F7", None, _index(), role="surface")
    assert res.token == "gray-surface"


# --------------------------------------------------------------------------
# resolve_typography — the curated type ramp.
# --------------------------------------------------------------------------


def test_typography_exact_h1() -> None:
    typo = resolve_typography({"fontSize": 29, "fontWeight": 300})
    assert isinstance(typo, Typography)
    assert typo.style_token == "title-h1"
    assert typo.size_token == "title-h1-font-size"
    assert typo.font_size == 29
    assert typo.confidence == 1.0


def test_typography_paragraph_wins_tie_over_label() -> None:
    # (14, 400) is shared by paragraph and label; ramp order favors
    # the more structural "paragraph".
    typo = resolve_typography({"fontSize": 14, "fontWeight": 400})
    assert typo is not None
    assert typo.style_token == "paragraph"


def test_typography_near_size_lowers_confidence() -> None:
    # 30px is 1px from the 29px h1 — adopted but at reduced confidence.
    typo = resolve_typography({"fontSize": 30, "fontWeight": 300})
    assert typo is not None
    assert typo.style_token == "title-h1"
    assert typo.confidence < 1.0


def test_typography_far_size_is_unresolved() -> None:
    assert resolve_typography({"fontSize": 200, "fontWeight": 400}) is None


def test_typography_missing_font_size_is_unresolved() -> None:
    assert resolve_typography({"fontWeight": 400}) is None


def test_typography_none_input_is_unresolved() -> None:
    assert resolve_typography(None) is None


def test_typography_weight_token_snaps_to_nearest() -> None:
    # 450 is closest to 400 ("regular").
    typo = resolve_typography({"fontSize": 14, "fontWeight": 450})
    assert typo is not None
    assert typo.weight_token == "regular"


def test_typography_missing_weight_uses_ramp_weight() -> None:
    typo = resolve_typography({"fontSize": 29})
    assert typo is not None
    assert typo.style_token == "title-h1"
    # Ramp weight for h1 is 300 -> "thin".
    assert typo.weight_token == "thin"


# --------------------------------------------------------------------------
# dominant_text_style — largest TEXT in the subtree.
# --------------------------------------------------------------------------


def test_dominant_text_style_picks_largest_in_subtree() -> None:
    node = {
        "type": "FRAME",
        "children": [
            {"type": "TEXT", "style": {"fontSize": 14}},
            {
                "type": "GROUP",
                "children": [
                    {"type": "TEXT", "style": {"fontSize": 29}},
                ],
            },
        ],
    }
    style = dominant_text_style(node)
    assert style is not None
    assert style["fontSize"] == 29


def test_dominant_text_style_none_when_no_text() -> None:
    node = {"type": "FRAME", "children": [{"type": "RECTANGLE"}]}
    assert dominant_text_style(node) is None


def test_dominant_text_style_ignores_text_without_style() -> None:
    node = {"type": "TEXT"}  # no style block
    assert dominant_text_style(node) is None


# --------------------------------------------------------------------------
# Walker integration.
# --------------------------------------------------------------------------


def test_walk_resolves_background_token_and_typography_with_index() -> None:
    mapping = walk_tree(
        tree_json=_card_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        color_token_index=_index(),
    )
    assert len(mapping.agenda) == 1
    region = mapping.agenda[0]
    assert region.box_style.background_color == "#F4F6F8"
    assert region.box_style.background_token == "gray-surface"
    assert region.typography is not None
    assert region.typography.style_token == "title-h1"
    # The page tokens map carries the resolved token as the value.
    assert mapping.tokens.get("#F4F6F8") == "gray-surface"


def test_walk_without_index_keeps_typography_but_no_color_token() -> None:
    mapping = walk_tree(
        tree_json=_card_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        color_token_index=None,
    )
    region = mapping.agenda[0]
    # Typography resolution is index-independent.
    assert region.typography is not None
    assert region.typography.style_token == "title-h1"
    # Without a perceptual index and no designer variable, the color is
    # left as a literal (token unset, tokens-map value empty).
    assert region.box_style.background_token is None
    assert mapping.tokens.get("#F4F6F8") == ""


def test_walk_designer_variable_flows_to_tokens_map_without_index() -> None:
    mapping = walk_tree(
        tree_json=_card_tree(),
        reference_jsx=None,
        variable_defs={"#F4F6F8": "color-app-surface"},
        map_figma_node_fn=None,
        color_token_index=None,
    )
    region = mapping.agenda[0]
    assert region.box_style.background_token == "color-app-surface"
    assert mapping.tokens.get("#F4F6F8") == "color-app-surface"


def test_lean_response_surfaces_typography_triple() -> None:
    mapping = walk_tree(
        tree_json=_card_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        color_token_index=_index(),
    )
    lean = leanify_tree_mapping(mapping, "lean")
    row = lean["agenda"][0]
    assert "typography" in row
    assert row["typography"] == {
        "style_token": "title-h1",
        "size_token": "title-h1-font-size",
        "weight_token": "thin",
    }


def test_lean_response_omits_typography_when_unresolved() -> None:
    # A frame with no styled text -> no typography key on the lean row.
    tree = {
        "id": "0:1",
        "type": "FRAME",
        "name": "Bare",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 80},
        "fills": [_solid(1.0, 1.0, 1.0)],
        "children": [
            {
                "id": "1:2",
                "type": "RECTANGLE",
                "name": "Box",
                "absoluteBoundingBox": {
                    "x": 10,
                    "y": 10,
                    "width": 40,
                    "height": 40,
                },
                "fills": [_solid(0.106, 0.42, 0.8)],
            }
        ],
    }
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        color_token_index=_index(),
    )
    lean = leanify_tree_mapping(mapping, "lean")
    for row in lean["agenda"]:
        assert "typography" not in row
