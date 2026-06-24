"""Figma → Prism page-level mapping.

This subpackage implements the deterministic Python walker that turns
a Figma node tree (raw REST API JSON) into a *FigmaTreeMapping* — a
pruned layout tree, an ordered agenda of Prism-component decisions,
and a tokens map.

See ``docs/figma-page-to-prism-plan.md`` for the design (especially
§3 architecture, §4 walker spec, §6 fetcher spec).

Public surface
--------------

* :class:`FigmaTreeMapping` — output shape.
* :class:`MapFigmaTreeInput` — MCP tool input shape.
* :func:`walk_tree` — pure-function walker; callers that already have
  the raw Figma JSON (typically test harnesses) hit this directly.

Deliberately not re-exported here (private to this package):

* ``_fetch_figma_tree`` — the Figma REST client. Lives in
  :mod:`prism_mcp.figma.fetch` and is only consumed by the
  ``map_figma_tree`` MCP tool wrapper in :mod:`prism_mcp.server`.
  Keeping it package-private avoids tempting callers to bypass the
  walker or to register the fetcher as a separate MCP tool (see
  design doc §3.6.1).
"""

from __future__ import annotations

from prism_mcp.figma.catalog import (
    CatalogEntry,
    FigmaCatalog,
    RegionResolution,
    get_catalog,
    resolve_prism_component,
)
from prism_mcp.figma.codespec import (
    PrismCodeNode,
    PrismCodeSpec,
    PrismImport,
    PrismProp,
    build_code_spec,
)
from prism_mcp.figma.content import (
    IconIndex,
    bind_text_content,
    build_icon_index,
    resolve_icon,
)
from prism_mcp.figma.layout import (
    detect_fill_children,
    detect_page_shell,
    layout_for_container,
    resolve_prism_layout,
    snap_item_gap,
    snap_padding,
)
from prism_mcp.figma.mocks import mock_path_for, try_load_mock
from prism_mcp.figma.models import (
    ContentBinding,
    DroppedNode,
    FigmaComponentIdentity,
    FigmaTreeMapping,
    LayoutNode,
    MapFigmaTreeInput,
    MappedRegion,
    PrismIcon,
    PrismLayout,
    PrismPageShell,
    Typography,
    leanify_tree_mapping,
)
from prism_mcp.figma.prop_schema import (
    ComponentPropSchema,
    PropSchema,
    PropSchemaIndex,
    get_prop_schema,
)
from prism_mcp.figma.props import (
    PropResolution,
    ResolvedProp,
    resolve_props,
)
from prism_mcp.figma.tokens import (
    ColorTokenResult,
    resolve_color_token,
    resolve_typography,
)
from prism_mcp.figma.walker import walk_tree

__all__ = [
    "CatalogEntry",
    "ColorTokenResult",
    "ComponentPropSchema",
    "ContentBinding",
    "DroppedNode",
    "FigmaCatalog",
    "FigmaComponentIdentity",
    "FigmaTreeMapping",
    "IconIndex",
    "LayoutNode",
    "MapFigmaTreeInput",
    "MappedRegion",
    "PrismCodeNode",
    "PrismCodeSpec",
    "PrismIcon",
    "PrismImport",
    "PrismLayout",
    "PrismPageShell",
    "PrismProp",
    "PropResolution",
    "PropSchema",
    "PropSchemaIndex",
    "RegionResolution",
    "ResolvedProp",
    "Typography",
    "bind_text_content",
    "build_code_spec",
    "build_icon_index",
    "detect_fill_children",
    "detect_page_shell",
    "get_catalog",
    "get_prop_schema",
    "layout_for_container",
    "leanify_tree_mapping",
    "mock_path_for",
    "resolve_color_token",
    "resolve_icon",
    "resolve_prism_component",
    "resolve_prism_layout",
    "resolve_props",
    "resolve_typography",
    "snap_item_gap",
    "snap_padding",
    "try_load_mock",
    "walk_tree",
]
