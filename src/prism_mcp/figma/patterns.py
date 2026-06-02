"""Pattern detectors for the Figma walker.

Each pattern is a pure predicate ``(node, ctx) -> PatternMatch | None``
that the walker calls AFTER role classification. A non-None return
tells the walker:

* Emit one :class:`prism_mcp.figma.models.MappedRegion` for the
  matched subtree (with the chosen ``role`` and content slots).
* Drop every descendant inside that subtree from further routing,
  with reason ``"folded_into_pattern"`` (or ``"icon_internal"`` for
  the icon pattern, per design doc §4.8).

Detection order matters: the most-reductive / cheapest predicates
run first. See :data:`PATTERNS` for the canonical order.

Reference: design doc §4.3 Pass 5 (icons) and §4.5 (5 cluster
patterns).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from prism_mcp.figma.types import PASS5_ICON_CHILD_TYPES
from prism_mcp.figma.utils import (
    bbox_area,
    collect_descendant_types,
    get_characters,
    iter_children,
)

# --------------------------------------------------------------------------
# PatternMatch — what each detector returns on a hit.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternMatch:
    """One pattern detector hit.

    Args:
        kind (str): the matched pattern name (``"icon"`` /
            ``"stat-list"`` / ``"table-column"`` / ``"tab-strip"``
            / ``"button-group"`` / ``"kpi-tile"``). The walker
            uses this as the :class:`MappedRegion.role`.
        content_slots (dict): captured slot data — text, item
            lists, counts, etc.
        structural_hints (list[str]): freeform hint strings
            inserted into the per-region BM25 query.
        children_summary (str): one-line description of the
            cluster's child shape.
        absorbed_ids (list[str]): node ids of every descendant
            the walker should drop with reason
            ``"folded_into_pattern"`` (or ``"icon_internal"``
            for the icon case). Empty when the pattern matched
            the node itself with no children to absorb.
    """

    kind: str
    content_slots: dict[str, str | list[str] | int] = field(
        default_factory=dict
    )
    structural_hints: list[str] = field(default_factory=list)
    children_summary: str = ""
    absorbed_ids: list[str] = field(default_factory=list)
    absorbed_reason: str = "folded_into_pattern"


# --------------------------------------------------------------------------
# Pass 5: icon coalesce.
# --------------------------------------------------------------------------


_ICON_NAME_PREFIX_RE = re.compile(r"^(icon|logo)[/ ]", re.IGNORECASE)


def match_icon(node: dict[str, Any]) -> PatternMatch | None:
    """Return a match if ``node`` looks like an icon.

    Three independent triggers (any one fires):

    1. ``type`` is ``BOOLEAN_OPERATION`` or ``VECTOR`` — Figma's
       canonical icon-internal types. The whole subtree collapses
       into one icon region.
    2. ``name`` matches ``/^(icon|logo)[/ ]/`` — the
       designer-declared icon convention.
    3. The node is small (≤24px in both dimensions) AND every
       descendant's type is in :data:`PASS5_ICON_CHILD_TYPES`.
       Catches glyphs encoded as stacks of vector primitives.

    On match, every descendant id is added to ``absorbed_ids``
    with reason ``"icon_internal"`` (not ``"folded_into_pattern"``)
    — the audit trail distinguishes "icon glyph noise" from
    "design pattern noise" because the histogram is interesting
    in both directions (see design doc §4.8).
    """
    node_type = node.get("type", "")
    name = str(node.get("name", ""))
    children = iter_children(node)

    is_icon_type = node_type in {"BOOLEAN_OPERATION", "VECTOR"}
    has_icon_name = bool(_ICON_NAME_PREFIX_RE.match(name))

    is_small_icon = False
    bbox = node.get("absoluteBoundingBox") or {}
    if bbox.get("width", 0) <= 24 and bbox.get("height", 0) <= 24:
        descendant_types = collect_descendant_types(node)
        if descendant_types and descendant_types <= PASS5_ICON_CHILD_TYPES:
            is_small_icon = True

    if not (is_icon_type or has_icon_name or is_small_icon):
        return None

    absorbed = [
        str(d.get("id", ""))
        for d in _iter_descendant_dicts(node)
        if d.get("id")
    ]
    return PatternMatch(
        kind="icon",
        content_slots={
            "icon_name_hint": name or "icon",
            "stroke_count": len(children),
        },
        structural_hints=[
            f"{int(bbox.get('width', 0))}x{int(bbox.get('height', 0))} icon",
        ],
        children_summary=f"{len(children)} {node_type} child"
        + ("ren" if len(children) != 1 else ""),
        absorbed_ids=absorbed,
        absorbed_reason="icon_internal",
    )


# --------------------------------------------------------------------------
# §4.5.1: stat-list.
# --------------------------------------------------------------------------


_ROW_NAME_RE = re.compile(r"^(row|item|frame \d+)( copy.*)?$", re.IGNORECASE)


def match_stat_list(node: dict[str, Any]) -> PatternMatch | None:
    """A FRAME or GROUP with 2+ row-like FRAME children, each
    containing exactly one TEXT and zero-or-more decorative
    rectangles.

    Real example: ``GROUP "Cluster Details"`` in §8.1.
    """
    if node.get("type") not in {"FRAME", "GROUP"}:
        return None
    children = iter_children(node)
    if len(children) < 2:
        return None

    items: list[str] = []
    for child in children:
        if child.get("type") != "FRAME":
            return None
        name = str(child.get("name", ""))
        if not _ROW_NAME_RE.match(name.strip()):
            return None
        sub_children = iter_children(child)
        text_children = [c for c in sub_children if c.get("type") == "TEXT"]
        non_text_children = [c for c in sub_children if c.get("type") != "TEXT"]
        # Exactly one text + only RECTANGLEs (decorative spacers).
        if len(text_children) != 1:
            return None
        if any(c.get("type") != "RECTANGLE" for c in non_text_children):
            return None
        items.append(get_characters(text_children[0]))

    absorbed = [str(d.get("id", "")) for d in _iter_descendant_dicts(node)]
    return PatternMatch(
        kind="stat-list",
        content_slots={"items": items},
        structural_hints=[
            f"{len(items)}-row vertical stack",
            "label-only"
            if all(":" not in it for it in items)
            else "label-value",
        ],
        children_summary=f"{len(children)} FRAME Row",
        absorbed_ids=absorbed,
    )


# --------------------------------------------------------------------------
# §4.5.2: column-of-cells (data-table column).
# --------------------------------------------------------------------------


_TABLE_COLUMN_NAME_RE = re.compile(r"^table\s*/\s*column\b", re.IGNORECASE)
_TABLE_TITLE_NAME_RE = re.compile(
    r"^table\s*/\s*table\s*title\b", re.IGNORECASE
)
_TABLE_CELL_NAME_RE = re.compile(
    r"^(table\s*/\s*table\s*cell|cell)\b", re.IGNORECASE
)


def match_column_of_cells(node: dict[str, Any]) -> PatternMatch | None:
    """A FRAME or INSTANCE named ``Table/Column`` with one
    ``Table/Table Title`` and N ``Table/Table Cell`` (or ``Cell``)
    children.

    Real examples: §8.2. The Opportunities page has 7 such columns
    built as one-off FRAMEs; the X-Ray Master File pages use the
    published ``Table/Column`` component which materialises as
    ``INSTANCE`` nodes with the same name + same children topology.
    Both shapes carry the same semantic — one logical table column
    that absorbs its header + cell sub-tree — so we accept both
    Figma types here. See ``docs/x-ray-walker-investigation.md`` §8
    "Fix A — pattern guards".

    The title text is allowed to be nested inside the
    ``Table/Table Title`` frame — real Nutanix designs wrap the
    header TEXT in helper layout FRAMEs (``Text + Icon``,
    ``Checkbox + Text``, etc.). We search up to four levels deep
    for the first TEXT inside the title so the predicate matches
    those production designs without losing the strong
    ``Table/Column`` name anchor.
    """
    if node.get("type") not in {"FRAME", "INSTANCE"}:
        return None
    if not _TABLE_COLUMN_NAME_RE.match(str(node.get("name", "")).strip()):
        return None
    children = iter_children(node)
    if len(children) < 2:
        return None

    title_text: str | None = None
    cell_count = 0
    for child in children:
        name = str(child.get("name", "")).strip()
        if _TABLE_TITLE_NAME_RE.match(name):
            if title_text is None:
                title_text = _first_text_within_depth(child, max_depth=4)
        elif _TABLE_CELL_NAME_RE.match(name):
            cell_count += 1

    if cell_count == 0 or title_text is None:
        return None

    absorbed = [str(d.get("id", "")) for d in _iter_descendant_dicts(node)]
    return PatternMatch(
        kind="table-column",
        content_slots={
            "header": title_text,
            "cell_count": cell_count,
        },
        structural_hints=[
            f"{cell_count}-cell column",
        ],
        children_summary=f"1 Table/Table Title + {cell_count} Table/Table Cell",
        absorbed_ids=absorbed,
    )


# --------------------------------------------------------------------------
# §4.5.3: tab-strip.
# --------------------------------------------------------------------------


_TAB_NAME_RE = re.compile(
    r"(tab|tabs|pill|segment|subheader/tabs)\b", re.IGNORECASE
)


def match_tab_strip(node: dict[str, Any]) -> PatternMatch | None:
    """A FRAME or INSTANCE containing 2+ INSTANCEs whose names match
    the tab convention. The container itself may also be named
    tabs/segment.

    Both FRAME and INSTANCE shapes are accepted because production
    pages frequently materialise the tab bar as a published
    ``Tabs`` component (INSTANCE) rather than a one-off FRAME. See
    ``docs/x-ray-walker-investigation.md`` §8 "Fix A — pattern
    guards".
    """
    if node.get("type") not in {"FRAME", "INSTANCE"}:
        return None
    children = iter_children(node)
    instances = [c for c in children if c.get("type") == "INSTANCE"]
    if len(instances) < 2:
        return None

    tab_like = [
        c for c in instances if _TAB_NAME_RE.search(str(c.get("name", "")))
    ]
    if len(tab_like) < 2:
        return None

    labels = [get_characters_of_first_text(c) for c in tab_like]
    labels = [label for label in labels if label]

    absorbed = [str(d.get("id", "")) for d in _iter_descendant_dicts(node)]
    return PatternMatch(
        kind="tab-strip",
        content_slots={"items": labels} if labels else {},
        structural_hints=[
            f"{len(tab_like)}-tab strip",
        ],
        children_summary=f"{len(tab_like)} tab INSTANCE",
        absorbed_ids=absorbed,
    )


# --------------------------------------------------------------------------
# §4.5.4: button-group.
# --------------------------------------------------------------------------


_BUTTON_NAME_RE = re.compile(r"(action/button|button|btn|cta)\b", re.IGNORECASE)


def match_button_group(node: dict[str, Any]) -> PatternMatch | None:
    """A FRAME / GROUP containing 2+ button-like INSTANCEs in a
    tight bbox.

    "Tight" is operationalised as: the container's area is at
    most 3x the sum of the button bboxes — protects against
    catching every modal that happens to have a couple of
    buttons mixed in with other content.
    """
    if node.get("type") not in {"FRAME", "GROUP"}:
        return None
    children = iter_children(node)
    buttons = [
        c
        for c in children
        if c.get("type") == "INSTANCE"
        and _BUTTON_NAME_RE.search(str(c.get("name", "")))
    ]
    if len(buttons) < 2:
        return None

    container_area = bbox_area(node)
    button_area = sum(bbox_area(b) for b in buttons)
    if (
        container_area > 0
        and button_area > 0
        and container_area > 3 * button_area
    ):
        return None

    labels = [get_characters_of_first_text(b) for b in buttons]
    labels = [label for label in labels if label]

    absorbed = [str(d.get("id", "")) for d in _iter_descendant_dicts(node)]
    return PatternMatch(
        kind="button-group",
        content_slots={"items": labels} if labels else {},
        structural_hints=[
            f"{len(buttons)}-button group",
        ],
        children_summary=f"{len(buttons)} button INSTANCE",
        absorbed_ids=absorbed,
    )


# --------------------------------------------------------------------------
# §4.5.5: kpi-tile.
# --------------------------------------------------------------------------


_KPI_TILE_MAX_EDGE = 400
"""Hard cap on the longer bbox edge of a kpi-tile, in pixels.

A real KPI tile in a Nutanix-style dashboard is at most ~300px on each
side; the 400px ceiling adds headroom without admitting whole pages.
Without this gate the predicate would (and did) match a 1280×800 page
whose ratio happens to fall inside the loose 1:3-3:1 aspect band — see
audit findings on nodes 624:6826 and 667:211 from the Figma-basics
file. See walker safety-rail discussion in the README's "patterns"
section."""

_KPI_TILE_MAX_DESCENDANTS = 30
"""Hard cap on total descendants for a candidate kpi-tile.

A real kpi-tile has at most a handful of internal nodes (a couple of
TEXTs plus optional icon plumbing). 30 leaves plenty of slack for
icon-as-VECTOR stacks while ruling out the catastrophic case where a
400-node page-content frame happens to satisfy the size and text
heuristics."""

_KPI_TILE_MAX_TEXT_NODES = 6
"""Cap on TEXT nodes within the (depth-limited) search.

The canonical kpi-tile has 1 value + 1 label, sometimes a unit or a
caption. 6 leaves margin without admitting "page with one H1 and
twenty body labels"."""

_KPI_TILE_TEXT_MAX_DEPTH = 3
"""Maximum subtree depth from the candidate node at which a TEXT
descendant still counts toward the value/label decision.

A real kpi-tile's value and label are direct children or wrapped in
at most a couple of layout FRAMEs; texts buried >3 levels deep belong
to nested clusters, not to the tile itself. Limits work too — large
subtrees stop early."""


def match_kpi_tile(node: dict[str, Any]) -> PatternMatch | None:
    """A small FRAME / INSTANCE roughly square (1:3 < w:h < 3:1)
    holding one big TEXT (font ≥ 24) and one small TEXT (font < 16),
    near the top of its subtree.

    Real example: dashboard KPI cards like "Active Clusters: 12".

    The four caps (``_KPI_TILE_MAX_EDGE``, ``_KPI_TILE_MAX_DESCENDANTS``,
    ``_KPI_TILE_MAX_TEXT_NODES``, ``_KPI_TILE_TEXT_MAX_DEPTH``) make
    this predicate genuinely "leaf-scale". Without them the aspect-ratio
    + descendant-text check would match any container whose ratio falls
    in (0.33, 3.0) AND whose subtree happens to contain exactly one
    ≥24pt TEXT — empirically that's most app pages, since most pages
    have a single H1.
    """
    if node.get("type") not in {"FRAME", "INSTANCE"}:
        return None
    bbox = node.get("absoluteBoundingBox") or {}
    w = float(bbox.get("width", 0))
    h = float(bbox.get("height", 0))
    if w <= 0 or h <= 0:
        return None
    if max(w, h) > _KPI_TILE_MAX_EDGE:
        return None
    ratio = w / h
    if ratio < (1.0 / 3.0) or ratio > 3.0:
        return None

    descendants = _iter_descendant_dicts(node)
    if len(descendants) > _KPI_TILE_MAX_DESCENDANTS:
        return None

    texts = list(
        _iter_texts_within_depth(node, max_depth=_KPI_TILE_TEXT_MAX_DEPTH)
    )
    if len(texts) > _KPI_TILE_MAX_TEXT_NODES:
        return None

    big = [t for t in texts if t[0] >= 24]
    small = [t for t in texts if t[0] < 16]
    if len(big) != 1 or not small:
        return None

    absorbed = [str(d.get("id", "")) for d in descendants]
    return PatternMatch(
        kind="kpi-tile",
        content_slots={
            "value": big[0][1],
            "label": small[0][1],
        },
        structural_hints=[
            f"{int(w)}x{int(h)} kpi tile",
            "value+label",
        ],
        children_summary="1 big TEXT + 1 small TEXT",
        absorbed_ids=absorbed,
    )


def _iter_texts_within_depth(
    node: dict[str, Any],
    *,
    max_depth: int,
) -> list[tuple[float, str]]:
    """Yield ``(font_size, characters)`` for TEXT descendants of
    ``node`` whose depth (root = 0) is at most ``max_depth``.

    Centralised so :func:`match_kpi_tile` can scope its text search to
    near-direct descendants without re-scanning the whole subtree the
    way the original implementation did. We yield in DFS order; callers
    that need a list materialise themselves.
    """
    out: list[tuple[float, str]] = []
    stack: list[tuple[dict[str, Any], int]] = [
        (c, 1) for c in iter_children(node)
    ]
    while stack:
        cur, depth = stack.pop()
        if cur.get("type") == "TEXT":
            chars = get_characters(cur)
            if chars:
                out.append((_extract_font_size(cur), chars))
        if depth < max_depth:
            stack.extend((c, depth + 1) for c in iter_children(cur))
    return out


def _extract_font_size(text_node: dict[str, Any]) -> float:
    """Return the ``fontSize`` from a TEXT node's ``style`` dict.

    Figma REST nests font properties under ``style``. Missing or
    malformed values return ``0.0`` so the pattern detector can
    fail gracefully (no font size = no kpi match)."""
    style = text_node.get("style")
    if not isinstance(style, dict):
        return 0.0
    try:
        return float(style.get("fontSize", 0))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Pattern registry. Order = priority.
# --------------------------------------------------------------------------


PATTERNS = [
    match_icon,
    match_column_of_cells,
    match_button_group,
    match_tab_strip,
    match_stat_list,
    match_kpi_tile,
]
"""Pattern evaluation order.

The walker calls each predicate in order; the first non-``None``
return wins. Ordering rationale:

1. **icon** — cheapest, most reductive (collapses 3-8 vectors per
   icon, and a page has 50+ icons). Must run before any
   container-level pattern would over-eagerly absorb the icon
   into its own children_summary.
2. **column-of-cells** — name-anchored (``Table/Column``) so very
   unambiguous; saves dozens of mapping rows per page.
3. **button-group** — INSTANCE-name anchored, must run before
   tab-strip in case both regexes loosely match.
4. **tab-strip** — INSTANCE-name anchored.
5. **stat-list** — structural pattern (Row children with text),
   triggers later because it doesn't have a name anchor.
6. **kpi-tile** — last because it's the loosest (any roughly-square
   container with a big TEXT + small TEXT).
"""


PATTERNS_LEAF_SCALE: frozenset = frozenset(
    {match_button_group, match_stat_list, match_kpi_tile}
)
"""Patterns that are only meaningful for small / leaf-scale clusters.

These predicates rely on shape + text heuristics without a strong layer-
name anchor, so they can over-match catastrophically on page-scale
FRAMEs. The walker (:func:`prism_mcp.figma.walker._try_cluster_patterns`)
skips them whenever a candidate node's bounding box exceeds
:data:`PAGE_SCALE_MIN_EDGE` on either axis — at that scale only the
name-anchored patterns (icon / column-of-cells / tab-strip) get a
chance to match. See design doc §4.4.1: a page-sized FRAME should
classify as a ``composed-region``, never as a leaf-scale pattern.
"""


PAGE_SCALE_MIN_EDGE = 600
"""A FRAME whose larger bbox edge exceeds this is treated as
page-scale.

At page scale we restrict pattern detection to the name-anchored
predicates (icon / column-of-cells / tab-strip). The threshold (600px)
is well above any realistic kpi-tile / button-group / stat-list and
well below any tablet-or-larger viewport, so it cleanly separates
"cluster candidate" from "page container".
"""


# --------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------


def _iter_descendant_dicts(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Iterative enumeration of all descendant dicts under ``node``.

    Local copy of :func:`prism_mcp.figma.walker._iter_descendants`
    to avoid an import cycle. The function is small enough that
    duplicating it costs less than a refactor.
    """
    out: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = list(iter_children(node))
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(iter_children(cur))
    return out


def get_characters_of_first_text(node: dict[str, Any]) -> str:
    """Return ``characters`` of the first descendant TEXT node,
    or ``""``. Used by tab/button label extraction."""
    for descendant in _iter_descendant_dicts(node):
        if descendant.get("type") == "TEXT":
            chars = get_characters(descendant)
            if chars:
                return chars
    return ""


def _first_text_within_depth(
    node: dict[str, Any],
    *,
    max_depth: int,
) -> str | None:
    """Return the first non-empty TEXT ``characters`` found within
    ``max_depth`` levels of ``node`` (DFS, depth 1 = direct child).

    Used by :func:`match_column_of_cells` to locate the column header
    text even when the designer has wrapped it in helper layout
    FRAMEs (``Text + Icon``, ``Checkbox + Text``, etc.). Keeps the
    search bounded so we don't accidentally pick a TEXT belonging to
    a nested cluster.
    """
    stack: list[tuple[dict[str, Any], int]] = [
        (c, 1) for c in iter_children(node)
    ]
    while stack:
        cur, depth = stack.pop(0)  # BFS so shallowest text wins
        if cur.get("type") == "TEXT":
            chars = get_characters(cur)
            if chars:
                return chars
        if depth < max_depth:
            stack.extend((c, depth + 1) for c in iter_children(cur))
    return None
