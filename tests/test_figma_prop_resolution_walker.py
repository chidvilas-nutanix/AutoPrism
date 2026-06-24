"""P3 Part B: prop resolution wired into the walker.

Covers :func:`prism_mcp.figma.walker._resolve_region_props` and
:func:`_stash_component_properties`: the pass that turns a *routed*
region's Figma ``componentProperties`` into typed
:attr:`MappedRegion.prism_props`, plus its compact surfacing in the
lean response and the ``prop_resolved`` summary counter.

Both a hand-built :class:`FigmaCatalog` and
:class:`PropSchemaIndex` are injected for hermeticity — these tests do
not touch the committed artifacts.
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma import walk_tree
from prism_mcp.figma.catalog import CatalogEntry, FigmaCatalog
from prism_mcp.figma.models import FigmaTreeMapping, leanify_tree_mapping
from prism_mcp.figma.prop_schema import (
    PROP_SCHEMA_VERSION,
    ComponentPropSchema,
    PropSchema,
    PropSchemaIndex,
)

# --------------------------------------------------------------------------
# Harness.
# --------------------------------------------------------------------------


def _catalog(*entries: CatalogEntry) -> FigmaCatalog:
    return FigmaCatalog({e.component_key: e for e in entries}, {})


def _entry(key: str, prism: str) -> CatalogEntry:
    return CatalogEntry(
        component_key=key,
        prism_component=prism,
        kind="component",
        method="key-override",  # type: ignore[arg-type]
        confidence=1.0,
        figma_name="test",
        library_key="LIB",
        library_name="Test Library",
    )


_BUTTON_SCHEMA = ComponentPropSchema(
    component="Button",
    family="Button",
    props={
        "type": PropSchema(
            name="type",
            kind="enum",
            enum_name="ButtonTypes",
            enum_members={"PRIMARY": "primary", "SECONDARY": "secondary"},
            values=["primary", "secondary"],
        ),
        "disabled": PropSchema(name="disabled", kind="boolean"),
        "children": PropSchema(name="children", kind="node"),
    },
)

_TABLE_SCHEMA = ComponentPropSchema(component="Table", family="Tables")
_TABLECELL_SCHEMA = ComponentPropSchema(
    component="TableCell",
    family="Tables",
    props={
        "align": PropSchema(
            name="align", kind="union", values=["left", "right", "center"]
        )
    },
)


def _prop_index(
    *schemas: ComponentPropSchema,
    families: dict[str, dict[str, Any]] | None = None,
) -> PropSchemaIndex:
    components = {s.component: s for s in schemas}
    return PropSchemaIndex(
        components,
        families or {},
        {"schema_version": PROP_SCHEMA_VERSION, "rplib_version": "test"},
    )


def _page_with_child(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "1:1",
        "name": "Page",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 300},
        "children": [child],
    }


def _instance(
    *,
    node_id: str = "1:2",
    name: str = "Button",
    component_id: str = "10:1",
    component_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "name": name,
        "type": "INSTANCE",
        "componentId": component_id,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }
    if component_properties is not None:
        node["componentProperties"] = component_properties
    return node


def _walk(
    document: dict[str, Any],
    *,
    components: dict[str, Any] | None = None,
    catalog: FigmaCatalog | None = None,
    prop_schema: PropSchemaIndex | None = None,
    prop_resolution: bool = True,
) -> FigmaTreeMapping:
    return walk_tree(
        tree_json=document,
        components=components,
        map_figma_node_fn=None,
        catalog=catalog,
        prop_schema=prop_schema,
        prop_resolution=prop_resolution,
    )


def _routed(mapping: FigmaTreeMapping):
    rows = [r for r in mapping.agenda if r.prism_resolution is not None]
    assert len(rows) == 1, f"expected 1 routed row, got {len(rows)}"
    return rows[0]


_BUTTON_PROPS = {
    "Weight": {"type": "VARIANT", "value": "Primary"},
    "Disabled": {"type": "VARIANT", "value": "False"},
    "Label#1:0": {"type": "TEXT", "value": "Save"},
}


# --------------------------------------------------------------------------
# Happy path: routed region with componentProperties -> prism_props.
# --------------------------------------------------------------------------


def test_routed_region_gains_typed_props() -> None:
    mapping = _walk(
        _page_with_child(_instance(component_properties=_BUTTON_PROPS)),
        components={"10:1": {"key": "k", "name": "Button"}},
        catalog=_catalog(_entry("k", "Button")),
        prop_schema=_prop_index(
            _BUTTON_SCHEMA,
            families={
                "Button": {"main": "Button", "components": ["Button"]}
            },
        ),
    )
    region = _routed(mapping)

    by = {p.prop: p for p in region.prism_props}
    assert by["type"].value == "ButtonTypes.PRIMARY"
    assert by["type"].value_kind == "expr"
    assert by["disabled"].value == "false"
    assert by["children"].value == "Save"
    assert mapping.summary["prop_resolved"] == 1


def test_lean_response_surfaces_compact_props() -> None:
    mapping = _walk(
        _page_with_child(_instance(component_properties=_BUTTON_PROPS)),
        components={"10:1": {"key": "k", "name": "Button"}},
        catalog=_catalog(_entry("k", "Button")),
        prop_schema=_prop_index(
            _BUTTON_SCHEMA,
            families={
                "Button": {"main": "Button", "components": ["Button"]}
            },
        ),
    )
    lean = leanify_tree_mapping(mapping, "lean")
    row = next(r for r in lean["agenda"] if r.get("prism_props"))

    # Compact triple only — no method/confidence/source_axis in lean.
    assert row["prism_props"][0].keys() == {"prop", "value", "value_kind"}
    props = {p["prop"]: p["value"] for p in row["prism_props"]}
    assert props["type"] == "ButtonTypes.PRIMARY"


# --------------------------------------------------------------------------
# Sub-component selection by Figma name (Table/Table Cell -> TableCell).
# --------------------------------------------------------------------------


def test_subcomponent_selected_by_figma_name() -> None:
    mapping = _walk(
        _page_with_child(
            _instance(
                name="Table/Table Cell",
                component_properties={
                    "Align": {"type": "VARIANT", "value": "Center"}
                },
            )
        ),
        components={"10:1": {"key": "tk", "name": "Table/Table Cell"}},
        catalog=_catalog(_entry("tk", "Tables")),
        prop_schema=_prop_index(
            _TABLE_SCHEMA,
            _TABLECELL_SCHEMA,
            families={
                "Tables": {
                    "main": "Table",
                    "components": ["Table", "TableCell"],
                }
            },
        ),
    )
    region = _routed(mapping)

    # The cell's `align` prop only exists on TableCell, not the family
    # main `Table` — resolving it proves the sub-component was selected.
    assert region.prism_props[0].prop == "align"
    assert region.prism_props[0].value == "center"


# --------------------------------------------------------------------------
# Guards: disabled, no-props, unrouted, summary suppression.
# --------------------------------------------------------------------------


def test_prop_resolution_disabled_emits_nothing() -> None:
    mapping = _walk(
        _page_with_child(_instance(component_properties=_BUTTON_PROPS)),
        components={"10:1": {"key": "k", "name": "Button"}},
        catalog=_catalog(_entry("k", "Button")),
        prop_schema=_prop_index(
            _BUTTON_SCHEMA,
            families={
                "Button": {"main": "Button", "components": ["Button"]}
            },
        ),
        prop_resolution=False,
    )
    region = _routed(mapping)

    assert region.prism_props == []
    assert "prop_resolved" not in mapping.summary


def test_routed_region_without_component_properties() -> None:
    """A routed region whose node had no componentProperties gets none."""
    mapping = _walk(
        _page_with_child(_instance(component_properties=None)),
        components={"10:1": {"key": "k", "name": "Button"}},
        catalog=_catalog(_entry("k", "Button")),
        prop_schema=_prop_index(
            _BUTTON_SCHEMA,
            families={
                "Button": {"main": "Button", "components": ["Button"]}
            },
        ),
    )
    region = _routed(mapping)

    assert region.prism_props == []
    assert "prop_resolved" not in mapping.summary


def test_unrouted_instance_gets_no_props() -> None:
    """A catalog miss + non-cascadable name means no routing, no props.

    The name "Zzz Mystery Layer" deliberately does not family-name match
    any Prism component, so the page-fallback cascade also declines and
    ``prism_resolution`` stays ``None``.
    """
    mapping = _walk(
        _page_with_child(
            _instance(
                name="Zzz Mystery Layer",
                component_properties=_BUTTON_PROPS,
            )
        ),
        components={"10:1": {"key": "absent", "name": "Zzz Mystery Layer"}},
        catalog=_catalog(_entry("different-key", "Button")),
        prop_schema=_prop_index(
            _BUTTON_SCHEMA,
            families={
                "Button": {"main": "Button", "components": ["Button"]}
            },
        ),
    )

    assert all(r.prism_resolution is None for r in mapping.agenda)
    assert all(r.prism_props == [] for r in mapping.agenda)
    assert "prop_resolved" not in mapping.summary


def test_missing_prop_schema_is_non_fatal() -> None:
    """No injected schema + no committed artifact match still walks.

    With a real committed artifact present this simply resolves against
    it; the assertion is only that routing output survives and the walk
    does not raise regardless of prop-resolution outcome.
    """
    mapping = _walk(
        _page_with_child(_instance(component_properties=_BUTTON_PROPS)),
        components={"10:1": {"key": "k", "name": "Button"}},
        catalog=_catalog(_entry("k", "Button")),
        prop_schema=None,
    )
    region = _routed(mapping)

    assert region.prism_resolution is not None
    assert region.prism_resolution.prism_component == "Button"
