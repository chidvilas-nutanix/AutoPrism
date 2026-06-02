"""The 7-pass noise filter (passes 1-4 and 6).

The walker visits every node in DFS order and runs each node through
this filter. The first failed check drops the node; the
``DropReason`` enum names the rule that fired so the audit trail
(:class:`prism_mcp.figma.models.DroppedNode`) is machine-readable.

Pass 5 (icon coalescing) and Pass 7 (capture-as-content) are *not*
pure drop predicates — they emit single regions for whole subtrees,
so they live in :mod:`prism_mcp.figma.patterns` and the walker's
routing layer respectively (design doc §4.3, §4.5).

Pass ordering matters: each pass is cheaper than the next, and each
one's drop subsumes the work the next would have done. Hence the
documented order in §4.3 — visible → invisible decoration → bad
type → passthrough collapse → icon coalesce → tiny → routing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from prism_mcp.figma.types import CONTAINER_TYPES, DROP_TYPES
from prism_mcp.figma.utils import (
    bbox_area,
    bboxes_equal,
    get_characters,
    has_visible_fill_or_stroke,
    iter_children,
)


class DropReason(StrEnum):
    """Machine-readable reason codes for the ``dropped`` audit trail.

    Stable strings: the Cursor skill keys off them and the
    ``summary.dropped_by_reason`` histogram is keyed off them too,
    so renaming a value is a breaking change to the MCP contract.
    """

    explicit_hidden = "explicit_hidden"
    invisible_decoration = "invisible_decoration"
    non_design_type = "non_design_type"
    same_bbox_passthrough_collapsed = "same_bbox_passthrough_collapsed"
    icon_internal = "icon_internal"
    redundant_inner_instance = "redundant_inner_instance"
    tiny_decorative = "tiny_decorative"
    captured_as_content_slot = "captured_as_content_slot"
    folded_into_pattern = "folded_into_pattern"
    unknown_type_fallback = "unknown_type_fallback"
    pattern_oversized_reject = "pattern_oversized_reject"
    """A pattern matched but would absorb more than the walker's
    safety-rail threshold of the input tree (default 50%). The
    walker rejects the match, logs the candidate here, and continues
    recursive walking. Guards against shape-only heuristics
    over-matching at page-scale FRAMEs. See walker safety rails."""

    agenda_truncated = "agenda_truncated"
    """The walker emitted a :class:`MappedRegion` for this node
    during the DFS but the post-DFS importance-ranking step
    truncated it from the agenda because ``len(ctx.agenda)``
    exceeded ``max_agenda``. Importance score is
    ``(-parent_chain_depth, -bbox_area, has_text_slot,
    is_pattern_region)``; lowest scorers are dropped. The dropped
    region's mapping payload is discarded too. See
    ``docs/x-ray-walker-investigation.md`` §8 "Fix C"."""

    variant_alternative = "variant_alternative"
    """One of N sibling FRAMEs / INSTANCEs that share a common
    ``Domain/`` name prefix, have non-overlapping comparable bboxes,
    and therefore look like documentation-style variant artboards
    (e.g. *Modal/Empty* next to *Modal/Filled* next to *Modal/Error*
    on an X-Ray Master File). Fix D keeps the first variant as the
    agenda's representative; every subsequent sibling is dropped
    here so the LLM only sees one component decision per logical
    variant group. Real application pages never tile multiple
    independent siblings under the same ``Foo/*`` prefix, so the
    heuristic is a no-op outside design-system documentation files.
    See ``docs/x-ray-walker-investigation.md`` §11.5 + §12 "Fix D"."""


_TINY_AREA_FLOOR = 50.0
"""Square pixels below which a non-text leaf is considered noise.

50 px² is one 7x7 block — well below the smallest meaningful UI
target (Material's 48dp recommendation, WCAG's 24x24 minimum).
Anything smaller is almost always a glyph fragment or a divider
pixel that survived a flatten."""


# --------------------------------------------------------------------------
# Pass 1: visibility flag.
# --------------------------------------------------------------------------


def pass_1_visible(node: dict[str, Any]) -> bool:
    """Return ``False`` when ``node["visible"]`` is explicitly ``False``.

    Drops the entire subtree. Reason:
    :attr:`DropReason.explicit_hidden`.

    Figma's ``visible`` defaults to ``True`` — only an explicit
    ``False`` is a designer's hint that this layer should not
    render. Hidden variants of a published library component are
    the common case.
    """
    return node.get("visible") is not False


# --------------------------------------------------------------------------
# Pass 2: invisible decoration.
# --------------------------------------------------------------------------


def pass_2_invisible_decoration(node: dict[str, Any]) -> bool:
    """Return ``False`` when ``node`` paints nothing and contains
    nothing.

    Container exception: any node with children passes through —
    its visibility is the union of its descendants' visibility.
    Text exception: a TEXT node with non-empty ``characters`` is
    *never* decoration, even when its fills are invisible.

    Otherwise we require **at least one** visible fill or stroke
    (opacity above the floor, ``visible != False``).

    Drops the node only (subtree is still inspected). Reason:
    :attr:`DropReason.invisible_decoration`.
    """
    if iter_children(node):
        return True
    if get_characters(node):
        return True
    return has_visible_fill_or_stroke(node)


# --------------------------------------------------------------------------
# Pass 3: mappable type.
# --------------------------------------------------------------------------


def pass_3_mappable_type(node: dict[str, Any]) -> bool:
    """Return ``False`` when ``node["type"]`` is in
    :data:`DROP_TYPES`.

    Drops the node *and its subtree* — none of these (SLICE,
    FigJam, Slides) wrap descendants we'd want to keep. Reason:
    :attr:`DropReason.non_design_type`.
    """
    return node.get("type") not in DROP_TYPES


# --------------------------------------------------------------------------
# Pass 4: same-bbox passthrough collapse.
# --------------------------------------------------------------------------


def pass_4_collapse_passthrough(
    parent: dict[str, Any],
    significant_children: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the child that ``parent`` should collapse into, or
    ``None`` if no collapse applies.

    Triggers iff:

    * ``parent.type`` is ``GROUP`` or ``FRAME`` (other container
      types like INSTANCE / COMPONENT carry semantic meaning we
      don't want to discard),
    * ``parent`` has exactly one significant child,
    * the child's ``absoluteBoundingBox`` matches ``parent``'s
      within half a pixel.

    "Significant" is what the walker passes in — typically the
    list of children that survived passes 1-3. By accepting that
    list (rather than recomputing it), the filter stays
    side-effect-free and the walker keeps the audit trail in one
    place.

    The collapsed parent's id should be appended to the child's
    ``aliased_ids`` by the caller. Reason for the parent's drop:
    :attr:`DropReason.same_bbox_passthrough_collapsed`.

    Args:
        parent (dict): the candidate wrapper node.
        significant_children (list[dict]): children that survived
            earlier filter passes.

    Returns:
        dict | None: the child to collapse into, or ``None`` to
        keep ``parent`` separate.
    """
    if parent.get("type") not in {"GROUP", "FRAME"}:
        return None
    if len(significant_children) != 1:
        return None
    child = significant_children[0]
    if not bboxes_equal(
        parent.get("absoluteBoundingBox"),
        child.get("absoluteBoundingBox"),
        tol=0.5,
    ):
        return None
    return child


# --------------------------------------------------------------------------
# Pass 6: tiny decorative.
# --------------------------------------------------------------------------


def pass_6_tiny_decorative(node: dict[str, Any]) -> bool:
    """Return ``False`` for sub-50px² nodes that aren't text and
    have no meaningful children.

    "Meaningful children" follows the same logic as pass 2 — a
    container with descendants is exempt, because its job is to
    aggregate. Reason: :attr:`DropReason.tiny_decorative`.

    Examples this catches: bullet dots, 1-pixel dividers that
    escape Pass 2 because they have a visible fill, fragments of
    icon arts that survived Pass 5.
    """
    if bbox_area(node) >= _TINY_AREA_FLOOR:
        return True
    if get_characters(node):
        return True
    if iter_children(node):
        return True
    return node.get("type") in CONTAINER_TYPES
