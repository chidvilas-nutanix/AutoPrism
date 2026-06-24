"""CSS layout inference -> Prism Layout primitive (roadmap P4).

The walker already derives a CSS-aligned :class:`LayoutAnalysis` for every
container (direction / justify / align / gap) via
:func:`prism_mcp.figma.layout_inference.analyze_layout`. This module maps
that decision onto the **Prism Layout vocabulary** so codegen emits
``<FlexLayout flexDirection="column" itemGap="M" padding="20px">`` instead
of a hand-written ``<div style={{display:'flex',…}}>`` — the roadmap's
"no divs / no CSS" layer (`docs/figma-to-prism-codegen-roadmap.md` §3 P4).

Two Prism primitives carry the bulk of structural layout:

* **FlexLayout** — the general flex container. Owns ``flexDirection`` /
  ``alignItems`` / ``justifyContent`` / ``itemGap`` / ``padding`` /
  ``flexWrap``. Default ``flexDirection`` is ``row`` and default
  ``itemSpacing`` is ``20px`` (so a zero-gap row MUST emit
  ``itemGap="none"`` to override it).
* **StackingLayout** — a plain vertical stack (no align/justify props).
  Idiomatic for content/form columns; owns ``itemGap`` / ``padding``.

Spacing snaps to the Prism T-shirt size ladder (verified in
``src/styles/v2/Variables.less:118-124``):
``XS=5  S=10  M=15  L=20  XL=30  XXL=40`` px (``none``=0).

Pure + deterministic: no network, no LLM. ``resolve_prism_layout`` returns
``None`` for non-flow containers (single child, overlap ``stack``) so the
caller simply omits a layout wrapper.
"""

from __future__ import annotations

from typing import Any, Literal

from prism_mcp.figma.layout_inference import analyze_layout
from prism_mcp.figma.models import LayoutAnalysis, PrismLayout, PrismPageShell
from prism_mcp.figma.utils import extract_box_style, infer_padding

# --------------------------------------------------------------------------
# Translation tables — CSS-aligned LayoutAnalysis values -> Prism prop enums.
#
# LayoutAnalysis uses the CSS shorthand (``start`` / ``end``); the Prism
# ``FlexLayout`` props use the full flexbox keywords (``flex-start`` /
# ``flex-end``). Everything else is identity.
# --------------------------------------------------------------------------

_ALIGN_CSS_TO_PRISM: dict[str, str] = {
    "start": "flex-start",
    "end": "flex-end",
    "center": "center",
    "baseline": "baseline",
    "stretch": "stretch",
}

_JUSTIFY_CSS_TO_PRISM: dict[str, str] = {
    "start": "flex-start",
    "end": "flex-end",
    "center": "center",
    "space-between": "space-between",
    "space-around": "space-around",
    "space-evenly": "space-evenly",
}

# Prism flex defaults — we omit a prop whose value equals the default so the
# emitted layout stays minimal and matches the library's own examples.
_DEFAULT_ALIGN_ITEMS = "stretch"  # CSS flex default
_DEFAULT_JUSTIFY = "start"  # CSS flex default (-> flex-start)

# --------------------------------------------------------------------------
# Size ladders. ``itemGap`` uses named T-shirt tokens; ``padding`` uses a
# fixed px set (single value) plus a curated set of vertical-horizontal
# pairs. Both verified against the FlexLayout ``.d.ts`` + Variables.less.
# --------------------------------------------------------------------------

_ITEM_GAP_LADDER: tuple[tuple[int, str], ...] = (
    (0, "none"),
    (5, "XS"),
    (10, "S"),
    (15, "M"),
    (20, "L"),
    (30, "XL"),
    (40, "XXL"),
)
"""``(px, token)`` pairs for ``FlexLayout`` / ``StackingLayout`` ``itemGap``.

The px column mirrors ``@size-xs … @size-xxl`` (Variables.less:119-124).
A measured gap snaps to the nearest entry; ``none`` (0) is emitted
explicitly so it overrides FlexLayout's 20px ``itemSpacing`` default."""

# Per-component ``padding`` token vocabularies, read verbatim from the P3
# prop schema (``data/prism_prop_schema.json`` rplib 2.54.0). Each component
# accepts a different set of single values + symmetric ``{V}px-{H}px`` pairs;
# snapping must respect the *target* component's union so we never emit a
# value the type checker rejects. ``StackingLayout`` is by far the widest
# (every cross pair of its singles), which is why P4 follow-up #4 routes a
# vertical stack's padding through it instead of the narrow FlexLayout set.

_FLEX_PAD_SINGLES: tuple[int, ...] = (0, 5, 10, 15, 20, 30, 40)
_STACK_PAD_SINGLES: tuple[int, ...] = (0, 5, 10, 15, 20, 30, 40)
_CONTAINER_PAD_SINGLES: tuple[int, ...] = (0, 10, 15, 20, 30, 40)

_FLEX_PAD_PAIRS: frozenset[tuple[int, int]] = frozenset(
    {
        (0, 5),
        (0, 10),
        (0, 15),
        (0, 20),
        (5, 0),
        (10, 0),
        (15, 0),
        (20, 0),
        (15, 20),
    }
)
"""``FlexLayout`` ``{V}px-{H}px`` pairs (the narrow curated set)."""

_STACK_PAD_PAIRS: frozenset[tuple[int, int]] = frozenset(
    (v, h) for v in _STACK_PAD_SINGLES for h in _STACK_PAD_SINGLES if v != h
)
"""``StackingLayout`` pairs — every ordered ``(V, H)`` of its singles
where ``V != H`` (42 pairs; verified against the schema union)."""

_CONTAINER_PAD_PAIRS: frozenset[tuple[int, int]] = frozenset(
    {(10, 0), (0, 20), (20, 0), (30, 0)}
)
"""``ContainerLayout`` pairs (the small curated set from its schema)."""

_PAD_SETS: dict[str, tuple[tuple[int, ...], frozenset[tuple[int, int]]]] = {
    "FlexLayout": (_FLEX_PAD_SINGLES, _FLEX_PAD_PAIRS),
    "StackingLayout": (_STACK_PAD_SINGLES, _STACK_PAD_PAIRS),
    "ContainerLayout": (_CONTAINER_PAD_SINGLES, _CONTAINER_PAD_PAIRS),
}


def snap_item_gap(px: float | None) -> str | None:
    """Snap a pixel gap to the nearest Prism ``itemGap`` T-shirt token.

    Returns ``None`` when ``px`` is ``None`` (gap not determinable — the
    caller omits ``itemGap`` and accepts the component default). ``0`` maps
    to ``"none"`` so it overrides FlexLayout's 20px ``itemSpacing`` default.
    Ties resolve to the smaller ladder entry (deterministic).
    """
    if px is None:
        return None
    best_token = _ITEM_GAP_LADDER[0][1]
    best_dist = abs(_ITEM_GAP_LADDER[0][0] - px)
    for ladder_px, token in _ITEM_GAP_LADDER[1:]:
        dist = abs(ladder_px - px)
        if dist < best_dist:
            best_dist = dist
            best_token = token
    return best_token


def _snap_one(px: float, singles: tuple[int, ...]) -> int:
    """Snap a single padding side to the nearest entry in ``singles``."""
    return min(singles, key=lambda v: abs(v - px))


def snap_padding(
    quad: tuple[float, float, float, float] | None,
    component: str = "FlexLayout",
) -> tuple[str | None, str | None]:
    """Map a ``(top, right, bottom, left)`` inset to a Prism ``padding`` token.

    ``component`` selects the target's token vocabulary (P4 follow-up #4):
    ``StackingLayout`` accepts a far wider pair set than ``FlexLayout``, so a
    vertical stack recovers paddings the narrow flex set would have dropped.

    Returns ``(token, note)``:

    * uniform (all four sides equal after snapping) and non-zero ->
      ``("20px", None)``.
    * symmetric (``top==bottom``, ``left==right``) and the
      ``(vertical, horizontal)`` pair is in the component's set ->
      ``("0px-20px", None)``.
    * uniform zero -> ``(None, None)`` (nothing to emit).
    * asymmetric (``top != bottom`` or ``left != right``) -> ``(None,
      "asymmetric padding ... -> use style")``: no Prism padding token can
      express it, so the caller emits a structured ``style`` escape rather
      than dropping the inset silently.
    * a symmetric pair outside the component's set -> ``(None, "<reason>")``.
    """
    if quad is None:
        return None, None
    singles, pairs = _PAD_SETS.get(component, _PAD_SETS["FlexLayout"])
    top, right, bottom, left = quad
    st = _snap_one(top, singles)
    sr = _snap_one(right, singles)
    sb = _snap_one(bottom, singles)
    sl = _snap_one(left, singles)
    if st == sr == sb == sl:
        if st == 0:
            return None, None
        return f"{st}px", None
    if st == sb and sl == sr:
        if (st, sl) in pairs:
            return f"{st}px-{sl}px", None
        return None, f"unsupported {component} padding pair {st}px-{sl}px dropped"
    return None, f"asymmetric padding {(st, sr, sb, sl)} -> use style"


def _luminance(hex_color: str) -> float:
    """Return a quick relative luminance (0=black, 1=white) for ``#RRGGBB``.

    Uses the Rec. 601 weights on the raw 8-bit channels — precise enough for
    the coarse white / dark / colored three-way split that ``ContainerLayout``
    needs (it is NOT a perceptual metric; the P5 token index does that job)."""
    body = hex_color.lstrip("#")
    if len(body) == 3:
        body = "".join(c * 2 for c in body)
    if len(body) < 6:
        return 0.5
    try:
        r = int(body[0:2], 16)
        g = int(body[2:4], 16)
        b = int(body[4:6], 16)
    except ValueError:
        return 0.5
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


# A fill is "white" for ContainerLayout purposes only when it is genuinely
# near-white (#F4+ on every channel ~= luminance 0.96). A light-grey Prism
# surface (#EDF0F2 ~= 0.92) is NOT ContainerLayout-white — it stays a colored
# bg that the P5 token pass resolves to a grey token.
_CONTAINER_WHITE_LUM = 0.96
_CONTAINER_DARK_LUM = 0.18


_MAIN_AXIS_FILL_FIELD: dict[str, str] = {
    "row": "layoutSizingHorizontal",
    "column": "layoutSizingVertical",
}


def detect_fill_children(
    children: list[dict[str, Any]],
    direction: str,
) -> list[str]:
    """Return child ids that fill the container's **main** axis.

    The canonical "filling child" (P4 follow-up #2): a Figma child with
    ``layoutGrow == 1`` (auto-layout grow along the primary axis) or
    ``layoutSizing{Horizontal,Vertical} == "FILL"`` on the main axis. The
    generator wraps each such child in ``<FlexItem flexGrow="1">``.

    ``layoutGrow`` is always main-axis; the ``layoutSizing*`` field is chosen
    to match ``direction`` so a column child that fills its *width* (cross
    axis) is not mistaken for a main-axis fill. Ids are returned in child
    order; ``[]`` when nothing fills.
    """
    fill_field = _MAIN_AXIS_FILL_FIELD.get(direction)
    out: list[str] = []
    for child in children:
        cid = str(child.get("id", ""))
        if not cid:
            continue
        grows = child.get("layoutGrow") in (1, 1.0)
        fills = fill_field is not None and child.get(fill_field) == "FILL"
        if grows or fills:
            out.append(cid)
    return out


def _container_layout(
    node: dict[str, Any],
    children: list[dict[str, Any]],
) -> PrismLayout | None:
    """Resolve a styled **non-flow** box to a ``ContainerLayout`` (P4 #3).

    Fires for single-child / overlap / childless containers that nonetheless
    paint a box (fill / border / shadow / corner radius) whose background can
    be named in ``ContainerLayout``'s three-value vocabulary
    (``white`` / ``dark`` / ``transparent``). A *colored* background (a grey
    surface, a brand fill) is intentionally left ``None`` here — it stays on
    ``box_style`` for the P5 token pass to resolve to a real color token.
    """
    box = extract_box_style(node, children=children)
    bg = box.get("background_color")
    has_border = box.get("border_color") is not None
    has_shadow = bool(box.get("has_shadow"))
    corner = box.get("corner_radius")

    background: str | None = None
    if isinstance(bg, str):
        lum = _luminance(bg)
        if lum >= _CONTAINER_WHITE_LUM:
            background = "white"
        elif lum <= _CONTAINER_DARK_LUM:
            background = "dark"
        # else: colored bg -> not expressible; leave None.
    elif has_border or has_shadow:
        background = "transparent"

    styled = has_border or has_shadow or corner is not None or bg is not None
    if background is None or not styled:
        return None

    props: dict[str, str] = {"backgroundColor": background}
    if has_border:
        props["border"] = "true"
    pad_token, _pad_note = snap_padding(
        infer_padding(node, children=children), "ContainerLayout"
    )
    if pad_token is not None:
        props["padding"] = pad_token
    return PrismLayout(
        component="ContainerLayout",
        props=props,
        source="geometry",
        confidence=0.6,
        notes=["styled non-flow box -> ContainerLayout"],
    )


def resolve_prism_layout(
    node: dict[str, Any],
    analysis: LayoutAnalysis,
    children: list[dict[str, Any]],
) -> PrismLayout | None:
    """Map a CSS :class:`LayoutAnalysis` to a :class:`PrismLayout`.

    Args:
        node (dict[str, Any]): the container's Figma node JSON (read for
            ``paddingTop|Right|Bottom|Left`` via
            :func:`prism_mcp.figma.utils.infer_padding`, ``layoutWrap``, and
            the per-child ``layoutGrow`` / ``layoutSizing*`` fill signals).
        analysis (LayoutAnalysis): the CSS decision from
            :func:`prism_mcp.figma.layout_inference.analyze_layout`.
        children (list[dict[str, Any]]): the immediate child node dicts
            (used by ``infer_padding`` + the fill-child detector).

    Returns:
        PrismLayout | None: a ``FlexLayout`` / ``StackingLayout`` for flow
        containers; a ``ContainerLayout`` for styled non-flow boxes (P4 #3);
        ``None`` for unstyled non-flow containers where no wrapper is
        warranted.
    """
    direction = analysis.direction
    if direction in (None, "single", "stack"):
        return _container_layout(node, children)

    notes: list[str] = []
    props: dict[str, str] = {}

    justify = analysis.justify_content
    align = analysis.align_items
    fill_ids = detect_fill_children(children, direction)

    if direction == "row":
        component: Literal["FlexLayout", "StackingLayout", "ContainerLayout"] = (
            "FlexLayout"
        )
        # row is the FlexLayout default -> omit flexDirection.
    elif direction == "column":
        pure_stack = justify in (None, "start") and align in (
            None,
            "start",
            "stretch",
        )
        # A pure stack with a filling child must still be a FlexLayout so the
        # child can carry FlexItem flexGrow (StackingLayout has no flex item).
        if pure_stack and not fill_ids:
            component = "StackingLayout"
        else:
            component = "FlexLayout"
            props["flexDirection"] = "column"
    elif direction == "grid":
        component = "FlexLayout"
        props["flexWrap"] = "wrap"
        notes.append("figma GRID -> FlexLayout+flexWrap (no Prism grid primitive)")
    else:  # pragma: no cover - LayoutAnalysis.direction is a closed set
        return None

    if component == "FlexLayout":
        if align is not None and align != _DEFAULT_ALIGN_ITEMS:
            mapped = _ALIGN_CSS_TO_PRISM.get(align)
            if mapped is not None:
                props["alignItems"] = mapped
        if justify is not None and justify != _DEFAULT_JUSTIFY:
            mapped = _JUSTIFY_CSS_TO_PRISM.get(justify)
            if mapped is not None:
                props["justifyContent"] = mapped

    gap_token = snap_item_gap(analysis.gap)
    if gap_token is not None:
        props["itemGap"] = gap_token

    pad_token, pad_note = snap_padding(
        infer_padding(node, children=children), component
    )
    if pad_token is not None:
        props["padding"] = pad_token
    elif pad_note is not None:
        notes.append(pad_note)

    if (
        component == "FlexLayout"
        and "flexWrap" not in props
        and node.get("layoutWrap") == "WRAP"
    ):
        props["flexWrap"] = "wrap"

    source: Literal["figma_auto_layout", "geometry"] = (
        "figma_auto_layout"
        if analysis.rationale.startswith("figma_auto_layout")
        else "geometry"
    )

    # FlexItem flexGrow only applies inside a FlexLayout flex context.
    fill_child_ids = fill_ids if component == "FlexLayout" else []

    return PrismLayout(
        component=component,
        props=props,
        source=source,
        confidence=analysis.confidence,
        fill_child_ids=fill_child_ids,
        notes=notes,
    )


def layout_for_container(
    node: dict[str, Any],
    children: list[dict[str, Any]],
) -> PrismLayout | None:
    """Convenience wrapper: ``analyze_layout`` + ``resolve_prism_layout``.

    Lets callers go straight from a container node + its significant
    children to a :class:`PrismLayout` without touching the intermediate
    CSS :class:`LayoutAnalysis`.
    """
    analysis = analyze_layout(node, children)
    return resolve_prism_layout(node, analysis, children)


# --------------------------------------------------------------------------
# Page-shell detection (P4 follow-up #1) — the single route-anchoring frame.
#
# Conservative on purpose: a wrong shell call mangles the whole page, while a
# *missed* shell still renders fine via the FlexLayout column fallback. So we
# only fire on clear, flat header / left-nav / body / footer geometry and bail
# to ``None`` (let FlexLayout handle it) on anything ambiguous.
# --------------------------------------------------------------------------

_SHELL_PAGE_MIN_W = 1000.0
_SHELL_PAGE_MIN_H = 600.0
"""A shell only applies to a page-scale container (matches ``shape_bucket``
``"page"``: w >= 1000 AND h >= 600)."""

_SHELL_FULL_WIDTH_FRAC = 0.85
"""A header / footer spans >= 85% of the parent width."""

_SHELL_BAR_MAX_H_FRAC = 0.25
"""A header / footer is <= 25% of the parent height (a strip, not the body)."""

_SHELL_NAV_MAX_W_FRAC = 0.33
_SHELL_NAV_MIN_H_FRAC = 0.5
"""A left nav is <= 33% of the parent width and >= 50% of its height."""


def _shell_align_tol(extent: float) -> float:
    """Edge-alignment tolerance: max of 2 px or 1% of the parent extent."""
    return max(2.0, 0.01 * extent)


def _classify_shell_child(
    parent_bbox: tuple[float, float, float, float],
    child_bbox: tuple[float, float, float, float],
) -> str:
    """Bucket one top-level child as header / footer / leftNav / body.

    Pure geometry relative to the parent box. Returns ``"body"`` for the
    catch-all (a large interior region) so the caller can pick the largest
    body candidate.
    """
    px, py, pw, ph = parent_bbox
    cx, cy, cw, ch = child_bbox
    if pw <= 0 or ph <= 0 or cw <= 0 or ch <= 0:
        return "body"
    w_frac = cw / pw
    h_frac = ch / ph
    tol_x = _shell_align_tol(pw)
    tol_y = _shell_align_tol(ph)

    full_width = w_frac >= _SHELL_FULL_WIDTH_FRAC
    short = h_frac <= _SHELL_BAR_MAX_H_FRAC
    at_top = abs(cy - py) <= tol_y
    at_bottom = abs((cy + ch) - (py + ph)) <= tol_y
    at_left = abs(cx - px) <= tol_x

    if full_width and short and at_top:
        return "header"
    if full_width and short and at_bottom:
        return "footer"
    if (
        at_left
        and w_frac <= _SHELL_NAV_MAX_W_FRAC
        and h_frac >= _SHELL_NAV_MIN_H_FRAC
    ):
        return "leftNav"
    return "body"


def detect_page_shell(
    node: dict[str, Any],
    children: list[dict[str, Any]],
) -> PrismPageShell | None:
    """Detect a Prism page shell for the route-anchoring container (P4 #1).

    Conservative geometric classifier over the **immediate** children of a
    page-scale frame. Recognised flat arrangements:

    * header + leftNav + body            -> ``MainPageLayout``
    * leftNav + body (no header)         -> ``LeftNavLayout``
    * header + body (+ optional footer)  -> ``HeaderFooterLayout``

    Returns ``None`` (let the ``FlexLayout`` fallback handle it) for anything
    ambiguous — a missed shell renders fine, a wrong one does not. The
    returned slot ids are child **node** ids; the walker remaps them to region
    ids.
    """
    bb = node.get("absoluteBoundingBox")
    if not isinstance(bb, dict):
        return None
    try:
        parent_bbox = (
            float(bb["x"]),
            float(bb["y"]),
            float(bb["width"]),
            float(bb["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if parent_bbox[2] < _SHELL_PAGE_MIN_W or parent_bbox[3] < _SHELL_PAGE_MIN_H:
        return None

    typed: list[tuple[str, tuple[float, float, float, float], float]] = []
    for child in children:
        cid = str(child.get("id", ""))
        cb = child.get("absoluteBoundingBox")
        if not cid or not isinstance(cb, dict):
            continue
        try:
            box = (
                float(cb["x"]),
                float(cb["y"]),
                float(cb["width"]),
                float(cb["height"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if box[2] <= 0 or box[3] <= 0:
            continue
        typed.append((cid, box, box[2] * box[3]))
    if len(typed) < 2:
        return None

    header_id: str | None = None
    footer_id: str | None = None
    nav_id: str | None = None
    body_candidates: list[tuple[str, float]] = []
    for cid, box, area in typed:
        kind = _classify_shell_child(parent_bbox, box)
        if kind == "header" and header_id is None:
            header_id = cid
        elif kind == "footer" and footer_id is None:
            footer_id = cid
        elif kind == "leftNav" and nav_id is None:
            nav_id = cid
        else:
            body_candidates.append((cid, area))
    if not body_candidates:
        return None
    body_id = max(body_candidates, key=lambda t: t[1])[0]

    notes: list[str] = []
    if nav_id is not None and header_id is not None:
        return PrismPageShell(
            component="MainPageLayout",
            slots={"header": header_id, "leftPanel": nav_id, "body": body_id},
            confidence=0.75,
            notes=["header + left-nav + body geometry"],
        )
    if nav_id is not None:
        return PrismPageShell(
            component="LeftNavLayout",
            slots={"leftPanel": nav_id, "rightBodyContent": body_id},
            confidence=0.7,
            notes=["left-nav + body geometry"],
        )
    if header_id is not None:
        slots = {"header": header_id, "bodyContent": body_id}
        if footer_id is not None:
            slots["footer"] = footer_id
            notes.append("header + body + footer geometry")
        else:
            notes.append("header + body geometry")
        return PrismPageShell(
            component="HeaderFooterLayout",
            slots=slots,
            confidence=0.7,
            notes=notes,
        )
    return None
