"""Routing layer: pick an action per node based on type / role.

The router runs **after** the noise filter passes (see
:mod:`prism_mcp.figma.filter`). For each surviving node it returns a
:class:`RouterDecision` telling the walker what to do — recurse,
pass-through, emit a region, capture into a parent's content slot,
or hand off to pattern detection.

The split between this module and ``filter.py`` is intentional:

* ``filter.py`` answers *"is this node noise?"* — drop predicates.
* ``routing.py`` answers *"this node is real; what kind of work
  goes here?"* — action selection.

See ``docs/figma-page-to-prism-plan.md`` §4.4 for the action table.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from prism_mcp.figma.types import MAPPABLE_TYPES


class RouterDecision(StrEnum):
    """Possible actions the walker can take per node.

    Values are kept as plain strings so they show up cleanly in
    log lines and in the ``summary`` block. The walker
    canonicalises on these — adding a new value is a contract
    change.
    """

    recurse = "recurse"
    """Pass-through: keep walking children but emit nothing here
    (e.g. PAGE / DOCUMENT / SECTION)."""

    pass_through = "pass_through"
    """Like ``recurse`` but also indicates the node itself has no
    role of its own (raw GROUPs, TRANSFORM_GROUPs). Used by the
    layout-tree builder to avoid spurious wrapper nodes."""

    map_and_stop = "map_and_stop"
    """Emit a :class:`MappedRegion` for this node and do NOT
    recurse further for routing (children may still be inspected
    for content-slot capture). The canonical INSTANCE / COMPONENT
    case."""

    map_and_recurse = "map_and_recurse"
    """Emit a :class:`MappedRegion` for this node AND keep
    routing on its children. The ``composed-region`` FRAME case —
    e.g. a "Page" frame that contains its own header + sidebar
    sub-regions."""

    capture_as_slot = "capture_as_slot"
    """Fold this node into the nearest mapping ancestor's
    ``content_slots`` (TEXT / TEXT_PATH typically). The walker
    appends a :class:`DroppedNode` with reason
    ``captured_as_content_slot``."""

    pattern_candidate = "pattern_candidate"
    """Defer to :mod:`prism_mcp.figma.patterns` — the node might
    match a stat-list / table-column / button-group / tab-strip /
    kpi-tile / icon cluster."""

    drop = "drop"
    """Filter passes already handle most drops; this value is
    reserved for routing-time drops (e.g. SLICE that escaped the
    filter, unknown types we explicitly want to discard)."""


class FrameRole(StrEnum):
    """The four FRAME roles per design doc §4.4.1."""

    component_instance_equivalent = "component-instance-equivalent"
    """The FRAME's name matches a known Prism component shape
    (slash-separated, e.g. ``"Tile/Header"``). Treat it like an
    INSTANCE — map and stop."""

    composed_region = "composed-region"
    """2-10 children, at least one INSTANCE or a name that looks
    like a component. The FRAME itself maps (e.g. to ``<Card>``)
    and we also recurse into children for sub-regions."""

    layout_container = "layout-container"
    """Only contains other FRAMEs/GROUPs — no INSTANCEs, no TEXT
    directly. Pass-through; recurse without emitting a region."""

    pattern_cluster = "pattern-cluster"
    """Matches one of the pattern detectors. The walker defers to
    the matched pattern's ``PatternMatch`` for region emission."""


# --------------------------------------------------------------------------
# Per-type dispatcher.
# --------------------------------------------------------------------------


_RECURSE_TYPES: frozenset[str] = frozenset({"DOCUMENT", "PAGE", "CANVAS"})
"""Document / page nodes are always pure pass-through — the
interesting content is below them."""

_PASS_THROUGH_TYPES: frozenset[str] = frozenset(
    {"GROUP", "TRANSFORM_GROUP", "SECTION"}
)
"""Generic organisational wrappers with no semantic of their own.

GROUPs can still trigger pattern detection (stat-list lives at the
GROUP level in §8.1) — see :func:`route_node` for that handoff.
"""

_MAP_AND_STOP_TYPES: frozenset[str] = frozenset({"INSTANCE", "COMPONENT"})
"""The most reliable mapping targets: INSTANCE references a
published library component; COMPONENT *is* one. Either way, the
node's name is usually the Prism component name."""

_LEAF_RECURSE_FOR_PATTERN: frozenset[str] = frozenset(
    {"BOOLEAN_OPERATION", "VECTOR", "STAR", "POLYGON", "REGULAR_POLYGON"}
)
"""These types are almost always icon-internals; the icon pattern
in :mod:`prism_mcp.figma.patterns` collapses them into a single
icon region."""

_TEXT_TYPES: frozenset[str] = frozenset({"TEXT", "TEXT_PATH"})


def route_node(node: dict[str, Any]) -> RouterDecision:
    """Pick a routing action for one node.

    Args:
        node (dict): a SceneNode dict. Assumed to have survived
            the filter (so ``visible != False``, type not in
            ``DROP_TYPES``, has fills/strokes or descendants).

    Returns:
        RouterDecision: the action the walker should take. The
        ``map_*`` decisions also require the walker to call
        :func:`classify_frame_role` (for FRAME) or to consult
        :mod:`prism_mcp.figma.patterns` (for ``pattern_candidate``).
    """
    node_type = node.get("type")

    if node_type in _RECURSE_TYPES:
        return RouterDecision.recurse

    if node_type in _MAP_AND_STOP_TYPES:
        return RouterDecision.map_and_stop

    if node_type == "COMPONENT_SET":
        # Variant containers: recurse into the default variant.
        # Phase 4 emits a single mapping for the cluster.
        return RouterDecision.recurse

    if node_type == "FRAME":
        # FRAMEs are overloaded; the role classifier picks the
        # right sub-action. The walker calls classify_frame_role
        # to refine into map_and_stop / map_and_recurse /
        # pass_through / pattern_candidate.
        return RouterDecision.pattern_candidate

    if node_type in _PASS_THROUGH_TYPES:
        # GROUPs are also pattern candidates (stat-list lives at
        # the GROUP level in §8.1). The walker hands off to
        # pattern detection first; if no match, falls back to
        # pass-through.
        return RouterDecision.pattern_candidate

    if node_type in _TEXT_TYPES:
        return RouterDecision.capture_as_slot

    if node_type in _LEAF_RECURSE_FOR_PATTERN:
        # Icon-shaped primitives — Pass 5 catches them when the
        # parent is small enough.
        return RouterDecision.pattern_candidate

    if node_type in {"RECTANGLE", "ELLIPSE", "LINE"}:
        # These are decorative leaves by default. The walker may
        # decide they're an icon-component shape via pattern
        # detection, but the per-type default is "no separate
        # region".
        return RouterDecision.capture_as_slot

    if node_type in {"TABLE", "TABLE_CELL"}:
        # Native Figma tables — rare in design files; recurse.
        return RouterDecision.recurse

    if node_type in MAPPABLE_TYPES:
        # Fall-through for any in-set type we haven't matched
        # specifically. Treat as pass-through; the next walker
        # pass picks them up.
        return RouterDecision.pass_through

    # The unknown-type fallback. We do NOT drop unknown types —
    # the walker logs them with reason ``unknown_type_fallback``
    # and treats them as GROUP-equivalents (recurse if they have
    # children, otherwise leaf). See design doc §2.2.
    return RouterDecision.recurse


# --------------------------------------------------------------------------
# FRAME role classifier.
# --------------------------------------------------------------------------


_KNOWN_COMPONENT_SLASH_RE = re.compile(
    r"^[A-Z][A-Za-z0-9]*(?:/[A-Z][A-Za-z0-9]*)+$"
)
"""Match slash-separated PascalCase paths like ``"Tile/Header"``
or ``"Action/Button/Primary"`` — Figma's convention for variant
references that mirror a Prism component name. We're conservative
on purpose: the first segment must be PascalCase (no spaces, no
lowercased starts) so we don't false-positive on layer names like
``"folder/items/3"``."""


def classify_frame_role(node: dict[str, Any]) -> FrameRole:
    """Classify a FRAME into one of four roles.

    Args:
        node (dict): a FRAME-typed SceneNode dict. The caller
            (the walker) is responsible for ensuring
            ``node["type"] == "FRAME"`` before calling this.

    Returns:
        FrameRole: the chosen role. Mapping to actions is the
        walker's job:

        * ``component_instance_equivalent`` → emit one region,
          do not recurse for routing (like INSTANCE).
        * ``composed_region`` → emit one region AND recurse.
        * ``layout_container`` → recurse only.
        * ``pattern_cluster`` → defer to pattern detection.
    """
    name = str(node.get("name", ""))
    children: list[dict[str, Any]] = [
        c for c in node.get("children") or [] if isinstance(c, dict)
    ]
    child_types = [str(c.get("type", "")) for c in children]

    if _KNOWN_COMPONENT_SLASH_RE.match(name.strip()):
        return FrameRole.component_instance_equivalent

    has_instance = any(t == "INSTANCE" for t in child_types)
    has_text_directly = any(t in _TEXT_TYPES for t in child_types)
    only_layout_children = bool(children) and all(
        t in {"FRAME", "GROUP", "TRANSFORM_GROUP"} for t in child_types
    )

    # A frame with 2-10 children of mixed types is a composed
    # region (the dashboards-page or modal case). Beyond 10
    # children it's likely a pattern cluster (a long list / table
    # column) — defer to pattern detection.
    if 2 <= len(children) <= 10 and (has_instance or has_text_directly):
        return FrameRole.composed_region

    if only_layout_children and not has_instance:
        return FrameRole.layout_container

    # Default to pattern_cluster — pattern detection will either
    # find a match or fall back to layout-container behaviour.
    return FrameRole.pattern_cluster
