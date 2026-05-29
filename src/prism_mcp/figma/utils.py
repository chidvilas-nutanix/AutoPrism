"""Pure helpers used by the Figma walker / filter / patterns.

Everything here is a side-effect-free function over Figma JSON
dicts. We deliberately do **not** depend on Pydantic models in this
module — the helpers operate on the raw ``dict`` shapes that come
straight from the REST API, so :mod:`prism_mcp.figma.walker` can call
them before any model construction.
"""

from __future__ import annotations

import re
from typing import Any

_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
"""Match six-digit (``#1B6BCC``) or three-digit (``#1BC``) hex
literals. We tighten the same regex used in
:mod:`prism_mcp.workflow.reflection` because Figma's fill arrays
may contain either form depending on whether the variable was
authored as the short form.
"""

_OPACITY_FLOOR = 0.01
"""Below this opacity, a fill or stroke is treated as invisible.

The threshold is empirical: Figma designers commonly drop
hit-target rectangles to ``opacity=0.0001`` so they don't render
but still capture pointer events. Anything below 1% is effectively
invisible to a user, but we leave a 0.01 margin for floating-point
noise out of the REST API.
"""


# --------------------------------------------------------------------------
# bbox helpers.
# --------------------------------------------------------------------------


def bbox_tuple_from_dict(
    bbox: dict[str, float] | None,
) -> tuple[float, float, float, float]:
    """Return ``(x, y, width, height)`` from a Figma
    ``absoluteBoundingBox`` dict.

    A missing key is treated as ``0.0`` rather than raising — the
    walker should fail gracefully on incomplete REST payloads, not
    abort the whole page.

    Args:
        bbox (dict[str, float] | None): ``{"x": ..., "y": ...,
            "width": ..., "height": ...}`` or ``None``.

    Returns:
        tuple[float, float, float, float]: a four-tuple suitable
        for :class:`LayoutNode.bbox`.
    """
    if not bbox:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        float(bbox.get("x", 0.0)),
        float(bbox.get("y", 0.0)),
        float(bbox.get("width", 0.0)),
        float(bbox.get("height", 0.0)),
    )


def bboxes_equal(
    a: dict[str, float] | None,
    b: dict[str, float] | None,
    tol: float = 0.5,
) -> bool:
    """Return True iff two bbox dicts agree within ``tol`` pixels.

    The tolerance is half a pixel — sub-pixel offsets are common in
    Figma exports because the renderer uses fractional coordinates,
    but a half-pixel diff is well below human perception and well
    below the granularity a designer would have introduced
    intentionally. See design doc §4.3 Pass 4.

    Args:
        a (dict[str, float] | None): first bbox.
        b (dict[str, float] | None): second bbox.
        tol (float): tolerance in pixels per dimension.

    Returns:
        bool: ``True`` when every dimension agrees within ``tol``.
        ``False`` when either bbox is missing (you cannot be
        "equal" to a missing bbox).
    """
    if not a or not b:
        return False
    keys = ("x", "y", "width", "height")
    return all(
        abs(float(a.get(k, 0.0)) - float(b.get(k, 0.0))) <= tol for k in keys
    )


# --------------------------------------------------------------------------
# Fill / colour extraction.
# --------------------------------------------------------------------------


def _normalise_hex(raw: str) -> str | None:
    """Return uppercased ``#XXXXXX``, or ``None`` if not hex-shaped.

    Three-digit shorthand is expanded so callers can dedupe on a
    single canonical form.
    """
    m = _HEX_RE.search(raw)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return f"#{digits.upper()}"


def _colour_to_hex(colour: dict[str, float]) -> str | None:
    """Convert a Figma SOLID fill colour dict to ``#XXXXXX``.

    Figma REST emits ``{"r": 0.106, "g": 0.42, "b": 0.8, "a": 1}``
    with channels in ``[0, 1]``; we round to nearest 8-bit value.
    """
    if not isinstance(colour, dict):
        return None
    try:
        r = max(0, min(255, round(float(colour["r"]) * 255)))
        g = max(0, min(255, round(float(colour["g"]) * 255)))
        b = max(0, min(255, round(float(colour["b"]) * 255)))
    except (KeyError, TypeError, ValueError):
        return None
    return f"#{r:02X}{g:02X}{b:02X}"


def extract_visible_hexes(node: dict[str, Any]) -> list[str]:
    """Return de-duplicated visible fill hexes on ``node``.

    Only SOLID fills are considered (gradient stops aren't a
    token-mapping signal for our purposes). A fill is considered
    "visible" iff its ``visible`` flag is not ``False`` *and* its
    opacity is above :data:`_OPACITY_FLOOR`. The node-level
    ``opacity`` is *not* checked here because Pass 2 already
    handles node-level invisibility.

    Args:
        node (dict): Figma SceneNode dict.

    Returns:
        list[str]: ``["#XXXXXX", ...]`` in first-seen order.
    """
    fills = node.get("fills") or []
    seen: dict[str, None] = {}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        if fill.get("visible") is False:
            continue
        opacity = fill.get("opacity", 1.0)
        try:
            if float(opacity) <= _OPACITY_FLOOR:
                continue
        except (TypeError, ValueError):
            continue
        if fill.get("type") != "SOLID":
            continue
        hex_value = _colour_to_hex(fill.get("color", {}))
        if hex_value is None:
            continue
        seen.setdefault(hex_value, None)
    return list(seen.keys())


def has_visible_fill_or_stroke(node: dict[str, Any]) -> bool:
    """Return True iff any fill or stroke on the node is visible.

    "Visible" follows the same rule as :func:`extract_visible_hexes`
    — ``visible != False`` and ``opacity > _OPACITY_FLOOR``. Used
    by Pass 2 of the noise filter to drop spacer rectangles.
    """
    for collection_key in ("fills", "strokes"):
        for entry in node.get(collection_key) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("visible") is False:
                continue
            opacity = entry.get("opacity", 1.0)
            try:
                if float(opacity) > _OPACITY_FLOOR:
                    return True
            except (TypeError, ValueError):
                continue
    return False


# --------------------------------------------------------------------------
# Tree traversal helpers.
# --------------------------------------------------------------------------


def iter_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the children list (or ``[]`` if missing).

    Centralised so a future change to the Figma JSON shape (e.g.
    children rolled into a sub-key) only needs a single edit.
    """
    children = node.get("children")
    if not isinstance(children, list):
        return []
    return [c for c in children if isinstance(c, dict)]


def collect_descendant_types(node: dict[str, Any]) -> set[str]:
    """Return the set of ``type`` values present anywhere below
    ``node`` (excluding ``node`` itself).

    Used by the Pass-5 icon heuristic to confirm a small container
    only wraps icon-shaped primitives. Iterative implementation —
    Figma trees can be deep enough (think nested COMPONENT_SET
    variants) that recursion is a footgun.
    """
    types: set[str] = set()
    stack: list[dict[str, Any]] = list(iter_children(node))
    while stack:
        cur = stack.pop()
        node_type = cur.get("type")
        if isinstance(node_type, str):
            types.add(node_type)
        stack.extend(iter_children(cur))
    return types


def count_descendants(node: dict[str, Any]) -> int:
    """Return the number of nodes in ``node``'s subtree, excluding
    ``node`` itself. Used for the ``summary.input_nodes`` counter
    and the ``max_nodes`` safety rail."""
    count = 0
    stack: list[dict[str, Any]] = list(iter_children(node))
    while stack:
        count += 1
        cur = stack.pop()
        stack.extend(iter_children(cur))
    return count


def get_characters(node: dict[str, Any]) -> str:
    """Return the ``characters`` string of a TEXT-shaped node, or
    ``""`` when missing. Centralised because the empty-vs-missing
    distinction differs across REST and Plugin payloads."""
    raw = node.get("characters")
    return raw if isinstance(raw, str) else ""


def bbox_area(node: dict[str, Any]) -> float:
    """Return ``width * height`` from the node's
    ``absoluteBoundingBox``, or ``0.0`` when missing."""
    bbox = node.get("absoluteBoundingBox") or {}
    try:
        return float(bbox.get("width", 0.0)) * float(bbox.get("height", 0.0))
    except (TypeError, ValueError):
        return 0.0
