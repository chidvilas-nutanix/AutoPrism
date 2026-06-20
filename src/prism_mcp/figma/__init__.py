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

from prism_mcp.figma.mocks import mock_path_for, try_load_mock
from prism_mcp.figma.models import (
    DroppedNode,
    FigmaTreeMapping,
    LayoutNode,
    MapFigmaTreeInput,
    MappedRegion,
    leanify_tree_mapping,
)
from prism_mcp.figma.walker import walk_tree

__all__ = [
    "DroppedNode",
    "FigmaTreeMapping",
    "LayoutNode",
    "MapFigmaTreeInput",
    "MappedRegion",
    "leanify_tree_mapping",
    "mock_path_for",
    "try_load_mock",
    "walk_tree",
]
