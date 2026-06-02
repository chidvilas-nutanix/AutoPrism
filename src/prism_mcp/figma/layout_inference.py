"""Deterministic Figma bbox → flex / grid / stack inference.

This module answers the question *"given a parent node and its
immediate visible children, what CSS-aligned layout should the
generator emit?"*. Two trust sources:

* **Auto-layout fast path.** When ``parent["layoutMode"]`` is
  ``HORIZONTAL`` / ``VERTICAL`` / ``GRID`` we trust Figma's own
  ``itemSpacing`` / ``primaryAxisAlignItems`` /
  ``counterAxisAlignItems`` / ``counterAxisSpacing`` /
  ``layoutPositioning`` fields and emit
  :attr:`LayoutAnalysis.confidence` = 1.0 with
  :attr:`LayoutAnalysis.rationale` = ``"figma_auto_layout"``. The
  REST API only populates these fields for auto-layout FRAMEs (per
  https://developers.figma.com/docs/rest-api/file-node-types/), so
  every other case must fall back to geometry.

* **Absolute-positioned fallback.** Pure bbox math, mirroring the
  algorithm documented at
  https://github.com/1yhy/Figma-Context-MCP/blob/main/docs/en/layout-detection.md:

  1. Pairwise IoU using ``min(area_a, area_b)`` as the denominator
     (NOT classic ``union`` — ``min`` correctly treats a badge
     overlapping its host as "the badge is on top"). Threshold 0.1.
     Smaller-area child joins :attr:`absolute_children`, larger
     stays in :attr:`flow_children`.
  2. Score row vs column on the remaining flow children:
     ``score = 0.7 * distribution_score + 0.3 * alignment_score``.
     ``distribution_score`` is the fraction of consecutive pairs
     with a positive gap in the [0, 50] px range after sorting by
     the main-axis start. ``alignment_score`` is the largest
     fraction of children clustered within 2 px on the cross axis.
     Winner must exceed 0.4; otherwise the parent is a ``"stack"``
     and every flow child moves into :attr:`absolute_children`.
  3. Gap analysis. Mean + standard deviation over the consecutive
     pair gaps; ``gap_consistent = std <= 0.2 * mean``. Round to
     the nearest 4-px grid when consistent; emit ``gap=None`` when
     not.
  4. Align / justify scoring on the chosen flow:
     :attr:`align_items` from cross-axis clustering,
     :attr:`justify_content` from the main-axis whitespace before /
     after / between siblings relative to the parent's bbox.

The module is pure-function: no I/O, no logging, no mutation. The
caller (``prism_mcp.figma.walker``) decides where to attach the
returned :class:`LayoutAnalysis` and per-child :class:`AbsolutePos`
values. See ``docs/figma-page-to-prism-plan.md`` §4.6.2.
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma.models import AbsolutePos, LayoutAnalysis

# --------------------------------------------------------------------------
# Translation tables: Figma auto-layout enums → CSS-aligned strings.
#
# Source: Figma REST API node-types reference. ``primaryAxisAlignItems``
# accepts MIN / CENTER / MAX / SPACE_BETWEEN (and SPACE_AROUND /
# SPACE_EVENLY in grid mode only); ``counterAxisAlignItems`` accepts
# MIN / CENTER / MAX / BASELINE. Anything outside these maps falls
# through to ``None`` so the rationale can flag the unknown value
# for debugging — it never silently drops a real signal.
# --------------------------------------------------------------------------


_LAYOUT_MODE_TO_DIRECTION: dict[str, str] = {
    "HORIZONTAL": "row",
    "VERTICAL": "column",
    "GRID": "grid",
}

_PRIMARY_AXIS_TO_JUSTIFY: dict[str, str] = {
    "MIN": "start",
    "CENTER": "center",
    "MAX": "end",
    "SPACE_BETWEEN": "space-between",
    "SPACE_AROUND": "space-around",
    "SPACE_EVENLY": "space-evenly",
}

_COUNTER_AXIS_TO_ALIGN: dict[str, str] = {
    "MIN": "start",
    "CENTER": "center",
    "MAX": "end",
    "BASELINE": "baseline",
}


# --------------------------------------------------------------------------
# Tuned defaults — every constant traces to Figma-Context-MCP's
# layout-detection guide.
# --------------------------------------------------------------------------


_OVERLAP_IOU_THRESHOLD = 0.1
"""IoU above which siblings are considered overlapping.

Computed as ``intersection_area / min(area_a, area_b)`` (NOT
``union``). The min-area denominator is what Figma-Context-MCP uses
because it correctly handles the badge-over-host case: a 16×16 badge
covering 20% of a 100×100 button has IoU 0.16 by min-area but only
0.026 by union — and we want to flag this as an overlap so the
badge becomes ``position: absolute``."""


_ALIGNMENT_TOLERANCE = 2.0
"""Pixel tolerance for "same top" / "same left" clustering.

Sub-pixel jitter is common in Figma exports because the renderer
uses fractional coordinates; 2 px is well below human perception and
matches the value Figma-Context-MCP and Locofy both ship."""


_GAP_MIN = 0.0
_GAP_MAX = 50.0
"""Acceptable per-pair gap range when scoring row/column flow.

Beyond 50 px the gap is more likely a layout break (two clusters of
elements separated by a divider) than a uniform flex gap. The 0-50
range is what Figma-Context-MCP uses for its
``calculateRowScore`` / ``calculateColumnScore``."""


_GAP_GRID = 4
"""Snap detected gaps to the nearest multiple of this many px.

Designers work on 4-px or 8-px grids; rounding to 4 keeps the
output stable across re-exports without losing meaningful
distinctions (a 6 px gap rounds to 8, not 4 — borderline cases
are explicit)."""


_DISTRIBUTION_WEIGHT = 0.7
_ALIGNMENT_WEIGHT = 0.3
"""Per Figma-Context-MCP's ``analyzeLayoutDirection`` formula:

    score = distribution * 0.7 + alignment * 0.3

Distribution dominates because evenly-spaced children are the
single strongest signal of flex flow. Alignment is the tie-breaker
that distinguishes a row from a stack of identically-tall items."""


_WINNER_THRESHOLD = 0.4
"""Minimum combined score to commit to a row/column decision.

Below this we collapse to ``direction="stack"`` rather than guess.
Figma-Context-MCP uses the same threshold and reports that lowering
it produces frequent misclassifications on dense layouts where two
weakly-correlated axes both score around 0.3-0.35."""


_GAP_CONSISTENT_RATIO = 0.2
"""``gap_consistent`` iff ``std_dev <= 0.2 * mean``.

20% coefficient of variation is the boundary Figma-Context-MCP
documents between "uniform spacing — emit a single gap" and "per-
child margins — emit gap=None and let the generator place each
child explicitly"."""


_STRETCH_HEIGHT_RANGE = 10.0
"""Minimum cross-axis size variation (in px) before we infer
``align_items="stretch"`` on a row/column flow.

Designers don't bother making children differ by a pixel or two for
visual reasons; a > 10 px spread means the children are genuinely
filling different cross-axis extents and the generator should
stretch rather than centre."""


# --------------------------------------------------------------------------
# Public entrypoint.
# --------------------------------------------------------------------------


def analyze_layout(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
) -> LayoutAnalysis:
    """Infer a :class:`LayoutAnalysis` for ``parent``'s children.

    Pure function. No side effects, deterministic given the inputs.

    Args:
        parent (dict[str, Any]): the Figma node JSON dict for the
            parent. Reads ``layoutMode``, ``itemSpacing``,
            ``primaryAxisAlignItems``, ``counterAxisAlignItems``,
            ``counterAxisSpacing``, ``counterAxisSizingMode``, and
            ``absoluteBoundingBox``.
        children (list[dict[str, Any]]): the immediate child node
            JSON dicts that the walker considered "significant"
            (survived the noise filter). The walker's
            ``significant_children`` list is the canonical input.

    Returns:
        LayoutAnalysis: the layout decision for this parent.
        ``direction`` is ``None`` when the parent has zero
        children, ``"single"`` when it has exactly one, otherwise
        one of ``"row"`` / ``"column"`` / ``"grid"`` / ``"stack"``.
        See :class:`LayoutAnalysis` for the full field reference.
    """
    layout_mode = parent.get("layoutMode")
    if isinstance(layout_mode, str) and layout_mode in _LAYOUT_MODE_TO_DIRECTION:
        return _from_auto_layout(parent, children, layout_mode)

    typed = _typed_children(children)
    n = len(typed)
    if n == 0:
        return LayoutAnalysis(
            direction=None,
            rationale="no_children_with_geometry",
        )
    if n == 1:
        return LayoutAnalysis(
            direction="single",
            flow_children=[typed[0][0]],
            confidence=1.0,
            rationale="single_child",
        )

    absolute_ids = _detect_overlaps(typed)
    flow = [tc for tc in typed if tc[0] not in absolute_ids]

    if len(flow) == 0:
        return LayoutAnalysis(
            direction="stack",
            absolute_children=sorted(absolute_ids),
            flow_children=[],
            confidence=1.0,
            rationale=f"all_{len(absolute_ids)}_children_overlap",
        )
    if len(flow) == 1:
        return LayoutAnalysis(
            direction="single",
            flow_children=[flow[0][0]],
            absolute_children=sorted(absolute_ids),
            confidence=1.0,
            rationale=(
                "one_flow_child_after_overlap"
                if absolute_ids
                else "single_child"
            ),
        )

    row_combined, row_dist, row_align, row_gaps = _score_direction(
        flow, axis="row"
    )
    col_combined, col_dist, col_align, col_gaps = _score_direction(
        flow, axis="column"
    )

    winner = max(row_combined, col_combined)
    if winner < _WINNER_THRESHOLD:
        return LayoutAnalysis(
            direction="stack",
            absolute_children=sorted(absolute_ids | {tc[0] for tc in flow}),
            flow_children=[],
            confidence=winner,
            rationale=(
                f"weak_direction row={row_combined:.2f} "
                f"col={col_combined:.2f} threshold={_WINNER_THRESHOLD}"
            ),
        )

    if row_combined >= col_combined:
        direction = "row"
        dist_score = row_dist
        align_score = row_align
        gaps = row_gaps
        flow_sorted = sorted(flow, key=lambda c: c[1][0])
    else:
        direction = "column"
        dist_score = col_dist
        align_score = col_align
        gaps = col_gaps
        flow_sorted = sorted(flow, key=lambda c: c[1][1])

    gap_value, gap_consistent = _compute_gap(gaps)
    parent_bbox = _bbox(parent)
    align_items = _align_items(flow_sorted, direction)
    justify_content = _justify_content(
        flow_sorted, direction, parent_bbox, gap_consistent
    )

    rationale = (
        f"{direction} score={winner:.2f} "
        f"(distribution={dist_score:.2f}, alignment={align_score:.2f}, "
        f"{len(flow_sorted)} children"
    )
    if not gap_consistent:
        rationale += ", gap inconsistent — use per-child margins"
    rationale += ")"

    return LayoutAnalysis(
        direction=direction,
        justify_content=justify_content,
        align_items=align_items,
        gap=gap_value,
        gap_consistent=gap_consistent,
        confidence=winner,
        absolute_children=sorted(absolute_ids),
        flow_children=[tc[0] for tc in flow_sorted],
        rationale=rationale,
    )


def compute_absolute_pos(
    parent: dict[str, Any],
    child: dict[str, Any],
    z_order: int,
) -> AbsolutePos | None:
    """Build an :class:`AbsolutePos` for ``child`` relative to
    ``parent``'s bbox origin.

    Returns ``None`` when either bbox is missing — the caller should
    skip attaching :attr:`MappedRegion.absolute_pos` in that case
    rather than carry zeros that look like real coordinates.

    The ``z_order`` is supplied by the caller (typically the walker
    after sorting overlapping siblings by area descending — largest
    first / bottom, smallest last / top). Keeping the policy at the
    caller leaves room for future overrides without changing this
    function's contract.
    """
    parent_bbox = _bbox(parent)
    child_bbox = _bbox(child)
    if parent_bbox is None or child_bbox is None:
        return None
    top = max(0.0, child_bbox[1] - parent_bbox[1])
    left = max(0.0, child_bbox[0] - parent_bbox[0])
    return AbsolutePos(
        top=round(top, 2),
        left=round(left, 2),
        width=round(child_bbox[2], 2),
        height=round(child_bbox[3], 2),
        z_order=z_order,
    )


# --------------------------------------------------------------------------
# Auto-layout fast path.
# --------------------------------------------------------------------------


def _from_auto_layout(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    layout_mode: str,
) -> LayoutAnalysis:
    """Build a :class:`LayoutAnalysis` directly from Figma's own
    auto-layout fields.

    Per the REST API node-types reference, ``itemSpacing`` /
    ``primaryAxisAlignItems`` / ``counterAxisAlignItems`` /
    ``counterAxisSpacing`` are only populated when ``layoutMode`` is
    HORIZONTAL / VERTICAL / GRID. Children with
    ``layoutPositioning: "ABSOLUTE"`` escape the auto-layout flow
    and join :attr:`absolute_children`.
    """
    direction = _LAYOUT_MODE_TO_DIRECTION[layout_mode]

    flow_ids: list[str] = []
    abs_ids: list[str] = []
    for c in children:
        cid = str(c.get("id", ""))
        if not cid:
            continue
        if c.get("layoutPositioning") == "ABSOLUTE":
            abs_ids.append(cid)
        else:
            flow_ids.append(cid)

    primary_raw = parent.get("primaryAxisAlignItems")
    justify_content = (
        _PRIMARY_AXIS_TO_JUSTIFY.get(primary_raw)
        if isinstance(primary_raw, str)
        else None
    )

    counter_raw = parent.get("counterAxisAlignItems")
    align_items: str | None = None
    if isinstance(counter_raw, str):
        align_items = _COUNTER_AXIS_TO_ALIGN.get(counter_raw)
    if align_items is None and parent.get("counterAxisSizingMode") == "AUTO":
        # When the counter-axis sizing mode is AUTO and Figma did
        # not specify an explicit alignment, children fill the
        # cross axis — i.e. ``stretch`` in CSS terms.
        align_items = "stretch"

    gap: float | None = None
    primary_gap = _as_float(parent.get("itemSpacing"))
    if direction == "grid":
        counter_gap = _as_float(parent.get("counterAxisSpacing"))
        if primary_gap is not None and counter_gap is not None:
            gap = (
                primary_gap
                if abs(primary_gap - counter_gap) < 0.5
                else None
            )
        else:
            gap = primary_gap if primary_gap is not None else counter_gap
    elif primary_gap is not None:
        gap = primary_gap

    rationale_parts = ["figma_auto_layout"]
    if isinstance(primary_raw, str) and justify_content is None:
        rationale_parts.append(f"unmapped_primary={primary_raw}")
    if (
        isinstance(counter_raw, str)
        and align_items is None
        and counter_raw not in _COUNTER_AXIS_TO_ALIGN
    ):
        rationale_parts.append(f"unmapped_counter={counter_raw}")
    rationale = " ".join(rationale_parts)

    return LayoutAnalysis(
        direction=direction,
        justify_content=justify_content,
        align_items=align_items,
        gap=gap,
        gap_consistent=True,
        confidence=1.0,
        absolute_children=abs_ids,
        flow_children=flow_ids,
        rationale=rationale,
    )


# --------------------------------------------------------------------------
# Geometry helpers.
# --------------------------------------------------------------------------


def _bbox(
    node: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    """Return the ``(x, y, w, h)`` tuple, or ``None`` when missing.

    Centralised so the inference algorithm never has to scatter
    defensive ``.get("absoluteBoundingBox") or {}`` patterns.
    """
    bb = node.get("absoluteBoundingBox")
    if not isinstance(bb, dict):
        return None
    try:
        return (
            float(bb["x"]),
            float(bb["y"]),
            float(bb["width"]),
            float(bb["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _typed_children(
    children: list[dict[str, Any]],
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Filter to children that have an id AND a positive-area bbox.

    Returns ``(id, bbox)`` pairs in the input order. Children
    without geometry can't participate in any spatial decision; they
    are dropped from inference and not surfaced in either
    ``flow_children`` or ``absolute_children`` so downstream
    consumers don't have to special-case them.
    """
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for c in children:
        cid = str(c.get("id", ""))
        if not cid:
            continue
        bb = _bbox(c)
        if bb is None or bb[2] <= 0 or bb[3] <= 0:
            continue
        out.append((cid, bb))
    return out


def _area(bbox: tuple[float, float, float, float]) -> float:
    return bbox[2] * bbox[3]


def _intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Return the area of the rectangular intersection of ``a`` and
    ``b`` in absolute coordinates. ``0.0`` when disjoint."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2 = ax1 + aw
    ay2 = ay1 + ah
    bx2 = bx1 + bw
    by2 = by1 + bh
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    return ix * iy


def _as_float(value: Any) -> float | None:
    """Best-effort numeric coercion. ``None`` on any failure or for
    a value that isn't ``int``/``float`` to start with."""
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------
# Overlap detection (the IoU pass).
# --------------------------------------------------------------------------


def _detect_overlaps(
    typed: list[tuple[str, tuple[float, float, float, float]]],
) -> set[str]:
    """Return the set of ids that should render as
    ``position: absolute`` because they overlap a sibling.

    Pairwise IoU with ``min(area_a, area_b)`` denominator and
    :data:`_OVERLAP_IOU_THRESHOLD`. The smaller-area sibling joins
    the absolute set on every overlap pair; the larger stays in
    flow. When two roughly-equal siblings overlap (rare in practice
    — only one ever joins the absolute set), the smaller one wins
    deterministically (Python's stable sort keeps the input order
    as the tie-breaker).
    """
    absolute_ids: set[str] = set()
    for i, (id_a, bb_a) in enumerate(typed):
        area_a = _area(bb_a)
        for id_b, bb_b in typed[i + 1 :]:
            area_b = _area(bb_b)
            min_area = min(area_a, area_b)
            if min_area <= 0:
                continue
            iou = _intersection_area(bb_a, bb_b) / min_area
            if iou > _OVERLAP_IOU_THRESHOLD:
                if area_a <= area_b:
                    absolute_ids.add(id_a)
                else:
                    absolute_ids.add(id_b)
    return absolute_ids


# --------------------------------------------------------------------------
# Direction scoring (row vs column).
# --------------------------------------------------------------------------


def _score_direction(
    flow: list[tuple[str, tuple[float, float, float, float]]],
    *,
    axis: str,
) -> tuple[float, float, float, list[float]]:
    """Return ``(combined, distribution, alignment, gaps)`` for the
    given axis.

    For ``axis="row"`` the children are sorted by left edge and the
    main-axis gap is computed as ``next.left - current.right``;
    cross-axis alignment is over top edges. For ``axis="column"``
    the analogue with top edges and left edges swapped.
    """
    if axis == "row":
        ordered = sorted(flow, key=lambda c: c[1][0])
        starts = [c[1][0] for c in ordered]
        ends = [c[1][0] + c[1][2] for c in ordered]
        cross_starts = [c[1][1] for c in ordered]
    elif axis == "column":
        ordered = sorted(flow, key=lambda c: c[1][1])
        starts = [c[1][1] for c in ordered]
        ends = [c[1][1] + c[1][3] for c in ordered]
        cross_starts = [c[1][0] for c in ordered]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown axis {axis!r}")

    n = len(ordered)
    pair_count = max(1, n - 1)
    gaps: list[float] = []
    positive_in_range = 0
    for i in range(n - 1):
        gap = starts[i + 1] - ends[i]
        gaps.append(gap)
        if _GAP_MIN <= gap <= _GAP_MAX:
            positive_in_range += 1
    distribution = positive_in_range / pair_count

    alignment = _cluster_fraction(cross_starts)
    combined = _DISTRIBUTION_WEIGHT * distribution + _ALIGNMENT_WEIGHT * alignment
    return combined, distribution, alignment, gaps


def _cluster_fraction(values: list[float]) -> float:
    """Return the largest fraction of ``values`` whose pairwise
    distance to a common anchor is within :data:`_ALIGNMENT_TOLERANCE`.

    Equivalent to the fraction of children in the densest cluster on
    the cross axis. O(n²) but ``n`` is bounded by the number of
    immediate children (rarely > 30), so the cost is negligible.
    """
    n = len(values)
    if n == 0:
        return 0.0
    best = 0
    for anchor in values:
        count = sum(
            1 for v in values if abs(v - anchor) <= _ALIGNMENT_TOLERANCE
        )
        if count > best:
            best = count
    return best / n


# --------------------------------------------------------------------------
# Gap analysis.
# --------------------------------------------------------------------------


def _compute_gap(gaps: list[float]) -> tuple[float | None, bool]:
    """Return ``(gap, gap_consistent)`` for a list of per-pair gaps.

    Drops negative gaps (caused by overlapping siblings that
    survived the IoU pass with a tiny overlap below threshold), then:

    * 0 positive gaps -> ``(None, True)`` — nothing to report.
    * 1 positive gap -> snap to the 4-px grid; consistent.
    * 2+ positive gaps -> use mean if ``std / mean <= 0.2``; else
      emit ``None`` and flag inconsistent so the caller can
      surface per-child margins instead.
    """
    positive = [g for g in gaps if g >= 0]
    if not positive:
        return None, True
    mean = sum(positive) / len(positive)
    if mean < 0.5:
        return 0.0, True
    if len(positive) == 1:
        return float(_snap(mean)), True
    variance = sum((g - mean) ** 2 for g in positive) / len(positive)
    std = variance**0.5
    if std <= _GAP_CONSISTENT_RATIO * mean:
        return float(_snap(mean)), True
    return None, False


def _snap(value: float) -> int:
    """Round to the nearest multiple of :data:`_GAP_GRID` px."""
    return int(round(value / _GAP_GRID) * _GAP_GRID)


# --------------------------------------------------------------------------
# Cross-axis (align-items) + main-axis (justify-content) scoring.
# --------------------------------------------------------------------------


def _align_items(
    flow_sorted: list[tuple[str, tuple[float, float, float, float]]],
    direction: str,
) -> str | None:
    """Decide a CSS ``align-items`` value from cross-axis geometry.

    Row direction means the cross axis is vertical (tops, bottoms,
    heights); column direction means horizontal. The function looks
    for the strongest of four signals — same start, same end, same
    centerline, or strongly-varying extents (stretch) — and returns
    the first that matches. ``None`` when none of them apply.
    """
    if not flow_sorted:
        return None
    if direction == "row":
        starts = [c[1][1] for c in flow_sorted]
        sizes = [c[1][3] for c in flow_sorted]
    else:
        starts = [c[1][0] for c in flow_sorted]
        sizes = [c[1][2] for c in flow_sorted]
    ends = [s + sz for s, sz in zip(starts, sizes, strict=True)]
    centers = [(s + e) / 2 for s, e in zip(starts, ends, strict=True)]

    if _all_within(starts, _ALIGNMENT_TOLERANCE):
        if (max(sizes) - min(sizes)) > _STRETCH_HEIGHT_RANGE:
            return "stretch"
        return "start"
    if _all_within(ends, _ALIGNMENT_TOLERANCE):
        return "end"
    if _all_within(centers, _ALIGNMENT_TOLERANCE):
        return "center"
    return None


def _justify_content(
    flow_sorted: list[tuple[str, tuple[float, float, float, float]]],
    direction: str,
    parent_bbox: tuple[float, float, float, float] | None,
    gap_consistent: bool,
) -> str | None:
    """Decide a CSS ``justify-content`` value from main-axis geometry.

    Needs the parent's bbox so it can measure the whitespace before
    the first child and after the last child. Returns ``None`` when
    the parent has no bbox or the flow is empty.

    Rules, in order:

    * Flush start AND flush end AND ``gap_consistent`` with ≥ 2
      children -> ``"space-between"``.
    * Flush start (and not the above) -> ``"start"``.
    * Flush end -> ``"end"``.
    * Balanced whitespace on both sides -> ``"center"``.
    * Otherwise no decision (``None``).
    """
    if not flow_sorted or parent_bbox is None:
        return None
    if direction == "row":
        first_start = flow_sorted[0][1][0]
        last_end = flow_sorted[-1][1][0] + flow_sorted[-1][1][2]
        parent_start = parent_bbox[0]
        parent_end = parent_bbox[0] + parent_bbox[2]
    else:
        first_start = flow_sorted[0][1][1]
        last_end = flow_sorted[-1][1][1] + flow_sorted[-1][1][3]
        parent_start = parent_bbox[1]
        parent_end = parent_bbox[1] + parent_bbox[3]

    space_before = first_start - parent_start
    space_after = parent_end - last_end
    tol = _ALIGNMENT_TOLERANCE
    flush_start = abs(space_before) <= tol
    flush_end = abs(space_after) <= tol

    if flush_start and flush_end and gap_consistent and len(flow_sorted) >= 2:
        return "space-between"
    if flush_start and not flush_end:
        return "start"
    if flush_end and not flush_start:
        return "end"
    if abs(space_before - space_after) <= tol:
        return "center"
    return None


def _all_within(values: list[float], tol: float) -> bool:
    """True iff ``max - min`` of ``values`` is within ``tol`` px."""
    if not values:
        return True
    return (max(values) - min(values)) <= tol
