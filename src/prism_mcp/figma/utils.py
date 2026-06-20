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
literals. Figma's fill arrays may contain either form depending
on whether the variable was authored as the short form.
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


def shape_bucket(
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Classify a bbox by aspect ratio and absolute size.

    Returns one of:

    * ``"icon"`` — area < ~1024 px² (~ 32×32). Every Figma page has
      dozens of these; the bucket exists so the ranker prefers
      Icon-family components over Card/Tile when the geometry is
      this small.
    * ``"banner"`` — wider than 600 px AND height ≤ 100 px. The
      classic page-banner / status-banner shape (long horizontal
      strip).
    * ``"sidebar"`` — taller than 400 px AND width ≤ 300 px. The
      classic left/right nav rail shape.
    * ``"page"`` — width ≥ 1000 px AND height ≥ 600 px. The
      page-scale FRAMEs that anchor a route.
    * ``"modal"`` — width ≥ 400 px AND height ≥ 300 px AND
      aspect ratio between 0.5 and 2.0. Dialog / drawer / large
      card geometry.
    * ``"tile"`` — square-ish (0.7 ≤ w/h ≤ 1.4) AND 50 ≤ w ≤ 400.
      Stat tile / KPI card / dashboard tile shape.
    * ``"card"`` — wider than tall (w/h > 1.4) with moderate area
      (< 200_000 px²). Horizontal list item / row shape.
    * ``"block"`` — everything else; the catch-all when none of
      the more specific shapes match.
    * ``""`` — empty bbox (``None`` or zero width/height). The
      empty string is intentional so callers can treat it as
      "no signal" without raising.

    The thresholds match what designers actually ship in Prism
    pages (verified against the three Figma-basics fixtures); a
    future tweak should re-run :data:`docs/figma-page-to-prism-
    plan.md` §8.5 spot-checks.
    """
    if bbox is None:
        return ""
    _x, _y, w, h = bbox
    if w <= 0 or h <= 0:
        return ""
    area = w * h
    aspect = w / h
    if area < 1024:
        return "icon"
    if w >= 1000 and h >= 600:
        return "page"
    if w >= 600 and h <= 100:
        return "banner"
    if h >= 400 and w <= 300:
        return "sidebar"
    if w >= 400 and h >= 300 and 0.5 <= aspect <= 2.0:
        return "modal"
    if 0.7 <= aspect <= 1.4 and 50 <= w <= 400:
        return "tile"
    if aspect > 1.4 and area < 200_000:
        return "card"
    return "block"


# --------------------------------------------------------------------------
# Visual-presence + box-style extraction.
#
# These helpers exist to answer the question *"does this FRAME paint
# something the user sees?"* and *"what CSS-ish box style would a
# generator need to reproduce it?"*. The walker uses them at region-
# emission time so the LLM downstream gets fills / borders / corner
# radius / padding as structured facts rather than discovering them
# from the raw JSON.
#
# Padding inference is deliberately split from the auto-layout fast-
# path: Figma's REST API only exposes ``paddingTop/Right/Bottom/Left``
# when ``layoutMode`` is ``"HORIZONTAL"``, ``"VERTICAL"`` or ``"GRID"``
# — see https://developers.figma.com/docs/rest-api/file-node-types/.
# For absolute-positioned designs we derive padding from the
# parent-child bbox offsets using the well-known formula documented in
# Figma-Context-MCP's layout-detection guide.
# --------------------------------------------------------------------------


def _first_visible_solid_hex(entries: list[Any] | None) -> str | None:
    """Return the hex of the first visible SOLID paint, or ``None``.

    "Visible" follows the same rule as :func:`extract_visible_hexes` —
    ``visible != False`` and ``opacity > _OPACITY_FLOOR``. Used by
    :func:`extract_box_style` to summarise background / border colour
    as a single value (the most common case is a single SOLID paint —
    multi-paint stacks are rare for the rectangles we walk and would
    be a follow-up enhancement).
    """
    if not entries:
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("visible") is False:
            continue
        opacity = entry.get("opacity", 1.0)
        try:
            if float(opacity) <= _OPACITY_FLOOR:
                continue
        except (TypeError, ValueError):
            continue
        if entry.get("type") != "SOLID":
            continue
        hex_value = _colour_to_hex(entry.get("color", {}))
        if hex_value is not None:
            return hex_value
    return None


def _has_visible_shadow(effects: list[Any] | None) -> bool:
    """Return True iff any visible drop/inner shadow effect exists."""
    if not effects:
        return False
    for fx in effects:
        if not isinstance(fx, dict):
            continue
        if fx.get("visible") is False:
            continue
        if fx.get("type") in {"DROP_SHADOW", "INNER_SHADOW"}:
            return True
    return False


def _coerce_corner_radius(
    node: dict[str, Any],
) -> float | list[float] | None:
    """Return the node's corner radius in a JSON-friendly form.

    Figma exposes two fields:

    * ``cornerRadius`` (single float) when all four corners share
      the same value.
    * ``rectangleCornerRadii`` (``[tl, tr, br, bl]``) when corners
      differ.

    We prefer the single form when it's available and the list is
    either absent or homogeneous, falling back to the list otherwise.
    Missing / zero values return ``None`` so the field stays out of
    the agenda entirely when there's no rounding to surface.
    """
    rect = node.get("rectangleCornerRadii")
    if isinstance(rect, list) and len(rect) == 4:
        try:
            corners = [float(v) for v in rect]
        except (TypeError, ValueError):
            corners = []
        if corners:
            if all(c == corners[0] for c in corners):
                return corners[0] if corners[0] > 0 else None
            return corners

    single = node.get("cornerRadius")
    if isinstance(single, (int, float)) and single > 0:
        return float(single)
    return None


def has_visual_presence(node: dict[str, Any]) -> bool:
    """Return True iff the node paints something the user would see.

    A FRAME is a *visual container* when it has at least one of:

    * a visible SOLID fill (background colour),
    * a visible stroke (border),
    * a corner radius > 0 (rounded card),
    * a visible drop/inner shadow effect.

    Used by :func:`prism_mcp.figma.routing.classify_frame_role` to
    distinguish a "card / panel / banner" FRAME (which deserves its
    own :class:`MappedRegion` so its background, border and corner
    radius reach the generator) from a pure layout container (a
    spacer FRAME that only groups children).

    Before this helper landed, FRAMEs like ``Status/Alert Banner``
    (one child, but a visible grey fill and ``cornerRadius=2``) were
    classified as ``layout-container`` and silently passed through,
    losing the entire visual identity of the alert.
    """
    if has_visible_fill_or_stroke(node):
        return True
    if _coerce_corner_radius(node) is not None:
        return True
    if _has_visible_shadow(node.get("effects")):
        return True
    return False


def _pad_quad(
    top: float,
    right: float,
    bottom: float,
    left: float,
    *,
    floor: float = 0.5,
) -> tuple[float, float, float, float] | None:
    """Return a (top, right, bottom, left) tuple, or ``None`` if all
    four values are below ``floor`` pixels.

    Sub-pixel offsets are noise (the Figma renderer regularly emits
    half-pixel coordinates) and we don't want to clutter the agenda
    with ``"padding": [0.5, 0.0, 0.5, 0.0]`` rows.
    """
    if max(top, right, bottom, left) < floor:
        return None
    return (
        round(top, 2),
        round(right, 2),
        round(bottom, 2),
        round(left, 2),
    )


def infer_padding(
    node: dict[str, Any],
    *,
    children: list[dict[str, Any]] | None = None,
) -> tuple[float, float, float, float] | None:
    """Infer the padding inside ``node`` as a ``(T, R, B, L)`` tuple.

    Two paths, in order:

    1. **Auto-layout fast path.** When ``node["layoutMode"]`` is
       ``"HORIZONTAL"`` / ``"VERTICAL"`` / ``"GRID"`` we trust
       Figma's own ``paddingTop`` / ``paddingRight`` /
       ``paddingBottom`` / ``paddingLeft`` fields (the REST API only
       populates them in this case — see Figma node-type docs).
    2. **Absolute-positioned fallback.** For ``layoutMode == "NONE"``
       or missing, derive padding from the parent-child bbox offsets
       using the canonical formula popularised by Figma-Context-MCP's
       layout-detection guide:

       ::

           paddingLeft   = min(children.left)   - parent.left
           paddingTop    = min(children.top)    - parent.top
           paddingRight  = parent.right         - max(children.right)
           paddingBottom = parent.bottom        - max(children.bottom)

       Children that fall outside the parent bbox (negative
       computed padding) are ignored — Figma allows overflow but we
       only surface positive insets.

    Returns ``None`` when no padding is detectable (no children, or
    every inset is sub-pixel). Callers should treat ``None`` as
    "skip the field" rather than "padding is 0".
    """
    layout_mode = node.get("layoutMode")
    if layout_mode in {"HORIZONTAL", "VERTICAL", "GRID"}:
        try:
            return _pad_quad(
                float(node.get("paddingTop", 0) or 0),
                float(node.get("paddingRight", 0) or 0),
                float(node.get("paddingBottom", 0) or 0),
                float(node.get("paddingLeft", 0) or 0),
            )
        except (TypeError, ValueError):
            return None

    children = children if children is not None else iter_children(node)
    if not children:
        return None

    parent_bbox = node.get("absoluteBoundingBox") or {}
    try:
        px = float(parent_bbox["x"])
        py = float(parent_bbox["y"])
        pw = float(parent_bbox["width"])
        ph = float(parent_bbox["height"])
    except (KeyError, TypeError, ValueError):
        return None
    p_right = px + pw
    p_bottom = py + ph

    child_lefts: list[float] = []
    child_tops: list[float] = []
    child_rights: list[float] = []
    child_bottoms: list[float] = []
    for child in children:
        b = child.get("absoluteBoundingBox") or {}
        try:
            cx = float(b["x"])
            cy = float(b["y"])
            cw = float(b["width"])
            ch = float(b["height"])
        except (KeyError, TypeError, ValueError):
            continue
        child_lefts.append(cx)
        child_tops.append(cy)
        child_rights.append(cx + cw)
        child_bottoms.append(cy + ch)
    if not child_lefts:
        return None

    return _pad_quad(
        max(0.0, min(child_tops) - py),
        max(0.0, p_right - max(child_rights)),
        max(0.0, p_bottom - max(child_bottoms)),
        max(0.0, min(child_lefts) - px),
    )


def extract_box_style(
    node: dict[str, Any],
    *,
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a CSS-ish style dict for ``node``.

    The returned dict mirrors the
    :class:`prism_mcp.figma.models.BoxStyle` shape (the model
    constructor accepts it directly) and intentionally uses CSS-
    aligned property names — ``background_color``, ``border_color``,
    ``corner_radius``, ``padding``, ``gap`` — so an LLM generator
    can render it without translating Figma internals. This mirrors
    the approach used by the Figma-Context-MCP and figma-to-code-mcp
    open-source projects (see web research notes in design doc
    §4.4.1.1).

    Empty / absent properties are omitted rather than set to falsey
    values: the agenda stays compact and the downstream model
    serialisation skips them entirely.
    """
    style: dict[str, Any] = {}
    bg = _first_visible_solid_hex(node.get("fills"))
    if bg is not None:
        style["background_color"] = bg
    border = _first_visible_solid_hex(node.get("strokes"))
    if border is not None:
        style["border_color"] = border
        try:
            sw = node.get("strokeWeight")
            if isinstance(sw, (int, float)) and sw > 0:
                style["border_width"] = float(sw)
        except (TypeError, ValueError):
            pass
    corner = _coerce_corner_radius(node)
    if corner is not None:
        style["corner_radius"] = corner
    if _has_visible_shadow(node.get("effects")):
        style["has_shadow"] = True
    layout_mode = node.get("layoutMode")
    if layout_mode and layout_mode != "NONE":
        style["layout_mode"] = layout_mode
        try:
            gap = node.get("itemSpacing")
            if isinstance(gap, (int, float)) and gap > 0:
                style["gap"] = float(gap)
        except (TypeError, ValueError):
            pass
    padding = infer_padding(node, children=children)
    if padding is not None:
        style["padding"] = padding
    try:
        opacity = node.get("opacity")
        if (
            isinstance(opacity, (int, float))
            and 0 <= float(opacity) < 0.999
            and float(opacity) > _OPACITY_FLOOR
        ):
            style["opacity"] = float(opacity)
    except (TypeError, ValueError):
        pass
    return style
