"""Phase-6 content resolution tests — icons + text-slot binding.

Covers the two deterministic resolvers added in P6 plus their walker wiring:

* :func:`prism_mcp.figma.content.resolve_icon` — the normalized-name cascade
  (exact → curated synonym → conservative fuzzy → unresolved) against an
  :class:`prism_mcp.figma.content.IconIndex` built from the Prism icon
  vocabulary.
* :func:`prism_mcp.figma.content.bind_text_content` — the text-bearing prop
  pick (named prop from the P3 schema, by priority) with a ``children``
  fallback for body-text leaf components and ``None`` for containers.
* the walker integration: per-region ``prism_icon`` / ``content_binding``,
  the ``content_resolved`` summary key (present only when an ``icon_index``
  is supplied AND something resolved), and the lean-response surfacing.

The walker assertions reuse the committed ``hamburger-icon.json`` fixture
(which collapses to a single ``role='icon'`` region named ``"Menu"``) and a
tiny fake mapper, so the tests are hermetic — no tarball, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prism_mcp.figma import (
    bind_text_content,
    build_icon_index,
    leanify_tree_mapping,
    resolve_icon,
    walk_tree,
)
from prism_mcp.figma.content import _normalize_icon
from prism_mcp.figma.models import ContentBinding, PrismIcon
from prism_mcp.figma.prop_schema import ComponentPropSchema, PropSchema
from prism_mcp.figma_mapping import FigmaNodeMapping

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"

# --------------------------------------------------------------------------
# Fixtures / builders.
# --------------------------------------------------------------------------

# A small but representative icon vocabulary. Each name ends in ``Icon`` so it
# survives :func:`build_icon_index`; the normalized keys are what queries hit.
_ICON_NAMES = [
    "ChevronDownIcon",
    "MenuIcon",
    "MagGlassIcon",
    "CloseIcon",
    "SettingsIcon",
    "EditIcon",
    "RemoveIcon",
    "PlusIcon",
    "AIIcon",
    "DashboardIcon",
]


def _icon_index() -> Any:
    return build_icon_index(_ICON_NAMES, version="t")


def _schema(component: str, props: dict[str, PropSchema]) -> ComponentPropSchema:
    return ComponentPropSchema(component=component, family=component, props=props)


def _string_prop(name: str) -> PropSchema:
    return PropSchema(name=name, kind="string")


# --------------------------------------------------------------------------
# _normalize_icon — the comparison key.
# --------------------------------------------------------------------------


def test_normalize_strips_path_affixes_and_punctuation() -> None:
    # Slash path, kebab, and the trailing/leading "icon" affix all collapse.
    assert _normalize_icon("icon/chevron-down") == "chevrondown"
    assert _normalize_icon("ChevronDownIcon") == "chevrondown"
    assert _normalize_icon("ic_chevron_down") == "chevrondown"


def test_normalize_is_idempotent_and_lowercases() -> None:
    once = _normalize_icon("Menu")
    assert once == "menu"
    assert _normalize_icon(once) == once


# --------------------------------------------------------------------------
# build_icon_index.
# --------------------------------------------------------------------------


def test_build_index_skips_non_icon_names() -> None:
    idx = build_icon_index(["MenuIcon", "Button", "Tile", "CloseIcon"])
    assert len(idx) == 2
    assert idx.by_norm["menu"] == "MenuIcon"
    assert idx.by_norm["close"] == "CloseIcon"


def test_build_index_first_writer_wins_on_collision() -> None:
    # Both normalize to "menu"; the first survives (deterministic for
    # sorted input).
    idx = build_icon_index(["MenuIcon", "MenuesIcon"])
    assert idx.by_norm["menu"] == "MenuIcon"


def test_build_index_empty() -> None:
    assert len(build_icon_index([])) == 0


# --------------------------------------------------------------------------
# resolve_icon — the cascade.
# --------------------------------------------------------------------------


def test_resolve_exact_normalized_match() -> None:
    icon = resolve_icon("icon/chevron-down", _icon_index())
    assert isinstance(icon, PrismIcon)
    assert icon.prism_component == "ChevronDownIcon"
    assert icon.method == "exact"
    assert icon.confidence == 1.0


def test_resolve_exact_from_figma_layer_name() -> None:
    icon = resolve_icon("Menu", _icon_index())
    assert icon is not None
    assert icon.prism_component == "MenuIcon"
    assert icon.method == "exact"


def test_resolve_synonym_maps_alias_to_prism_icon() -> None:
    for alias, expected in [
        ("search", "MagGlassIcon"),
        ("hamburger", "MenuIcon"),
        ("x", "CloseIcon"),
        ("gear", "SettingsIcon"),
        ("trash", "RemoveIcon"),
        ("add", "PlusIcon"),
    ]:
        icon = resolve_icon(alias, _icon_index())
        assert icon is not None, alias
        assert icon.prism_component == expected, alias
        assert icon.method == "synonym"
        assert icon.confidence == 0.9


def test_resolve_synonym_skipped_when_target_absent_from_index() -> None:
    # "search" → "magglass", but this index has no MagGlassIcon.
    idx = build_icon_index(["MenuIcon", "CloseIcon"])
    assert resolve_icon("search", idx) is None


def test_resolve_fuzzy_unique_contains_match() -> None:
    # "dashboards" contains "dashboard" — single hit → fuzzy.
    icon = resolve_icon("dashboards", _icon_index())
    assert icon is not None
    assert icon.prism_component == "DashboardIcon"
    assert icon.method == "fuzzy"
    assert icon.confidence == 0.6


def test_resolve_fuzzy_rejected_when_ambiguous() -> None:
    # Two icons share the "chevron" stem → ambiguous → no fuzzy match.
    idx = build_icon_index(["ChevronDownIcon", "ChevronUpIcon"])
    assert resolve_icon("chevron", idx) is None


def test_resolve_short_name_never_fuzzy_matches() -> None:
    # "ai" is below the fuzzy floor; it must hit exact/synonym or miss.
    idx = build_icon_index(["AIIcon", "DashboardIcon"])
    # "AIIcon" normalizes to "a" (the "icon" affix is peeled), so a bare
    # "ai" query doesn't even exact-match; the short-name guard blocks fuzzy.
    assert resolve_icon("airplane-mode", idx) is None


def test_resolve_generic_layer_names_never_resolve() -> None:
    # Structural / primitive Figma layer names must never fuzzy-match a glyph
    # (regression: "Group" → GroupByIcon, "Icon + Text" → BoldTextIcon).
    idx = build_icon_index(["GroupByIcon", "BoldTextIcon", "MenuIcon"])
    for generic in [
        "Group",
        "Vector 39",
        "Fill 3",
        "Icon + Text",
        "Frame 12",
        "Rectangle",
        "Mask",
    ]:
        assert resolve_icon(generic, idx) is None, generic


def test_resolve_miss_returns_none() -> None:
    assert resolve_icon("totally-unknown-glyph", _icon_index()) is None


def test_resolve_empty_inputs_return_none() -> None:
    assert resolve_icon("", _icon_index()) is None
    assert resolve_icon("Menu", build_icon_index([])) is None


# --------------------------------------------------------------------------
# bind_text_content — the prop pick.
# --------------------------------------------------------------------------


def test_bind_named_prop_from_schema_wins() -> None:
    schema = _schema("Input", {"label": _string_prop("label")})
    binding = bind_text_content("Input", "Name", schema)
    assert isinstance(binding, ContentBinding)
    assert binding.prop == "label"
    assert binding.value == "Name"
    assert binding.value_kind == "string"
    assert binding.source == "prop-schema"


def test_bind_respects_priority_order_title_before_label() -> None:
    schema = _schema(
        "Card",
        {"label": _string_prop("label"), "title": _string_prop("title")},
    )
    binding = bind_text_content("Card", "Overview", schema)
    assert binding is not None
    assert binding.prop == "title"


def test_bind_accepts_string_enum_union_prop() -> None:
    # A prop whose kind isn't string but accepts a raw string (``Enum |
    # string``) still qualifies as a text target.
    prop = PropSchema(name="placeholder", kind="other", accepts_string=True)
    schema = _schema("Select", {"placeholder": prop})
    binding = bind_text_content("Select", "Pick one", schema)
    assert binding is not None
    assert binding.prop == "placeholder"


def test_bind_ignores_non_text_props() -> None:
    # A boolean / number prop named off-priority is never a text sink; the
    # leaf fallback decides instead (Tile is a container → None).
    schema = _schema("Tile", {"disabled": PropSchema(name="disabled", kind="boolean")})
    assert bind_text_content("Tile", "label", schema) is None


def test_bind_children_fallback_for_leaf_without_schema() -> None:
    binding = bind_text_content("Button", "Save", None)
    assert binding is not None
    assert binding.prop == "children"
    assert binding.value_kind == "children"
    assert binding.source == "children-default"


def test_bind_container_without_named_prop_returns_none() -> None:
    # A container leaf (not in the body-text set) with no named text prop
    # gets no binding — its visible text belongs to an inner element.
    assert bind_text_content("Tile", "Group label", None) is None
    assert bind_text_content("Card", "Group label", None) is None


def test_bind_empty_text_returns_none() -> None:
    assert bind_text_content("Button", "   ", None) is None
    assert bind_text_content("Title", "", None) is None


# --------------------------------------------------------------------------
# Walker integration — icon path (hermetic via the hamburger fixture).
# --------------------------------------------------------------------------


def _hamburger_tree() -> dict[str, Any]:
    return json.loads(
        (FIXTURE_DIR / "hamburger-icon.json").read_text(encoding="utf-8")
    )


def test_walk_resolves_icon_region_with_index() -> None:
    mapping = walk_tree(
        tree_json=_hamburger_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        icon_index=_icon_index(),
    )
    assert len(mapping.agenda) == 1
    region = mapping.agenda[0]
    assert region.role == "icon"
    assert region.prism_icon is not None
    assert region.prism_icon.prism_component == "MenuIcon"
    assert region.prism_icon.method == "exact"
    assert mapping.summary.get("content_resolved") == 1


def test_walk_without_icon_index_is_a_noop() -> None:
    mapping = walk_tree(
        tree_json=_hamburger_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        icon_index=None,
    )
    region = mapping.agenda[0]
    assert region.prism_icon is None
    assert region.content_binding is None
    # The summary key is absent entirely on the no-P6 path (byte-identity
    # with the committed walker goldens).
    assert "content_resolved" not in mapping.summary


def test_lean_response_surfaces_resolved_icon() -> None:
    mapping = walk_tree(
        tree_json=_hamburger_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        icon_index=_icon_index(),
    )
    lean = leanify_tree_mapping(mapping, "lean")
    row = lean["agenda"][0]
    assert row.get("prism_icon") == "MenuIcon"


def test_lean_response_omits_icon_when_unresolved() -> None:
    mapping = walk_tree(
        tree_json=_hamburger_tree(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        icon_index=None,
    )
    lean = leanify_tree_mapping(mapping, "lean")
    for row in lean["agenda"]:
        assert "prism_icon" not in row


# --------------------------------------------------------------------------
# Walker integration — text-binding path (hermetic via a fake mapper).
# --------------------------------------------------------------------------


def _button_instance() -> dict[str, Any]:
    """A button-like INSTANCE with a single TEXT child.

    The walker emits one *simple* region (``role='instance'``) whose text is
    captured into ``content_slots["title"]`` — the canonical "this element
    renders one run of text" shape that text binding targets.
    """
    return {
        "id": "0:1",
        "type": "INSTANCE",
        "name": "PrimaryButton",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
        "fills": [
            {
                "type": "SOLID",
                "visible": True,
                "opacity": 1.0,
                "color": {"r": 0.106, "g": 0.42, "b": 0.8, "a": 1},
            }
        ],
        "children": [
            {
                "id": "1:2",
                "type": "TEXT",
                "name": "Label",
                "characters": "Save changes",
                "absoluteBoundingBox": {
                    "x": 10,
                    "y": 10,
                    "width": 100,
                    "height": 20,
                },
                "style": {"fontSize": 14, "fontWeight": 500},
            }
        ],
    }


def test_walk_binds_text_to_children_for_resolved_leaf() -> None:
    # A fake mapper routes the region to a body-text leaf (``Button``); with
    # prop resolution off the binding takes the deterministic children path.
    def fake_map(**kwargs: Any) -> FigmaNodeMapping:
        return FigmaNodeMapping(
            node_name=kwargs.get("node_name", ""),
            suggested_component_name="Button",
        )

    mapping = walk_tree(
        tree_json=_button_instance(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=fake_map,
        prop_resolution=False,
        icon_index=_icon_index(),
    )
    region = mapping.agenda[0]
    assert region.content_binding is not None
    assert region.content_binding.prop == "children"
    assert region.content_binding.value == "Save changes"
    assert mapping.summary.get("content_resolved") == 1

    lean = leanify_tree_mapping(mapping, "lean")
    row = lean["agenda"][0]
    assert row["content_binding"]["prop"] == "children"
    assert row["content_binding"]["value"] == "Save changes"


def test_walk_no_binding_for_unrouted_region() -> None:
    # No mapper → the region's stub mapping has no suggested component, so it
    # resolves to no component and text stays unbound (lean omits it).
    mapping = walk_tree(
        tree_json=_button_instance(),
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
        icon_index=_icon_index(),
    )
    region = mapping.agenda[0]
    assert region.content_binding is None
    lean = leanify_tree_mapping(mapping, "lean")
    assert "content_binding" not in lean["agenda"][0]
