"""Phase-8 code-spec assembly tests — the render-ready ``PrismCodeSpec``.

Covers :func:`prism_mcp.figma.codespec.build_code_spec` and the
``response_detail="codespec"`` wire path:

* the **tag cascade** (`_element_for`): icon → catalog identity → high-conf
  pattern pick → page shell → layout primitive → fuzzy mapper → ``<div>``;
* **props / text** assembly (typed P3 props, P6 content binding as an
  attribute vs ``children`` text);
* **tokens** collection (P5 background / border / typography) + passthrough;
* the **containment re-parent** that folds the walker's flattened forest back
  into a single page tree;
* the **prune** that drops empty ``<div/>`` scaffolding and collapses
  single-child wrappers (P8's "zero extra divs" metric);
* **shell slot** assignment + **flexGrow** child marking;
* **import dedup** + the cycle / depth guards;
* the walker integration (``leanify_tree_mapping(..., "codespec")`` shape +
  a committed fixture round-trip).

Hand-built :class:`FigmaTreeMapping`\\s keep the cascade tests hermetic — no
walker, no tarball, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prism_mcp.figma import (
    FigmaTreeMapping,
    LayoutNode,
    MappedRegion,
    PrismLayout,
    PrismPageShell,
    RegionResolution,
    ResolvedProp,
    Typography,
    build_code_spec,
    leanify_tree_mapping,
    walk_tree,
)
from prism_mcp.figma.codespec import PRISM_MODULE
from prism_mcp.figma.content import build_icon_index
from prism_mcp.figma.models import BoxStyle, ContentBinding, PrismIcon
from prism_mcp.figma_mapping import FigmaNodeMapping

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"

# --------------------------------------------------------------------------
# Builders.
# --------------------------------------------------------------------------


def _mapping(
    name: str = "n",
    *,
    suggested: str | None = None,
    primary: str | None = None,
    primary_conf: float = 0.0,
) -> FigmaNodeMapping:
    return FigmaNodeMapping(
        node_name=name,
        suggested_component_name=suggested,
        primary_recommendation=primary,
        primary_recommendation_confidence=primary_conf,
    )


def _resolution(component: str, *, confidence: float = 1.0) -> RegionResolution:
    return RegionResolution(
        prism_component=component,
        method="family-name",
        confidence=confidence,
        source="catalog",
        component_key="k",
    )


def _prop(prop: str, value: str, value_kind: str = "expr") -> ResolvedProp:
    return ResolvedProp(
        prop=prop,
        value=value,
        value_kind=value_kind,  # type: ignore[arg-type]
        prop_kind="enum",
        source_axis="Type",
        figma_value=value,
        method="value-map",
        confidence=1.0,
    )


def _region(
    node_id: str,
    *,
    role: str = "component",
    bbox: tuple[float, float, float, float] = (0, 0, 10, 10),
    box_style: BoxStyle | None = None,
    resolution: RegionResolution | None = None,
    props: list[ResolvedProp] | None = None,
    typography: Typography | None = None,
    icon: PrismIcon | None = None,
    binding: ContentBinding | None = None,
    mapping: FigmaNodeMapping | None = None,
    name: str | None = None,
) -> MappedRegion:
    return MappedRegion(
        id=node_id,
        name=name or node_id,
        role=role,
        bbox=bbox,
        box_style=box_style or BoxStyle(),
        mapping=mapping or _mapping(name or node_id),
        prism_resolution=resolution,
        prism_props=props or [],
        typography=typography,
        prism_icon=icon,
        content_binding=binding,
    )


def _node(
    node_id: str,
    *,
    role: str = "layout-container",
    bbox: tuple[float, float, float, float] = (0, 0, 10, 10),
    children: list[str] | None = None,
    layout: PrismLayout | None = None,
    shell: PrismPageShell | None = None,
    name: str | None = None,
) -> LayoutNode:
    return LayoutNode(
        id=node_id,
        name=name or node_id,
        role=role,
        bbox=bbox,
        children_ids=children or [],
        prism_layout=layout,
        prism_shell=shell,
    )


def _tree(
    *,
    agenda: list[MappedRegion] | None = None,
    layout_tree: list[LayoutNode] | None = None,
    tokens: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> FigmaTreeMapping:
    return FigmaTreeMapping(
        agenda=agenda or [],
        layout_tree=layout_tree or [],
        tokens=tokens or {},
        warnings=warnings or [],
    )


def _flatten(spec: Any) -> list[Any]:
    out: list[Any] = []

    def _visit(node: Any) -> None:
        out.append(node)
        for child in node.children:
            _visit(child)

    for root in spec.roots:
        _visit(root)
    return out


def _by_id(spec: Any, node_id: str) -> Any:
    for node in _flatten(spec):
        if node.figma_id == node_id:
            return node
    raise AssertionError(f"{node_id} not in spec")


# --------------------------------------------------------------------------
# Tag cascade — _element_for via build_code_spec.
# --------------------------------------------------------------------------


def test_icon_region_resolves_to_icon_component() -> None:
    region = _region(
        "1:1",
        role="icon",
        icon=PrismIcon(
            figma_name="hamburger",
            prism_component="MenuIcon",
            method="synonym",
            confidence=0.9,
        ),
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    node = _by_id(spec, "1:1")
    assert node.tag == "MenuIcon"
    assert node.source == "icon"
    assert node.import_from == PRISM_MODULE


def test_catalog_identity_wins_over_layout_primitive() -> None:
    # A region that BOTH resolved to a catalog component AND has a layout
    # primitive on its node — the semantic component must win.
    region = _region("1:1", resolution=_resolution("Button"))
    node = _node(
        "1:1",
        layout=PrismLayout(component="FlexLayout", source="geometry"),
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[node]))
    assert _by_id(spec, "1:1").tag == "Button"
    assert _by_id(spec, "1:1").source == "catalog"


def test_high_conf_pattern_recommendation_used() -> None:
    region = _region(
        "1:1", mapping=_mapping(primary="Tile", primary_conf=1.0)
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    assert _by_id(spec, "1:1").tag == "Tile"
    assert _by_id(spec, "1:1").source == "pattern"


def test_low_conf_pattern_does_not_beat_layout() -> None:
    region = _region(
        "1:1", mapping=_mapping(primary="Tile", primary_conf=0.5)
    )
    node = _node(
        "1:1", layout=PrismLayout(component="StackingLayout", source="geometry")
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[node]))
    assert _by_id(spec, "1:1").tag == "StackingLayout"
    assert _by_id(spec, "1:1").source == "layout"


def test_layout_primitive_for_pure_container() -> None:
    # A pure container (in layout tree, NOT in agenda) renders as its layout
    # primitive with the token-snapped props carried through.
    node = _node(
        "1:1",
        layout=PrismLayout(
            component="FlexLayout",
            props={"flexDirection": "column", "itemGap": "M"},
            source="figma_auto_layout",
            confidence=1.0,
        ),
    )
    spec = build_code_spec(_tree(layout_tree=[node]))
    root = spec.roots[0]
    assert root.tag == "FlexLayout"
    assert root.source == "layout"
    props = {p.name: p.value for p in root.props}
    assert props == {"flexDirection": "column", "itemGap": "M"}


def test_fuzzy_mapper_suggestion_used_as_last_resort() -> None:
    region = _region("1:1", mapping=_mapping(suggested="Tooltip"))
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    assert _by_id(spec, "1:1").tag == "Tooltip"
    assert _by_id(spec, "1:1").source == "mapper"


def test_fallback_div_for_unresolved_region() -> None:
    region = _region("1:1")  # no resolution, no suggestion, no layout
    spec = build_code_spec(
        _tree(
            agenda=[region],
            # two children keep it from being pruned as bare scaffolding
            layout_tree=[
                _node("1:1", children=["2:1", "2:2"]),
                _node("2:1", layout=PrismLayout(component="FlexLayout", source="geometry")),
                _node("2:2", layout=PrismLayout(component="FlexLayout", source="geometry")),
            ],
        )
    )
    root = spec.roots[0]
    assert root.tag == "div"
    assert root.source == "fallback"
    assert root.import_from is None
    assert any("fallback" in n.lower() for n in root.notes)


# --------------------------------------------------------------------------
# Props / text / tokens.
# --------------------------------------------------------------------------


def test_typed_props_carried_with_value_kinds() -> None:
    region = _region(
        "1:1",
        resolution=_resolution("Button"),
        props=[
            _prop("type", "ButtonTypes.PRIMARY", "expr"),
            _prop("disabled", "true", "bool"),
        ],
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    props = {p.name: (p.value, p.value_kind) for p in _by_id(spec, "1:1").props}
    assert props["type"] == ("ButtonTypes.PRIMARY", "expr")
    assert props["disabled"] == ("true", "bool")


def test_content_binding_children_becomes_text() -> None:
    region = _region(
        "1:1",
        resolution=_resolution("Button"),
        binding=ContentBinding(
            prop="children",
            value="Save",
            value_kind="children",
            source="leaf",
        ),
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    node = _by_id(spec, "1:1")
    assert node.text == "Save"
    assert all(p.name != "children" for p in node.props)


def test_content_binding_named_prop_becomes_attribute() -> None:
    region = _region(
        "1:1",
        resolution=_resolution("Badge"),
        binding=ContentBinding(
            prop="label", value="New", value_kind="string", source="schema"
        ),
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    node = _by_id(spec, "1:1")
    assert node.text is None
    assert ("label", "New") in [(p.name, p.value) for p in node.props]


def test_tokens_collected_from_box_and_typography() -> None:
    region = _region(
        "1:1",
        resolution=_resolution("Card"),
        box_style=BoxStyle(
            background_token="background-base",
            border_token="border-subtle",
        ),
        typography=Typography(
            font_size=14.0,
            font_weight=600,
            style_token="paragraph",
            size_token="md",
            weight_token="semibold",
            confidence=1.0,
        ),
    )
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    assert set(_by_id(spec, "1:1").tokens) == {
        "background-base",
        "border-subtle",
        "paragraph",
    }


def test_tokens_map_passed_through() -> None:
    spec = build_code_spec(
        _tree(layout_tree=[_node("1:1")], tokens={"#FFFFFF": "white"})
    )
    assert spec.tokens == {"#FFFFFF": "white"}


def test_composite_family_emits_pull_example_note() -> None:
    region = _region("1:1", resolution=_resolution("Tables"))
    spec = build_code_spec(_tree(agenda=[region], layout_tree=[_node("1:1")]))
    assert any("map_figma_node" in n for n in _by_id(spec, "1:1").notes)


# --------------------------------------------------------------------------
# Children / shells / flexGrow.
# --------------------------------------------------------------------------


def test_children_nest_in_reading_order() -> None:
    parent = _node(
        "1:1",
        layout=PrismLayout(component="FlexLayout", source="geometry"),
        children=["2:1", "2:2"],
    )
    spec = build_code_spec(
        _tree(
            agenda=[
                _region("2:1", resolution=_resolution("Button")),
                _region("2:2", resolution=_resolution("Badge")),
            ],
            layout_tree=[parent, _node("2:1"), _node("2:2")],
        )
    )
    root = spec.roots[0]
    assert [c.tag for c in root.children] == ["Button", "Badge"]


def test_shell_slots_assigned_to_children() -> None:
    shell = _node(
        "1:1",
        bbox=(0, 0, 100, 100),
        children=["2:1", "2:2"],
        shell=PrismPageShell(
            component="HeaderFooterLayout",
            slots={"header": "2:1", "bodyContent": "2:2"},
            confidence=0.8,
        ),
    )
    spec = build_code_spec(
        _tree(
            agenda=[
                _region("2:1", resolution=_resolution("Navigation")),
                _region("2:2", resolution=_resolution("Table")),
            ],
            layout_tree=[
                shell,
                _node("2:1", bbox=(0, 0, 100, 10)),
                _node("2:2", bbox=(0, 10, 100, 90)),
            ],
        )
    )
    root = spec.roots[0]
    assert root.tag == "HeaderFooterLayout"
    slots = {c.figma_id: c.slot for c in root.children}
    assert slots == {"2:1": "header", "2:2": "bodyContent"}


def test_flex_grow_marks_fill_child() -> None:
    parent = _node(
        "1:1",
        layout=PrismLayout(
            component="FlexLayout",
            source="geometry",
            fill_child_ids=["2:1"],
        ),
        children=["2:1"],
    )
    spec = build_code_spec(
        _tree(
            agenda=[_region("2:1", resolution=_resolution("Table"))],
            layout_tree=[parent, _node("2:1")],
        )
    )
    assert _by_id(spec, "2:1").flex_grow is True


# --------------------------------------------------------------------------
# Prune — zero extra divs.
# --------------------------------------------------------------------------


def test_empty_leaf_fallback_dropped() -> None:
    # A FlexLayout with two children: a real Button and an empty fallback div.
    parent = _node(
        "1:1",
        layout=PrismLayout(component="FlexLayout", source="geometry"),
        children=["2:1", "2:2"],
    )
    spec = build_code_spec(
        _tree(
            agenda=[_region("2:1", resolution=_resolution("Button"))],
            # 2:2 has no region, no layout, no children -> bare empty div
            layout_tree=[parent, _node("2:1"), _node("2:2")],
        )
    )
    ids = [n.figma_id for n in _flatten(spec)]
    assert "2:2" not in ids
    assert "2:1" in ids


def test_single_child_wrapper_collapsed() -> None:
    # Outer bare div -> inner bare div -> Button collapses to just Button.
    spec = build_code_spec(
        _tree(
            agenda=[_region("3:1", resolution=_resolution("Button"))],
            layout_tree=[
                _node("1:1", children=["2:1"]),
                _node("2:1", children=["3:1"]),
                _node("3:1"),
            ],
        )
    )
    assert len(spec.roots) == 1
    assert spec.roots[0].tag == "Button"


def test_multichild_bare_fallback_is_kept() -> None:
    spec = build_code_spec(
        _tree(
            agenda=[
                _region("2:1", resolution=_resolution("Button")),
                _region("2:2", resolution=_resolution("Badge")),
            ],
            layout_tree=[
                _node("1:1", children=["2:1", "2:2"]),
                _node("2:1"),
                _node("2:2"),
            ],
        )
    )
    assert spec.roots[0].tag == "div"
    assert len(spec.roots[0].children) == 2


# --------------------------------------------------------------------------
# Containment re-parent.
# --------------------------------------------------------------------------


def test_containment_reparents_orphan_into_single_root() -> None:
    # The walker flattens: an outer container references nothing (its child
    # was a pure container that returned no id), leaving two flat roots that
    # are actually spatially nested. Containment should re-nest them. Both
    # carry a layout primitive so neither is pruned as bare scaffolding.
    outer = _node(
        "1:1",
        bbox=(0, 0, 100, 100),
        layout=PrismLayout(component="StackingLayout", source="geometry"),
    )
    inner = _node(
        "2:1",
        bbox=(10, 10, 50, 50),
        layout=PrismLayout(component="FlexLayout", source="geometry"),
    )
    spec = build_code_spec(_tree(layout_tree=[outer, inner]))
    assert len(spec.roots) == 1
    assert spec.roots[0].figma_id == "1:1"
    assert [c.figma_id for c in spec.roots[0].children] == ["2:1"]


def test_disjoint_nodes_stay_separate_roots() -> None:
    a = _node("1:1", bbox=(0, 0, 10, 10), layout=PrismLayout(component="FlexLayout", source="geometry"))
    b = _node("2:1", bbox=(100, 100, 10, 10), layout=PrismLayout(component="FlexLayout", source="geometry"))
    spec = build_code_spec(_tree(layout_tree=[a, b]))
    assert {r.figma_id for r in spec.roots} == {"1:1", "2:1"}


# --------------------------------------------------------------------------
# Imports / guards.
# --------------------------------------------------------------------------


def test_imports_deduped_and_sorted() -> None:
    parent = _node(
        "1:1",
        layout=PrismLayout(component="FlexLayout", source="geometry"),
        children=["2:1", "2:2", "2:3"],
    )
    spec = build_code_spec(
        _tree(
            agenda=[
                _region("2:1", resolution=_resolution("Button")),
                _region("2:2", resolution=_resolution("Button")),
                _region("2:3", resolution=_resolution("Badge")),
            ],
            layout_tree=[parent, _node("2:1"), _node("2:2"), _node("2:3")],
        )
    )
    names = [i.component for i in spec.imports]
    assert names == sorted(names)
    assert names.count("Button") == 1
    assert set(names) == {"Badge", "Button", "FlexLayout"}
    assert all(i.module == PRISM_MODULE for i in spec.imports)


def test_fallback_div_not_imported() -> None:
    spec = build_code_spec(
        _tree(
            agenda=[_region("2:1", resolution=_resolution("Button"))],
            layout_tree=[_node("1:1", children=["2:1", "2:2"]), _node("2:1"), _node("2:2")],
        )
    )
    assert all(i.component != "div" for i in spec.imports)


def test_cycle_in_children_does_not_recurse_forever() -> None:
    # Pathological back-edge: root 0:1 -> 1:1 -> 2:1 -> 1:1. The seen-set guard
    # must drop the back-edge so 1:1 is emitted exactly once (no hang).
    spec = build_code_spec(
        _tree(
            layout_tree=[
                _node("0:1", children=["1:1"], layout=PrismLayout(component="StackingLayout", source="geometry")),
                _node("1:1", children=["2:1"], layout=PrismLayout(component="FlexLayout", source="geometry")),
                _node("2:1", children=["1:1"], layout=PrismLayout(component="StackingLayout", source="geometry")),
            ],
        )
    )
    ids = [n.figma_id for n in _flatten(spec)]
    assert ids.count("1:1") == 1
    assert ids.count("2:1") == 1


def test_stats_counts() -> None:
    spec = build_code_spec(
        _tree(
            agenda=[_region("2:1", resolution=_resolution("Button"))],
            layout_tree=[
                _node("1:1", children=["2:1", "2:2"], layout=PrismLayout(component="FlexLayout", source="geometry")),
                _node("2:1"),
                _node("2:2", layout=PrismLayout(component="StackingLayout", source="geometry")),
            ],
        )
    )
    assert spec.stats["nodes"] == len(_flatten(spec))
    assert spec.stats["resolved"] >= 2
    assert spec.stats["roots"] == len(spec.roots)
    assert spec.stats["imports"] == len(spec.imports)


# --------------------------------------------------------------------------
# Wire path + walker integration.
# --------------------------------------------------------------------------


def test_leanify_codespec_returns_spec_shape() -> None:
    region = _region("1:1", resolution=_resolution("Button"))
    payload = leanify_tree_mapping(
        _tree(agenda=[region], layout_tree=[_node("1:1")]), "codespec"
    )
    assert set(payload) == {"roots", "imports", "tokens", "stats", "warnings"}
    assert payload["roots"][0]["tag"] == "Button"


def test_walker_fixture_round_trips_to_codespec() -> None:
    raw = json.loads(
        (FIXTURE_DIR / "figma-active-cluster-page.json").read_text(
            encoding="utf-8"
        )
    )
    node = next(iter(raw["nodes"].values())) if "nodes" in raw else raw
    document = node.get("document", node)
    icon_index = build_icon_index(["MenuIcon", "CloseIcon"], version="t")
    mapping = walk_tree(
        tree_json=document,
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        icon_index=icon_index,
        max_depth=100,
        max_nodes=500_000,
        max_agenda=100_000,
    )
    spec = build_code_spec(mapping)
    # Every agenda region surfaces as a node in the spec tree.
    spec_ids = {n.figma_id for n in _flatten(spec)}
    assert all(r.id in spec_ids for r in mapping.agenda)
    # The tree is render-ready: at least one root, deduped imports.
    assert spec.roots
    import_names = [i.component for i in spec.imports]
    assert import_names == sorted(set(import_names))
    assert spec.stats["nodes"] >= len(mapping.agenda)
