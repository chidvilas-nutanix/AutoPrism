"""Deterministic Figma → Prism tree walker.

Phase 3+4+6 revision: integrates the noise filter (Phase 2), the
routing layer (Phase 3), the pattern detectors (Phase 4), pass-7
text capture, and the per-region :func:`map_figma_node` call (Phase
6) into a single DFS.

The walker is split into:

* :func:`walk_tree` — public entrypoint. Pure function.
* :class:`_WalkContext` — shared mutable state.
* :func:`_visit` — recursive DFS body. Returns the survivor's id
  (or ``None`` if dropped) plus the surviving sub-tree shape.

See ``docs/figma-page-to-prism-plan.md`` §4 for the algorithm.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeAlias

from prism_mcp.figma.filter import (
    DropReason,
    pass_1_visible,
    pass_2_invisible_decoration,
    pass_3_mappable_type,
    pass_4_collapse_passthrough,
    pass_6_tiny_decorative,
)
from prism_mcp.figma.layout_inference import (
    analyze_layout,
    compute_absolute_pos,
)
from prism_mcp.figma.models import (
    BoxStyle,
    DroppedNode,
    FigmaTreeMapping,
    LayoutNode,
    MappedRegion,
)
from prism_mcp.figma.patterns import (
    PAGE_SCALE_MIN_EDGE,
    PATTERNS,
    PATTERNS_LEAF_SCALE,
    PatternMatch,
)
from prism_mcp.figma.routing import (
    FrameRole,
    RouterDecision,
    classify_frame_role,
    route_node,
)
from prism_mcp.figma.types import MAPPABLE_TYPES
from prism_mcp.figma.utils import (
    bbox_tuple_from_dict,
    extract_box_style,
    extract_visible_hexes,
    get_characters,
    iter_children,
    shape_bucket,
)
from prism_mcp.workflow.figma_mapping import FigmaNodeMapping

logger = logging.getLogger(__name__)


class WalkerError(RuntimeError):
    """Raised on safety-rail trips (``max_depth`` / ``max_nodes``).

    The walker bails fast on these rather than silently producing
    truncated output. See design doc §4.7.
    """


_PATTERN_ABSORB_MAX_RATIO = 0.5
"""Reject a pattern match that would absorb more than this fraction
of the total input tree.

Shape-only pattern detectors can over-match catastrophically when the
heuristic accidentally lines up at page scale — a single
:func:`prism_mcp.figma.patterns.match_kpi_tile` hit on a 1280×800 root
FRAME would otherwise swallow the entire page into one agenda row.
The 50% ceiling is empirical: a legitimate pattern (a 5-cell table
column, a 4-icon button group) absorbs a tiny fraction of the page,
while runaway matches swallow nearly all of it. The
:attr:`prism_mcp.figma.filter.DropReason.pattern_oversized_reject`
audit entry records the rejected candidate so users can see what would
have happened without the rail.
"""


_PATTERN_ABSORB_MIN_TREE_SIZE = 20
"""Only apply the absorb-ratio rail when the input tree has at least
this many nodes.

Below this threshold the rail's denominator gets noisy — a perfectly
legitimate 4-stripe icon match in a 6-node mini-fixture is 67%
absorbed and would otherwise be falsely rejected. Real-world failures
of the rail always involve hundreds-of-nodes pages."""


_PARALLEL_MAPPING_WORKERS_ENV = "PRISM_MCP_PARALLEL_MAPPING_WORKERS"
"""Env-var name that controls the per-walk mapping-resolver worker count.

* Unset / ``"0"``: auto — ``min(_DEFAULT_PARALLEL_WORKERS,
  max(1, cpu_count // 2))``. Half-the-cores default avoids
  oversubscribing ONNX Runtime's internal intra-op thread pool when
  the dense encoder + cross-encoder reranker fire concurrently from
  multiple workers.
* ``"1"``: explicit serial execution (no ``ThreadPoolExecutor`` is
  spun up; useful for deterministic timing benchmarks and for
  bisecting any parallel-only bug to the parallel layer).
* ``"N"`` (N ≥ 1): exactly N workers, capped at
  :data:`_MAX_PARALLEL_WORKERS`."""


_DEFAULT_PARALLEL_WORKERS = 4
"""Auto-detected ceiling on per-walk mapping workers.

Empirically the dominant cost of one ``map_figma_node`` call is the
ONNX dense encode (≈ 50-100 ms on CPU) plus the optional cross-encoder
rerank (≈ 80 ms per 50 pairs). Both stages release the GIL, so 4
Python threads can overlap them on multi-core machines. Above ~4
workers we run into ONNX's own intra-op contention — past that point
extra parallelism either gives diminishing returns or actively slows
the wall-clock.
"""


_MAX_PARALLEL_WORKERS = 8
"""Hard ceiling on mapping workers regardless of env-var override.

The mapper indices (hybrid searcher, BM25 entity index, composition
graph, color token index, a11y rules) are all read-only and
thread-safe, but ONNX Runtime spins up its own intra-op thread pool
sized to the host CPU count. Allowing > 8 outer workers from this
file would push the host into deep oversubscription on smaller dev
machines. Operators wanting more concurrency can bump
:data:`_MAX_PARALLEL_WORKERS` after measuring; the env-var alone
cannot exceed it.
"""


# The MapFigmaNodeFn signature mirrors the public
# :func:`prism_mcp.workflow.figma_mapping.map_figma_node` so the
# walker can be unit-tested with a stub. The walker takes this as
# a parameter (not an import) to keep the function pure — the MCP
# server passes the real bound function, tests pass a stub.
MapFigmaNodeFn: TypeAlias = Callable[..., FigmaNodeMapping]


# --------------------------------------------------------------------------
# Public entrypoint.
# --------------------------------------------------------------------------


def walk_tree(
    *,
    tree_json: dict[str, Any],
    reference_jsx: str | None = None,
    variable_defs: dict[str, str] | None = None,
    max_depth: int = 20,
    max_nodes: int = 5000,
    max_agenda: int = 50,
    map_figma_node_fn: MapFigmaNodeFn | None = None,
) -> FigmaTreeMapping:
    """Build a :class:`FigmaTreeMapping` for ``tree_json``.

    Args:
        tree_json (dict[str, Any]): the document subtree returned
            by Figma's REST API node-fetch endpoint
            (``response["nodes"][node_id]["document"]``).
        reference_jsx (str | None): React+Tailwind JSX obtained
            from Figma's ``get_design_context``. The walker
            slices it per region by Figma node-id comments and
            forwards each slice to ``map_figma_node_fn``.
        variable_defs (dict[str, str] | None): designer-named
            ``hex → token-name`` map from Figma's
            ``get_variable_defs``. Seeds the ``tokens`` output.
        max_depth (int): hard cap on traversal depth.
        max_nodes (int): hard cap on total nodes visited.
        max_agenda (int): soft cap on agenda rows. When exceeded
            the walker emits a warning; the LLM is expected to
            group sub-regions in the second-pass composition.
        map_figma_node_fn (Callable | None): the bound
            :func:`prism_mcp.workflow.figma_mapping.map_figma_node`
            with library deps already curried in. When
            ``None``, the walker emits :class:`MappedRegion`
            rows with empty :class:`FigmaNodeMapping` placeholders
            — useful for filter / pattern unit tests that don't
            want to stand up a real library index.

    Returns:
        FigmaTreeMapping: full structured output.

    Raises:
        WalkerError: when ``max_depth`` or ``max_nodes`` is
            exceeded.
    """
    if not isinstance(tree_json, dict):
        logger.warning(
            "walk_tree received non-dict tree_json type=%s; returning empty",
            type(tree_json).__name__,
        )
        return _empty_mapping()

    ctx = _WalkContext(
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_agenda=max_agenda,
        variable_defs=variable_defs or {},
        reference_jsx=reference_jsx,
        map_figma_node_fn=map_figma_node_fn,
    )
    ctx.input_nodes = 1 + _count_nodes(tree_json)

    _visit(tree_json, ctx, depth=0, parent_chain=[])

    # ---- Phase 2: drain the queued :func:`map_figma_node` calls.
    # The DFS only built placeholder mappings + queued the inputs.
    # The actual fan-out (BM25 + hybrid + graph + a11y + tokens)
    # runs here, optionally in parallel, with dedup across regions
    # that share byte-identical inputs.
    _resolve_pending_mappings(ctx)

    # Trim agenda when it exceeds the hard cap. Fix C: the cap used
    # to be soft (warning only, no truncation), which let the X-Ray
    # Master File leak 297 / 632 regions into the LLM's input. The
    # hard cap protects against future emission bugs by guaranteeing
    # the LLM never receives more than ``max_agenda`` rows.
    # Importance ranking keeps the rows most likely to anchor the
    # page (page-scale FRAMEs, pattern regions, regions with text
    # captures); the lowest scorers are moved to ``ctx.dropped``
    # with reason :attr:`DropReason.agenda_truncated`. See
    # ``docs/x-ray-walker-investigation.md`` §8 "Fix C".
    if len(ctx.agenda) > ctx.max_agenda:
        _truncate_agenda_to_max(ctx)

    drop_histogram = Counter(d.reason for d in ctx.dropped)
    summary: dict[str, int] = {
        "input_nodes": ctx.input_nodes,
        "kept_for_mapping": len(ctx.agenda),
        "dropped_total": len(ctx.dropped),
        "agenda_size": len(ctx.agenda),
        "tokens_count": len(ctx.tokens),
        "warnings_count": len(ctx.warnings),
        "max_depth": ctx.max_depth,
        "max_agenda": ctx.max_agenda,
    }
    summary.update(
        {f"dropped_{reason}": count for reason, count in drop_histogram.items()}
    )

    logger.info(
        "walk_tree done input=%d kept=%d dropped=%d tokens=%d warnings=%d",
        summary["input_nodes"],
        summary["kept_for_mapping"],
        summary["dropped_total"],
        summary["tokens_count"],
        summary["warnings_count"],
    )

    return FigmaTreeMapping(
        layout_tree=ctx.layout_tree,
        agenda=ctx.agenda,
        tokens=ctx.tokens,
        dropped=ctx.dropped,
        summary=summary,
        warnings=ctx.warnings,
    )


# --------------------------------------------------------------------------
# Walking context. Kept private — mutated during the DFS to avoid
# threading a half-dozen accumulators through every recursive call.
# --------------------------------------------------------------------------


class _WalkContext:
    """Shared mutable state for a single :func:`walk_tree` invocation.

    Notes on threading:
        The DFS body (:func:`_visit`) runs strictly single-threaded.
        Mutations of :attr:`agenda` / :attr:`dropped` /
        :attr:`tokens` / :attr:`warnings` / :attr:`layout_tree` /
        :attr:`_jsx_slice_cache` all happen on that single thread.

        Parallelism is bolted on **after** the DFS completes via
        :func:`_resolve_pending_mappings`, which dispatches the
        deferred :func:`map_figma_node` calls to a
        :class:`ThreadPoolExecutor`. The mapper is read-only against
        the shared Prism indices (BM25 / hybrid / graph / tokens /
        a11y), so concurrent invocations are safe by construction —
        no shared mutable state is touched between threads.
    """

    __slots__ = (
        "_jsx_slice_cache",
        "agenda",
        "dropped",
        "input_nodes",
        "layout_tree",
        "map_figma_node_fn",
        "mapping_jobs",
        "max_agenda",
        "max_depth",
        "max_nodes",
        "reference_jsx",
        "tokens",
        "variable_defs",
        "visited",
        "warnings",
    )

    def __init__(
        self,
        *,
        max_depth: int,
        max_nodes: int,
        max_agenda: int,
        variable_defs: dict[str, str],
        reference_jsx: str | None,
        map_figma_node_fn: MapFigmaNodeFn | None,
    ) -> None:
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_agenda = max_agenda
        self.variable_defs = variable_defs
        self.reference_jsx = reference_jsx
        self.map_figma_node_fn = map_figma_node_fn
        self.input_nodes = 0
        self.visited = 0
        self.layout_tree: list[LayoutNode] = []
        self.agenda: list[MappedRegion] = []
        self.tokens: dict[str, str] = dict(variable_defs)
        self.dropped: list[DroppedNode] = []
        self.warnings: list[str] = []
        self._jsx_slice_cache: dict[str, str] = {}
        # Deferred mapper invocations: each tuple is
        # ``(region, kwargs, cache_key)``. ``region`` is the
        # already-appended :class:`MappedRegion` whose
        # ``.mapping`` field will be overwritten in place once
        # the queued :func:`map_figma_node` call resolves.
        # ``cache_key`` enables dedup so two regions with
        # byte-identical inputs share a single mapper call.
        # Populated only when :attr:`map_figma_node_fn` is set;
        # the stub path (tests with ``map_figma_node_fn=None``)
        # keeps the list empty.
        self.mapping_jobs: list[
            tuple[MappedRegion, dict[str, Any], tuple]
        ] = []

    def drop(
        self,
        node: dict[str, Any],
        reason: DropReason | str,
        detail: str = "",
    ) -> None:
        """Append ``node`` to the audit trail."""
        self.dropped.append(
            DroppedNode(
                id=str(node.get("id", "")),
                name=str(node.get("name", "")),
                type=str(node.get("type", "")),
                reason=str(reason),
                detail=detail,
            )
        )

    def slice_reference_jsx(self, node_id: str) -> str | None:
        """Return the chunk of ``reference_jsx`` annotated with
        this Figma node id, if any.

        Figma's ``get_design_context`` emits comments like
        ``{/* figma-node 626:987 */}`` around each region. We
        accept that exact pattern plus the dash-form
        (``626-987``) the URL uses, since some plugin versions
        emit either.
        """
        if not self.reference_jsx:
            return None
        if node_id in self._jsx_slice_cache:
            return self._jsx_slice_cache[node_id] or None
        normalized_id = node_id.replace(":", "-")
        pattern = re.compile(
            r"\{\s*/\*\s*figma-node\s+("
            + re.escape(node_id)
            + r"|"
            + re.escape(normalized_id)
            + r")\s*\*/\s*\}"
            r"(.*?)"
            r"\{\s*/\*\s*/figma-node\s+("
            + re.escape(node_id)
            + r"|"
            + re.escape(normalized_id)
            + r")\s*\*/\s*\}",
            flags=re.DOTALL,
        )
        match = pattern.search(self.reference_jsx)
        sliced = match.group(2).strip() if match else ""
        self._jsx_slice_cache[node_id] = sliced
        return sliced or None


def _empty_mapping() -> FigmaTreeMapping:
    """Return a mapping with a zeroed summary block.

    Centralised so the *type* signature is uniform between the
    happy and degenerate paths."""
    return FigmaTreeMapping(
        summary={
            "input_nodes": 0,
            "kept_for_mapping": 0,
            "dropped_total": 0,
            "agenda_size": 0,
            "tokens_count": 0,
            "warnings_count": 0,
        }
    )


def _count_nodes(node: dict[str, Any]) -> int:
    """Iterative count of all descendant nodes (excluding ``node``)."""
    count = 0
    stack: list[dict[str, Any]] = list(iter_children(node))
    while stack:
        cur = stack.pop()
        count += 1
        stack.extend(iter_children(cur))
    return count


def _agenda_importance(region: MappedRegion) -> tuple[int, float, int, int]:
    """Score a :class:`MappedRegion` for the Fix-C truncation pass.

    Higher tuples win. The score is intentionally simple — the goal
    is "keep the rows the LLM most needs to anchor the page" while
    being robust to the per-page variation in role labels:

    * ``-parent_chain_depth`` — shallower regions sit closer to the
      page root and are usually structural anchors (top-bar,
      sidebar, page container). Negated because tuple comparison is
      max-wins and shallower depth is preferred.
    * ``bbox_area`` — larger regions cover more of the visual page
      and are harder to reconstruct from siblings; preserved
      preferentially. ``None`` bboxes score 0.0 (still better than
      a tiny one).
    * ``has_text_slot`` — regions that captured TEXT content
      already discharged their content-slot duty, so they're
      preferred over near-empty leaves.
    * ``is_pattern_region`` — regions emitted by a pattern matcher
      (table-column / tab-strip / icon / etc.) are by definition
      load-bearing structural matches; the walker invested explicit
      heuristic work to identify them.

    The tuple is stable under :func:`sorted`; ties keep the
    DFS-emission order, which is reading order on the canvas.
    """
    chain_depth = len(region.parent_chain)
    bbox = region.bbox
    if bbox is not None and len(bbox) >= 4:
        bbox_area = max(0.0, float(bbox[2])) * max(0.0, float(bbox[3]))
    else:
        bbox_area = 0.0
    has_text_slot = 1 if region.content_slots.get("title") else 0
    is_pattern_region = 1 if region.role in _PATTERN_ROLES else 0
    return (-chain_depth, bbox_area, has_text_slot, is_pattern_region)


_VARIANT_GROUP_MIN_SIBLINGS = 2
"""Minimum number of siblings sharing a ``Foo/`` prefix before Fix D
considers them a variant stack. Two is the smallest interesting case
— a single ``Modal/Empty`` is just a Modal."""

_VARIANT_BBOX_RATIO_TOLERANCE = 0.20
"""Maximum per-axis relative difference between variant bboxes before
Fix D refuses to fold them. ``Modal/Empty`` and ``Modal/Filled`` are
basically the same size; mixing in a tiny ``Modal/Toast`` would push
this past 20% and Fix D would refuse to fold."""

_VARIANT_OVERLAP_TOLERANCE = 0.05
"""Maximum overlap area (fraction of the smaller bbox) before two
siblings count as "stacked on top of each other" rather than "laid
out side-by-side". State-overlay layers (selected / hover) frequently
share the same bbox as their base; Fix D MUST NOT fold those."""


def _bbox_tuple_from_dict(
    bbox: dict[str, Any] | None,
) -> tuple[float, float, float, float] | None:
    """Project ``absoluteBoundingBox`` to ``(x, y, w, h)`` or ``None``.

    Mirrors :func:`prism_mcp.figma.utils.bbox_tuple_from_dict` without
    pulling in a circular import — the walker already depends on
    ``utils`` heavily but this helper is hot-pathed during variant
    detection and stays here for locality.
    """
    if not isinstance(bbox, dict):
        return None
    try:
        x = float(bbox.get("x", 0.0))
        y = float(bbox.get("y", 0.0))
        w = float(bbox.get("width", 0.0))
        h = float(bbox.get("height", 0.0))
    except (TypeError, ValueError):
        return None
    return (x, y, w, h)


def _bboxes_overlap_area_ratio(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Intersection area as a fraction of the smaller bbox area.

    ``0.0`` ⇒ disjoint; ``1.0`` ⇒ one bbox completely covers the
    smaller one. Used by Fix D to filter out state-overlay siblings
    that share the same bbox.
    """
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = max(0.0, aw) * max(0.0, ah)
    area_b = max(0.0, bw) * max(0.0, bh)
    smaller = min(area_a, area_b)
    if smaller <= 0.0:
        return 0.0
    return inter / smaller


def _variant_prefix(name: str) -> str | None:
    """Extract the variant-group prefix from a Figma layer name.

    Returns ``"Modal"`` for ``"Modal/Empty"``, ``"Card"`` for
    ``"Card/Normal"``, ``"Table/Normal"`` for
    ``"Table/Normal(Detach Asset)"``, or ``None`` when the name has
    no slash. The prefix is the portion before the *last* slash so
    deeply-nested namespaces (``Domain/Family/Variant``) still
    group correctly on ``Domain/Family``.
    """
    stripped = name.strip()
    if "/" not in stripped:
        return None
    head, _, tail = stripped.rpartition("/")
    if not head or not tail:
        return None
    return head


def _drop_variant_alternatives(
    parent: dict[str, Any],
    ctx: "_WalkContext",
) -> list[dict[str, Any]]:
    """Filter ``parent``'s immediate children, keeping only one
    representative per variant group.

    Fix D — Documentation-style design-system files (the X-Ray
    Master Files) lay out N alternative states of the same
    component as N sibling FRAMEs / INSTANCEs under one parent.
    Without intervention every variant spawns its own deep walker
    subtree, multiplying the agenda by the number of variants.

    The detection rules (deliberately narrow so real product pages
    never trip):

    1. **Type**: only ``FRAME`` and ``INSTANCE`` siblings can form
       a variant group. ``TEXT`` / ``VECTOR`` / etc. children are
       passed through unchanged.
    2. **Common prefix**: ``≥2`` siblings whose name has a slash
       and share the portion before the last slash
       (``Modal/Empty`` + ``Modal/Filled`` ⇒ prefix ``Modal``).
    3. **Comparable bbox shape**: each pair of variants has width
       and height within ``±20%`` of each other. Tiles in a row
       always meet this; mixing a ``Modal/Fullscreen`` with a
       ``Modal/Toast`` does not.
    4. **Non-overlapping bboxes**: pairwise overlap (relative to
       the smaller bbox) is at most ``5%``. State-overlay layers
       (e.g. hover / selected on top of a base button) share a
       bbox and MUST NOT fold — they're not alternative tiles,
       they're transparent layers.
    5. **Visible**: hidden children were already removed by
       :func:`pass_1_visible` further down in :func:`_visit`; this
       helper relies on the immediate ``iter_children`` list, but
       hidden variants get dropped in their own DFS recursion and
       don't make it onto the agenda either way.

    When a variant group is detected, the helper:

    * Keeps the first sibling in document order — it becomes the
      representative the walker recurses into.
    * Audits every other sibling under
      :attr:`DropReason.variant_alternative` with a ``detail``
      that names the group + the representative ID so the audit
      trail is human-grep-able.

    Returns the filtered list of children to walk. On real product
    pages where no two siblings share a slash-prefix the result is
    identical to ``list(iter_children(parent))`` — Fix D is a
    no-op there by construction.
    """
    children = [c for c in iter_children(parent) if isinstance(c, dict)]
    if len(children) < _VARIANT_GROUP_MIN_SIBLINGS:
        return children
    groups: dict[str, list[int]] = {}
    for idx, child in enumerate(children):
        if child.get("type") not in {"FRAME", "INSTANCE"}:
            continue
        prefix = _variant_prefix(str(child.get("name", "")))
        if prefix is None:
            continue
        groups.setdefault(prefix, []).append(idx)
    dropped_indices: set[int] = set()
    for prefix, indices in groups.items():
        if len(indices) < _VARIANT_GROUP_MIN_SIBLINGS:
            continue
        bboxes: list[tuple[float, float, float, float] | None] = [
            _bbox_tuple_from_dict(children[i].get("absoluteBoundingBox"))
            for i in indices
        ]
        if any(b is None for b in bboxes):
            continue
        if not _variants_have_comparable_shapes(bboxes):
            continue
        if not _variants_are_non_overlapping(bboxes):
            continue
        representative_idx = indices[0]
        representative_id = str(children[representative_idx].get("id", ""))
        for drop_idx in indices[1:]:
            dropped_indices.add(drop_idx)
            dropped_child = children[drop_idx]
            ctx.drop(
                dropped_child,
                DropReason.variant_alternative,
                detail=(
                    f"variant group '{prefix}/*'; representative "
                    f"{representative_id}; {len(indices)} siblings "
                    "share the slash-prefix"
                ),
            )
            for descendant in _iter_descendant_dicts(dropped_child):
                ctx.drop(
                    descendant,
                    DropReason.variant_alternative,
                    detail=(
                        f"descendant of dropped variant "
                        f"{dropped_child.get('id', '')!s} in "
                        f"group '{prefix}/*'"
                    ),
                )
    if not dropped_indices:
        return children
    return [c for i, c in enumerate(children) if i not in dropped_indices]


_INHERITED_DESCENDANT_MIN_COUNT = 5
"""Below this descendant count the inheritance-ratio check is
meaningless — a 3-child instance is fine to short-circuit either way,
so Fix B's guard returns ``True`` (allow short-circuit) by default."""

_INHERITED_DESCENDANT_THRESHOLD = 0.5
"""Minimum fraction of descendants whose IDs match the Figma
inherited format (``I<inst>;<master>;<sub>``) before Fix B's
short-circuit is allowed to fire. ``0.5`` is permissive enough that
a handful of designer-overridden text layers (regular IDs) inside a
library-component INSTANCE don't disable the short-circuit, while
still catching the dominant regression case: a page-level FRAME
whose slash name happens to look like a library component but whose
descendants are configured product-page content (0% inherited)."""


def _has_predominantly_inherited_descendants(
    node: dict[str, Any],
) -> bool:
    """Return ``True`` when most of ``node``'s descendants look like
    library-component internals.

    Fix B's short-circuit was originally a blanket rule: every
    ``INSTANCE`` and every FRAME classified as
    :attr:`FrameRole.component_instance_equivalent` (slash name)
    skipped its sub-tree. That correctly suppressed the 2,300
    inherited descendants on X-Ray Master File library-instance
    pages — but it ALSO swallowed page-level FRAMEs that happened
    to use a slash naming convention (e.g.
    ``"Modal/Fullpage"`` wrapping a configured certificate table)
    and absorbed 1,000+ real configured descendants into one
    ``MappedRegion`` with everything dumped into ``content_slots``.

    The distinguishing signal between the two cases:

    * **Library-component internals** — descendants' IDs use the
      Figma inherited form ``I<inst>;<master>;<sub>``. The leading
      ``"I"`` and the embedded ``";"`` are the unambiguous markers
      that Figma uses to flag a node as "an instance of an inner
      node from a published component".
    * **Configured product-page content** — descendants' IDs use
      the normal sequential form ``"563:36148"``. These are nodes
      the designer authored in this file, with real layout
      decisions Prism needs to map.

    The helper compares the inherited-ID fraction against
    :data:`_INHERITED_DESCENDANT_THRESHOLD` (50%). Tiny subtrees
    (under :data:`_INHERITED_DESCENDANT_MIN_COUNT` descendants)
    return ``True`` by default because the ratio is statistically
    meaningless and short-circuiting them costs nothing.

    See ``docs/x-ray-walker-investigation.md`` §8 "Fix B" for the
    original motivation, and §13 "Channel Insights regression"
    for the case that motivated this guard.
    """
    descendants = _iter_descendant_dicts(node)
    if len(descendants) < _INHERITED_DESCENDANT_MIN_COUNT:
        return True
    inherited = 0
    for d in descendants:
        nid = str(d.get("id", ""))
        if nid.startswith("I") and ";" in nid:
            inherited += 1
    ratio = inherited / len(descendants)
    return ratio >= _INHERITED_DESCENDANT_THRESHOLD


def _variants_have_comparable_shapes(
    bboxes: list[tuple[float, float, float, float] | None],
) -> bool:
    """Return ``True`` iff every pair of bboxes is within
    :data:`_VARIANT_BBOX_RATIO_TOLERANCE` on both axes."""
    widths = [b[2] for b in bboxes if b is not None]
    heights = [b[3] for b in bboxes if b is not None]
    if not widths or not heights:
        return False
    w_min, w_max = min(widths), max(widths)
    h_min, h_max = min(heights), max(heights)
    if w_max <= 0.0 or h_max <= 0.0:
        return False
    w_ratio = (w_max - w_min) / w_max
    h_ratio = (h_max - h_min) / h_max
    return (
        w_ratio <= _VARIANT_BBOX_RATIO_TOLERANCE
        and h_ratio <= _VARIANT_BBOX_RATIO_TOLERANCE
    )


def _variants_are_non_overlapping(
    bboxes: list[tuple[float, float, float, float] | None],
) -> bool:
    """Return ``True`` iff no pair of bboxes overlaps by more than
    :data:`_VARIANT_OVERLAP_TOLERANCE` (as a fraction of the smaller
    bbox area)."""
    valid = [b for b in bboxes if b is not None]
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            if (
                _bboxes_overlap_area_ratio(valid[i], valid[j])
                > _VARIANT_OVERLAP_TOLERANCE
            ):
                return False
    return True


_PATTERN_ROLES: frozenset[str] = frozenset(
    {
        "table-column",
        "tab-strip",
        "icon",
        "icon-button",
        "list-item",
        "kpi-tile",
    }
)
"""Roles that ``_agenda_importance`` treats as "load-bearing pattern
match" for the importance score. Membership is intentionally narrow:
generic ``frame`` / ``component`` / ``instance`` roles do NOT get
the bonus because they are emitted by default routing and are
exactly the rows we want to truncate first."""


def _truncate_agenda_to_max(ctx: "_WalkContext") -> None:
    """Drop low-importance :class:`MappedRegion`\\s until
    ``len(ctx.agenda) <= ctx.max_agenda``.

    Called from :func:`walk_tree` after the DFS completes and before
    the summary is built. The trimmed regions are appended to
    :attr:`_WalkContext.dropped` with reason
    :attr:`DropReason.agenda_truncated` so the audit trail surfaces
    them. Their pending mapping jobs (if any) are removed so the
    post-DFS resolver doesn't waste BM25/hybrid work on rows that
    won't ship.

    The agenda is left sorted in *DFS-emission order* (reading
    order on the canvas). Without the resort step the LLM would see
    the survivors in importance order which loses the spatial
    narrative the agenda is meant to convey.
    """
    if len(ctx.agenda) <= ctx.max_agenda:
        return
    original_len = len(ctx.agenda)
    enumerated = list(enumerate(ctx.agenda))
    enumerated.sort(key=lambda pair: _agenda_importance(pair[1]), reverse=True)
    survivors_pairs = enumerated[: ctx.max_agenda]
    survivor_ids = {id(pair[1]) for pair in survivors_pairs}
    dropped_count = 0
    for region in ctx.agenda:
        if id(region) in survivor_ids:
            continue
        ctx.dropped.append(
            DroppedNode(
                id=region.id,
                type=region.role,
                name=region.name,
                reason=DropReason.agenda_truncated,
                detail=(
                    f"agenda_size={original_len} exceeded "
                    f"max_agenda={ctx.max_agenda}; truncated by "
                    "importance ranking"
                ),
            )
        )
        dropped_count += 1
        if hasattr(ctx, "mapping_jobs"):
            for idx in range(len(ctx.mapping_jobs) - 1, -1, -1):
                job = ctx.mapping_jobs[idx]
                if id(job[0]) == id(region):
                    ctx.mapping_jobs.pop(idx)
                    break
    survivors_pairs.sort(key=lambda pair: pair[0])
    ctx.agenda = [pair[1] for pair in survivors_pairs]
    ctx.warnings.append(
        f"agenda_size={original_len} exceeded max_agenda="
        f"{ctx.max_agenda}; truncated {dropped_count} low-"
        "importance region(s); see dropped_agenda_truncated"
    )


# --------------------------------------------------------------------------
# DFS body. Returns the survivor of this subtree (possibly the
# emitted MappedRegion's id) or None when the node dropped.
# --------------------------------------------------------------------------


def _visit(
    node: dict[str, Any],
    ctx: _WalkContext,
    *,
    depth: int,
    parent_chain: list[str],
) -> _VisitResult:
    """Visit one node + its subtree.

    Returns:
        _VisitResult: ``(region_id, survived, text_content_for_parent)``.
        ``survived`` is False when the filter dropped the node;
        ``region_id`` is the id of the MappedRegion this node
        emitted (or the descendant region a passthrough collapsed
        into). ``text_content_for_parent`` is the concatenated TEXT
        captured to be added to the nearest mapping ancestor's
        content_slots.
    """
    ctx.visited += 1
    if ctx.visited > ctx.max_nodes:
        raise WalkerError(
            f"max_nodes={ctx.max_nodes} exceeded at id={node.get('id')!r}",
        )
    if depth > ctx.max_depth:
        raise WalkerError(
            f"max_depth={ctx.max_depth} exceeded at id={node.get('id')!r}",
        )

    # ---- Pass 1: visibility flag.
    if not pass_1_visible(node):
        ctx.drop(node, DropReason.explicit_hidden)
        for descendant in _iter_descendant_dicts(node):
            ctx.drop(
                descendant,
                DropReason.explicit_hidden,
                detail=f"ancestor {node.get('id', '')!s} hidden",
            )
        return _VisitResult(region_id=None, survived=False, captured_text="")

    # ---- Pass 3: non-design type drops the whole subtree.
    if not pass_3_mappable_type(node):
        ctx.drop(node, DropReason.non_design_type)
        for descendant in _iter_descendant_dicts(node):
            ctx.drop(
                descendant,
                DropReason.non_design_type,
                detail=f"ancestor {node.get('id', '')!s} non-design type",
            )
        return _VisitResult(region_id=None, survived=False, captured_text="")

    # ---- Unknown type fallback: log but treat as recurse.
    node_type = str(node.get("type", ""))
    if node_type and node_type not in MAPPABLE_TYPES:
        ctx.drop(
            node,
            DropReason.unknown_type_fallback,
            detail=f"unknown type {node_type!r}; treated as GROUP-equivalent",
        )
        # Continue to recurse — we keep the audit record but
        # don't actually drop the children.

    # ---- Pass 5 (icon coalesce) — try BEFORE recursing, since the
    # whole subtree collapses into one icon region.
    icon_match = PATTERNS[0](node)
    if icon_match is not None and _pattern_within_size_budget(
        icon_match, ctx, node
    ):
        return _emit_pattern_region(node, ctx, parent_chain, icon_match)

    # ---- Pattern detection at the cluster level (stat-list, etc.)
    # ALSO runs before recursing — the matched pattern absorbs the
    # descendants. The absorb-ratio safety rail rejects matches that
    # would swallow more than half of the input tree (typically a
    # shape-only heuristic mis-firing at page scale).
    cluster_match = _try_cluster_patterns(node)
    if cluster_match is not None and _pattern_within_size_budget(
        cluster_match, ctx, node
    ):
        return _emit_pattern_region(node, ctx, parent_chain, cluster_match)

    # ---- Fix B: respect ``RouterDecision.map_and_stop`` semantically
    # — see docs/x-ray-walker-investigation.md §4 "Defect B" and §8
    # "Fix B". The contract on ``map_and_stop`` is "emit a region for
    # THIS node and do NOT recurse further". The pre-fix walker
    # always recursed before consulting the router, so every INSTANCE
    # subtree leaked one low-confidence agenda row per inherited
    # sub-node (the dominant agenda-bloat source on design-system
    # documentation pages). Determining the decision before the
    # recurse loop lets us short-circuit those subtrees while still
    # capturing their text content for content-slot population.
    #
    # ``route_node`` ALSO returns ``map_and_stop`` for COMPONENT —
    # but a COMPONENT is the *definition* of a published library
    # component, and ``walk_tree`` is sometimes asked to walk a tree
    # rooted at a COMPONENT (a single component's design). We want to
    # walk INTO COMPONENTs the same way we'd walk into a page FRAME;
    # only INSTANCE (a *use* of a component, where descendants are
    # inherited ``I<inst>;<master>`` sub-nodes) and FRAME-instance-
    # equivalent (a FRAME whose slash-named layer name maps it to
    # a published component) get the short-circuit.
    #
    # Pattern matches above already handled the cases where we DO
    # want to absorb the subtree (icon, table-column, tab-strip,
    # etc.) so reaching this point with an instance-like node means
    # no pattern claimed it.
    early_role_hint: FrameRole | None = None
    if node_type == "FRAME":
        early_role_hint = classify_frame_role(node)
    is_instance_equivalent = node_type == "INSTANCE" or (
        node_type == "FRAME"
        and early_role_hint is FrameRole.component_instance_equivalent
    )
    # ---- Channel-Insights guard for Fix B. The original Fix B
    # short-circuited every INSTANCE / slash-named FRAME, which
    # broke product pages whose layout *uses* a slash naming
    # convention (e.g. ``Modal/Fullpage`` wrapping a configured
    # certificate table). Short-circuit only when the sub-tree
    # actually looks like library internals — i.e. descendants'
    # IDs are predominantly the ``I<inst>;<master>;<sub>`` inherited
    # form. See ``docs/x-ray-walker-investigation.md`` §8 + §13.
    if is_instance_equivalent and _has_predominantly_inherited_descendants(
        node
    ):
        return _emit_instance_equivalent_without_recursion(
            node, ctx, parent_chain
        )

    # ---- Recurse into children. Capture their results.
    own_name = str(node.get("name", ""))
    new_parent_chain = [*parent_chain, own_name] if own_name else parent_chain

    # ---- Fix D: variant-stack pruning. Documentation-style files
    # (the X-Ray Master Files) tile multiple alternative states of
    # the same component side-by-side under the parent FRAME, e.g.
    # ``Modal/Empty``, ``Modal/Filled``, ``Modal/Error`` as three
    # sibling FRAMEs. Without intervention each sibling spawns its
    # own deep subtree of regions, multiplying the agenda by the
    # number of variants. ``_drop_variant_alternatives`` keeps only
    # the first variant and audits the rest under
    # :attr:`DropReason.variant_alternative` *before* the recurse
    # loop runs, so the dropped siblings never contribute to
    # ``child_pairs`` / ``significant_children``. The heuristic is
    # narrow (≥2 siblings sharing a slash-prefix + comparable bboxes
    # + non-overlapping) and is a no-op on real product pages where
    # repeated siblings under the same ``Foo/*`` prefix don't occur.
    # See ``docs/x-ray-walker-investigation.md`` §11.5 + §12 "Fix D".
    children_to_walk = _drop_variant_alternatives(node, ctx)

    # ``child_pairs`` is the paired list of immediate-child raw dicts
    # alongside the region id they bubbled up (``None`` when the
    # child captured-as-content-slot or was an icon-internal). The
    # *raw-dict* side carries the correct geometry for layout
    # inference; the *region-id* side is what the agenda indexes on.
    # See design doc §4.6.2 — keeping them paired is what lets
    # ``analyze_layout`` reason about the immediate children while
    # the walker still references the bubbled region in ``layout``.
    child_pairs: list[tuple[dict[str, Any], str | None]] = []
    significant_children: list[dict[str, Any]] = []
    child_region_ids: list[str] = []
    captured_text_parts: list[str] = []
    for child in children_to_walk:
        result = _visit(
            child, ctx, depth=depth + 1, parent_chain=new_parent_chain
        )
        if not result.survived:
            continue
        child_pairs.append((child, result.region_id))
        significant_children.append(child)
        if result.region_id:
            child_region_ids.append(result.region_id)
        if result.captured_text:
            captured_text_parts.append(result.captured_text)

    # ---- Pass 4: same-bbox passthrough collapse. If single
    # significant child + same bbox, drop the parent and tell the
    # caller to use the child's region id.
    collapse_target = pass_4_collapse_passthrough(node, significant_children)
    if collapse_target is not None:
        target_id = str(collapse_target.get("id", ""))
        ctx.drop(
            node,
            DropReason.same_bbox_passthrough_collapsed,
            detail=(
                f"collapsed into {target_id!s} "
                f"({collapse_target.get('name', '')!s})"
            ),
        )
        # The target inherited this node's place in the layout
        # tree; ascend.
        return _VisitResult(
            region_id=target_id or None,
            survived=True,
            captured_text="\n".join(captured_text_parts),
        )

    # ---- If this is a leaf with no significant children, apply
    # Pass 2 and Pass 6.
    if not significant_children:
        if not pass_2_invisible_decoration(node):
            ctx.drop(node, DropReason.invisible_decoration)
            return _VisitResult(
                region_id=None, survived=False, captured_text=""
            )
        if not pass_6_tiny_decorative(node):
            ctx.drop(node, DropReason.tiny_decorative)
            return _VisitResult(
                region_id=None, survived=False, captured_text=""
            )

    # ---- Pass 7 (capture-as-content-slot) — TEXT folds into the
    # nearest mapping ancestor. The walker emits an audit entry
    # but doesn't add the text node as an agenda row.
    if node_type in {"TEXT", "TEXT_PATH"}:
        chars = get_characters(node)
        ctx.drop(
            node,
            DropReason.captured_as_content_slot,
            detail=f"text={chars!r}" if chars else "empty",
        )
        return _VisitResult(
            region_id=None,
            survived=True,
            captured_text=chars,
        )

    # ---- Route. Decide whether to emit a region and / or recurse.
    decision = route_node(node)
    role_hint: FrameRole | None = None
    if node_type == "FRAME":
        role_hint = classify_frame_role(node)

    if decision is RouterDecision.map_and_stop or (
        node_type == "FRAME"
        and role_hint is FrameRole.component_instance_equivalent
    ):
        return _emit_simple_region(
            node,
            ctx,
            parent_chain,
            child_region_ids,
            captured_text_parts,
            role=node_type.lower(),
            significant_children=significant_children,
            child_pairs=child_pairs,
        )

    if node_type == "FRAME" and role_hint is FrameRole.composed_region:
        return _emit_simple_region(
            node,
            ctx,
            parent_chain,
            child_region_ids,
            captured_text_parts,
            role="composed-region",
            significant_children=significant_children,
            child_pairs=child_pairs,
        )

    # Layout containers + pattern_cluster fallback + generic GROUPs
    # don't emit a region themselves; they just pass through.
    text_for_parent = "\n".join(captured_text_parts)

    # Add this node to the layout tree IF it has child regions —
    # keeps the layout tree free of empty wrappers.
    if child_region_ids:
        layout_node = LayoutNode(
            id=str(node.get("id", "")),
            name=own_name or node_type or "node",
            role="layout-container",
            bbox=bbox_tuple_from_dict(node.get("absoluteBoundingBox")),
            children_ids=child_region_ids,
        )
        # Spatial layout inference is temporarily disabled to keep
        # the LLM-facing output compact while the X-Ray walker
        # fixes land. See docs/x-ray-walker-investigation.md §13.
        # The helper and its unit tests remain on disk so the
        # revival path is a one-line change.
        # _attach_layout_analysis(ctx, node, layout_node, child_pairs)
        ctx.layout_tree.append(layout_node)

    return _VisitResult(
        region_id=None,
        survived=True,
        captured_text=text_for_parent,
    )


# --------------------------------------------------------------------------
# Region emission helpers.
# --------------------------------------------------------------------------


def _emit_instance_equivalent_without_recursion(
    node: dict[str, Any],
    ctx: _WalkContext,
    parent_chain: list[str],
) -> _VisitResult:
    """Emit a region for an INSTANCE / COMPONENT / FRAME-instance-
    equivalent node WITHOUT visiting its sub-tree.

    Per :class:`prism_mcp.figma.routing.RouterDecision.map_and_stop`:
    *"Emit a MappedRegion for this node and do NOT recurse further
    for routing (children may still be inspected for content-slot
    capture)."* The published library component carries its own
    internals; the walker's job stops at the outermost instance
    boundary. See ``docs/x-ray-walker-investigation.md`` §8 "Fix B".

    The helper does three things:

    1. Walks the descendants once (no ``_visit`` recursion) to
       collect TEXT bodies for ``content_slots["title"]`` /
       ``["items"]`` population.
    2. Audits each descendant with
       :class:`DropReason.captured_as_content_slot` so the dropped
       histogram surfaces the absorbed sub-tree.
    3. Delegates to :func:`_emit_simple_region` with empty
       ``child_pairs`` / ``child_region_ids`` so the layout tree
       treats the instance as a leaf.

    The immediate children dicts ARE passed to
    :func:`_emit_simple_region` via ``significant_children`` so
    :func:`extract_box_style` can still infer padding from
    parent-child bbox offsets when the instance lacks auto-layout
    metadata.
    """
    node_type = str(node.get("type", ""))
    immediate_children = [
        c for c in iter_children(node) if isinstance(c, dict)
    ]
    descendant_texts: list[str] = []
    for descendant in _iter_descendant_dicts(node):
        if descendant.get("type") in {"TEXT", "TEXT_PATH"}:
            chars = get_characters(descendant)
            if chars:
                descendant_texts.append(chars)
        ctx.drop(
            descendant,
            DropReason.captured_as_content_slot,
            detail=(
                f"descendant of {node_type} "
                f"{node.get('id', '')!s}; subtree not walked "
                "(Fix B: instance boundary)"
            ),
        )
    return _emit_simple_region(
        node,
        ctx,
        parent_chain,
        child_region_ids=[],
        captured_text_parts=descendant_texts,
        role=node_type.lower() or "instance",
        significant_children=immediate_children,
        child_pairs=[],
    )


def _emit_simple_region(
    node: dict[str, Any],
    ctx: _WalkContext,
    parent_chain: list[str],
    child_region_ids: list[str],
    captured_text_parts: list[str],
    *,
    role: str,
    significant_children: list[dict[str, Any]] | None = None,
    child_pairs: list[tuple[dict[str, Any], str | None]] | None = None,
) -> _VisitResult:
    """Emit a non-pattern :class:`MappedRegion` for ``node``."""
    node_id = str(node.get("id", ""))
    name = str(node.get("name", "")) or node.get("type", "") or "region"

    text_content_for_region = "\n".join(captured_text_parts).strip() or None

    hex_colors = extract_visible_hexes(node)
    _seed_tokens(ctx, hex_colors)

    box_style_dict = extract_box_style(node, children=significant_children)
    box_style = BoxStyle(**box_style_dict)
    children_summary = _summarise_children(node)
    structural_hints = _structural_hints_for(node, box_style=box_style)

    bbox = bbox_tuple_from_dict(node.get("absoluteBoundingBox"))
    bucket = shape_bucket(bbox)

    mapping, queued_kwargs, cache_key = _invoke_mapping_fn(
        ctx,
        node_name=name,
        node_type=str(node.get("type", "")),
        text_content=text_content_for_region,
        children_summary=children_summary,
        structural_hints=structural_hints,
        parent_chain=list(parent_chain),
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        region_role=role,
        region_shape_bucket=bucket,
    )

    content_slots: dict[str, str | list[str] | int] = {}
    if text_content_for_region:
        content_slots["title"] = text_content_for_region.split("\n")[0]
        if "\n" in text_content_for_region:
            content_slots["items"] = text_content_for_region.split("\n")

    region = MappedRegion(
        id=node_id or name,
        name=name,
        role=role,
        bbox=bbox,
        parent_chain=list(parent_chain),
        content_slots=content_slots,
        structural_hints=structural_hints,
        children_summary=children_summary,
        hex_colors=hex_colors,
        box_style=box_style,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        mapping=mapping,
        shape_bucket=bucket,
    )
    if queued_kwargs is not None and cache_key is not None:
        # Production path: defer the real mapper call. The
        # low_confidence warning depends on the resolved candidates
        # so it runs inside :func:`_resolve_pending_mappings` after
        # the DFS, NOT here.
        ctx.mapping_jobs.append((region, queued_kwargs, cache_key))
    else:
        # Test / stub path: no real mapper, ``mapping`` is the final
        # value. Emit the audit synchronously to preserve the legacy
        # ordering tests rely on.
        _maybe_emit_low_confidence_warning(ctx, region)
    ctx.agenda.append(region)
    layout_node = LayoutNode(
        id=node_id or name,
        name=name,
        role=role,
        bbox=region.bbox,
        children_ids=list(child_region_ids),
    )
    # Spatial layout inference is temporarily disabled to keep the
    # LLM-facing output compact while the X-Ray walker fixes land.
    # See docs/x-ray-walker-investigation.md §13. The helper and
    # its unit tests remain on disk so the revival path is a
    # one-line change.
    # if child_pairs:
    #     _attach_layout_analysis(ctx, node, layout_node, child_pairs)
    ctx.layout_tree.append(layout_node)
    return _VisitResult(
        region_id=region.id,
        survived=True,
        captured_text="",  # consumed
    )


def _emit_pattern_region(
    node: dict[str, Any],
    ctx: _WalkContext,
    parent_chain: list[str],
    match: PatternMatch,
) -> _VisitResult:
    """Emit a :class:`MappedRegion` for a matched pattern + drop
    every absorbed descendant from further routing."""
    node_id = str(node.get("id", ""))
    name = str(node.get("name", "")) or node.get("type", "") or match.kind

    for absorbed_id in match.absorbed_ids:
        ctx.dropped.append(
            DroppedNode(
                id=absorbed_id,
                name="",
                type="",
                reason=str(
                    DropReason.icon_internal
                    if match.absorbed_reason == "icon_internal"
                    else DropReason.folded_into_pattern
                ),
                detail=f"folded into {match.kind} at {node_id}",
            )
        )

    hex_colors = extract_visible_hexes(node)
    _seed_tokens(ctx, hex_colors)

    box_style_dict = extract_box_style(node)
    box_style = BoxStyle(**box_style_dict)

    # Patterns provide their own text_content via content_slots —
    # the walker forwards the slot strings into map_figma_node.
    text_content_for_region = _content_slots_to_text(match.content_slots)
    enriched_hints = list(match.structural_hints) + _box_style_hints(box_style)

    bbox = bbox_tuple_from_dict(node.get("absoluteBoundingBox"))
    bucket = shape_bucket(bbox)

    mapping, queued_kwargs, cache_key = _invoke_mapping_fn(
        ctx,
        node_name=name,
        node_type=str(node.get("type", "")),
        text_content=text_content_for_region,
        children_summary=match.children_summary,
        structural_hints=enriched_hints,
        parent_chain=list(parent_chain),
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        region_role=match.kind,
        region_shape_bucket=bucket,
    )

    region = MappedRegion(
        id=node_id or name,
        name=name,
        role=match.kind,
        bbox=bbox,
        parent_chain=list(parent_chain),
        content_slots=dict(match.content_slots),
        structural_hints=enriched_hints,
        children_summary=match.children_summary,
        hex_colors=hex_colors,
        box_style=box_style,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        mapping=mapping,
        shape_bucket=bucket,
    )
    if queued_kwargs is not None and cache_key is not None:
        ctx.mapping_jobs.append((region, queued_kwargs, cache_key))
    else:
        _maybe_emit_low_confidence_warning(ctx, region)
    ctx.agenda.append(region)
    ctx.layout_tree.append(
        LayoutNode(
            id=node_id or name,
            name=name,
            role=match.kind,
            bbox=region.bbox,
            children_ids=[],
        )
    )
    return _VisitResult(
        region_id=region.id,
        survived=True,
        captured_text="",
    )


def _try_cluster_patterns(node: dict[str, Any]) -> PatternMatch | None:
    """Run cluster patterns (skipping the icon detector at index 0).

    Returns the first match or ``None``. The icon pattern is run
    *before* this function inside :func:`_visit` because icons
    collapse leaf subtrees and shouldn't share the cluster
    code-path.

    Page-scale gate: when the candidate node's larger bbox edge
    exceeds :data:`prism_mcp.figma.patterns.PAGE_SCALE_MIN_EDGE`,
    we skip the patterns in
    :data:`prism_mcp.figma.patterns.PATTERNS_LEAF_SCALE` (kpi-tile,
    button-group, stat-list). Those rely on shape + text heuristics
    without a strong layer-name anchor and are only meaningful for
    small clusters; at page scale only the name-anchored patterns
    (column-of-cells, tab-strip) can correctly fire. See design doc
    §4.4.1 (a page-sized FRAME is a composed-region, not a leaf
    pattern).
    """
    bbox = node.get("absoluteBoundingBox") or {}
    try:
        max_edge = max(
            float(bbox.get("width", 0)), float(bbox.get("height", 0))
        )
    except (TypeError, ValueError):
        max_edge = 0.0
    is_page_scale = max_edge > PAGE_SCALE_MIN_EDGE

    for predicate in PATTERNS[1:]:
        if is_page_scale and predicate in PATTERNS_LEAF_SCALE:
            continue
        match = predicate(node)
        if match is not None:
            return match
    return None


def _pattern_within_size_budget(
    match: PatternMatch,
    ctx: _WalkContext,
    node: dict[str, Any],
) -> bool:
    """Return True if the pattern match is within the absorb-ratio
    safety budget, False if it should be rejected.

    On rejection, appends one :class:`DroppedNode` audit row with
    reason :attr:`DropReason.pattern_oversized_reject` (so the user
    can see what the walker chose NOT to do) and one warning to
    ``ctx.warnings`` (so the agenda's first-turn summary surfaces it).

    The rail only fires for trees with at least
    :data:`_PATTERN_ABSORB_MIN_TREE_SIZE` nodes — small fixtures with
    legitimately high absorb fractions (e.g. a 4-stripe icon in a
    6-node mini-tree) would otherwise be falsely rejected.
    """
    if ctx.input_nodes < _PATTERN_ABSORB_MIN_TREE_SIZE:
        return True
    absorb_count = len(match.absorbed_ids)
    if absorb_count == 0:
        return True
    ratio = absorb_count / ctx.input_nodes
    if ratio <= _PATTERN_ABSORB_MAX_RATIO:
        return True

    pct = int(round(ratio * 100))
    detail = (
        f"{match.kind} match would absorb {absorb_count} of "
        f"{ctx.input_nodes} nodes ({pct}%); exceeded "
        f"{int(_PATTERN_ABSORB_MAX_RATIO * 100)}% safety rail"
    )
    ctx.drop(node, DropReason.pattern_oversized_reject, detail=detail)
    ctx.warnings.append(
        f"pattern '{match.kind}' rejected at "
        f"{node.get('id', '?')!s} ({node.get('name', '')!r}): {detail}. "
        "Walker continued recursive descent instead."
    )
    logger.warning(
        "rejected oversized pattern match kind=%s node_id=%s ratio=%.2f",
        match.kind,
        node.get("id", "?"),
        ratio,
    )
    return False


# --------------------------------------------------------------------------
# Region-emission helpers — pure functions.
# --------------------------------------------------------------------------


def _summarise_children(node: dict[str, Any]) -> str:
    """One-line description of immediate descendants.

    Example: ``"FRAME Header(1 TEXT)"`` or ``"3 INSTANCE"``.
    """
    children = iter_children(node)
    if not children:
        return ""
    type_counts: dict[str, int] = {}
    for c in children:
        t = str(c.get("type", "?"))
        type_counts[t] = type_counts.get(t, 0) + 1
    parts = [f"{count} {t}" for t, count in sorted(type_counts.items())]
    return ", ".join(parts)


def _structural_hints_for(
    node: dict[str, Any],
    *,
    box_style: BoxStyle | None = None,
) -> list[str]:
    """Generate cheap structural hints for the BM25 query.

    When ``box_style`` is supplied, descriptive strings for any
    background / border / corner-radius / padding / shadow / opacity
    are appended so the query is biased toward Prism components that
    match the visual presence of the FRAME (e.g. surfacing
    ``Card`` / ``Alert`` for a rounded grey-filled banner).
    """
    hints: list[str] = []
    bbox = node.get("absoluteBoundingBox") or {}
    w = int(float(bbox.get("width", 0)))
    h = int(float(bbox.get("height", 0)))
    if w and h:
        ratio = w / h if h else 0
        if 0.95 < ratio < 1.05:
            hints.append(f"{w}x{h} square")
        elif ratio > 2.5:
            hints.append(f"{w}x{h} wide")
        elif ratio < 0.4:
            hints.append(f"{w}x{h} tall")
        else:
            hints.append(f"{w}x{h}")
    children = iter_children(node)
    if children:
        hints.append(f"{len(children)} children")
    if box_style is not None:
        hints.extend(_box_style_hints(box_style))
    return hints


def _box_style_hints(box_style: BoxStyle) -> list[str]:
    """Translate a :class:`BoxStyle` into BM25-friendly hint strings.

    These are duplicates of the structured ``box_style`` field on
    purpose — the structured field is exact (best for code
    generation), the hint strings widen the term overlap for the
    text search index (best for component matching). Empty styles
    produce an empty list.
    """
    hints: list[str] = []
    if box_style.background_color is not None:
        hints.append(f"background {box_style.background_color}")
    if box_style.border_color is not None:
        width = box_style.border_width or 1
        hints.append(f"border {int(width)}px {box_style.border_color}")
    corner = box_style.corner_radius
    if isinstance(corner, (int, float)) and corner > 0:
        hints.append(f"rounded {int(corner)}px")
    elif isinstance(corner, list):
        hints.append("rounded mixed corners")
    if box_style.padding is not None:
        t, r, b, _l = box_style.padding
        if t == b and r == _l:
            if t == r:
                hints.append(f"padding {int(t)}px")
            else:
                hints.append(f"padding {int(t)}px {int(r)}px")
        else:
            hints.append(
                f"padding {int(t)}px {int(r)}px {int(b)}px {int(_l)}px"
            )
    if box_style.gap is not None and box_style.gap > 0:
        hints.append(f"gap {int(box_style.gap)}px")
    if box_style.has_shadow:
        hints.append("shadow")
    if box_style.opacity is not None:
        hints.append(f"opacity {box_style.opacity:.2f}")
    return hints


def _content_slots_to_text(
    content_slots: dict[str, str | list[str] | int],
) -> str | None:
    """Flatten content slots into a single string for BM25/embedding."""
    pieces: list[str] = []
    for key in ("title", "header", "label", "value"):
        val = content_slots.get(key)
        if isinstance(val, str) and val:
            pieces.append(val)
    items = content_slots.get("items")
    if isinstance(items, list):
        pieces.extend(str(i) for i in items if i)
    return " ".join(pieces) if pieces else None


def _seed_tokens(ctx: _WalkContext, hex_colors: list[str]) -> None:
    """Seed the tokens map with the designer's variable_defs lookup."""
    for hex_value in hex_colors:
        ctx.tokens.setdefault(hex_value, ctx.variable_defs.get(hex_value, ""))


def _build_mapping_call(
    *,
    node_name: str,
    node_type: str,
    text_content: str | None,
    children_summary: str,
    structural_hints: list[str],
    parent_chain: list[str],
    hex_colors: list[str],
    reference_jsx_slice: str | None,
    region_role: str | None = None,
    region_shape_bucket: str | None = None,
) -> tuple[FigmaNodeMapping, dict[str, Any], tuple]:
    """Build the ``(placeholder_mapping, kwargs, cache_key)`` triple
    that the emit helpers stash on :attr:`_WalkContext.mapping_jobs`.

    The placeholder mapping carries only ``node_name`` and a
    ``None`` ``suggested_component_name``; it is the value attached
    to the :class:`MappedRegion` while the DFS runs. The actual
    candidates / related / a11y / token / examples lists are filled
    in by :func:`_resolve_pending_mappings` after the DFS returns.

    ``cache_key`` is a hashable tuple derived from the kwargs. Two
    regions with byte-identical inputs share the same key, so the
    resolver runs the underlying mapper exactly once for each
    unique key and broadcasts the result to every region that
    shares it (Optimisation 2 — dedup).
    """
    kwargs: dict[str, Any] = {
        "node_name": node_name,
        "node_type": node_type or None,
        "reference_code": reference_jsx_slice,
        "text_content": text_content,
        "children_summary": children_summary or None,
        "structural_hints": structural_hints or None,
        "parent_chain": parent_chain or None,
        "hex_colors": hex_colors or None,
        "region_role": region_role,
        "region_shape_bucket": region_shape_bucket,
    }
    cache_key = _make_mapping_cache_key(kwargs)
    placeholder = FigmaNodeMapping(
        node_name=node_name,
        suggested_component_name=None,
    )
    return placeholder, kwargs, cache_key


def _invoke_mapping_fn(
    ctx: _WalkContext,
    *,
    node_name: str,
    node_type: str,
    text_content: str | None,
    children_summary: str,
    structural_hints: list[str],
    parent_chain: list[str],
    hex_colors: list[str],
    reference_jsx_slice: str | None,
    region_role: str | None = None,
    region_shape_bucket: str | None = None,
) -> tuple[FigmaNodeMapping, dict[str, Any] | None, tuple | None]:
    """Decide whether to call the mapper now or defer it.

    Returns a ``(mapping, kwargs_or_None, cache_key_or_None)``
    tuple:

    * **Test path** — when ``ctx.map_figma_node_fn`` is ``None``
      (the filter / pattern unit tests use ``walk_tree`` with
      ``map_figma_node_fn=None`` to avoid standing up the real
      library index), this returns an empty stub immediately
      with ``kwargs=None`` / ``cache_key=None`` so the caller
      knows nothing needs queuing.
    * **Production path** — when a mapper is wired, returns a
      placeholder mapping plus the ``kwargs`` and ``cache_key``
      the emit helper appends to :attr:`_WalkContext.mapping_jobs`
      after creating the :class:`MappedRegion`. The placeholder's
      ``.candidates`` / ``.related`` / etc. stay empty until
      :func:`_resolve_pending_mappings` overwrites the mapping
      with the real result (post-DFS, optionally in parallel).

    ``region_role`` and ``region_shape_bucket`` flow into the
    ranker's role-synonym (+0.15) and shape-bucket (+0.05) bonuses
    (:data:`prism_mcp.workflow.figma_mapping.ROLE_TO_COMPONENT_SYNONYMS`
    and
    :data:`prism_mcp.workflow.figma_mapping.SHAPE_BUCKET_TO_COMPONENT_SYNONYMS`).
    Passing ``None`` for either keeps v1 behaviour byte-for-byte.
    """
    if ctx.map_figma_node_fn is None:
        return (
            FigmaNodeMapping(
                node_name=node_name,
                suggested_component_name=None,
            ),
            None,
            None,
        )
    placeholder, kwargs, cache_key = _build_mapping_call(
        node_name=node_name,
        node_type=node_type,
        text_content=text_content,
        children_summary=children_summary,
        structural_hints=structural_hints,
        parent_chain=parent_chain,
        hex_colors=hex_colors,
        reference_jsx_slice=reference_jsx_slice,
        region_role=region_role,
        region_shape_bucket=region_shape_bucket,
    )
    return placeholder, kwargs, cache_key


def _make_mapping_cache_key(kwargs: dict[str, Any]) -> tuple:
    """Build a hashable cache key from :func:`map_figma_node` kwargs.

    Two regions with identical kwargs produce identical mappings
    (the mapper is a pure function of its inputs), so they can
    share one resolved result. The key tuple's fields cover every
    input that contributes to the queries, the ranker bonuses, or
    the token / a11y / example lookups:

    * ``node_name`` / ``node_type`` / ``reference_code`` — feed
      both ``_build_lexical_query`` and ``_build_semantic_query``.
    * ``text_content`` / ``children_summary`` / ``structural_hints``
      / ``parent_chain`` — additional lexical-query enrichment.
    * ``hex_colors`` — drive the per-region token mappings.
    * ``region_role`` / ``region_shape_bucket`` — drive the
      synonym-bonus rewrites in the fused ranker.

    List inputs are converted to tuples so the key remains
    hashable. ``None`` values are normalised to empty strings /
    tuples so e.g. ``hex_colors=None`` and ``hex_colors=[]``
    collapse to the same key (the mapper treats them identically).
    """
    return (
        kwargs.get("node_name") or "",
        kwargs.get("node_type") or "",
        kwargs.get("reference_code") or "",
        kwargs.get("text_content") or "",
        kwargs.get("children_summary") or "",
        tuple(kwargs.get("structural_hints") or ()),
        tuple(kwargs.get("parent_chain") or ()),
        tuple(kwargs.get("hex_colors") or ()),
        kwargs.get("region_role") or "",
        kwargs.get("region_shape_bucket") or "",
    )


def _resolve_worker_count() -> int:
    """Read :data:`_PARALLEL_MAPPING_WORKERS_ENV` and clamp.

    Honours an explicit env-var value (``1`` for serial, ``N`` up
    to :data:`_MAX_PARALLEL_WORKERS`) and otherwise auto-detects:
    ``min(_DEFAULT_PARALLEL_WORKERS, max(1, cpu_count // 2))``.
    ``cpu_count`` returning ``None`` (rare) falls back to 4 cores
    so we still get a reasonable default.
    """
    raw = os.environ.get(_PARALLEL_MAPPING_WORKERS_ENV, "")
    configured = 0
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            logger.warning(
                "ignoring non-integer %s=%r; falling back to auto-detect",
                _PARALLEL_MAPPING_WORKERS_ENV,
                raw,
            )
            configured = 0
    if configured > 0:
        return min(configured, _MAX_PARALLEL_WORKERS)
    cpu = os.cpu_count() or 4
    return min(_DEFAULT_PARALLEL_WORKERS, max(1, cpu // 2))


def _resolve_pending_mappings(ctx: _WalkContext) -> None:
    """Run every queued :func:`map_figma_node` call and stitch results
    back onto their :class:`MappedRegion`.

    Three optimisations layered into this one pass:

    1. **Dedup** (Optimisation 2) — group jobs by ``cache_key`` so
       byte-identical inputs run the mapper exactly once. Common
       on pages with repeated icons / repeated stat-list patterns.
    2. **Parallel execution** (Optimisation 7) — submit the unique
       jobs to a :class:`ThreadPoolExecutor`. ``map_figma_node``
       is read-only against the shared library indices, and the
       hot stages (dense encode + cross-encoder rerank) release
       the GIL during ONNX inference, so wall-time scales close
       to the worker count up to the
       :data:`_MAX_PARALLEL_WORKERS` ceiling.
    3. **Deferred low-confidence audit** — the per-region
       ``low_confidence`` warning depends on the resolved
       ``mapping.candidates`` and so cannot run during the DFS.
       We run it once per region here, after the in-place
       mapping update, preserving the audit semantics from the
       legacy single-threaded path.

    Fault tolerance: a single mapper raising does NOT abort the
    walk. The exception is logged, the placeholder mapping is
    left intact for affected regions, and a per-region warning
    is appended to :attr:`_WalkContext.warnings` so the LLM /
    operator sees what failed.
    """
    if not ctx.mapping_jobs:
        return
    fn = ctx.map_figma_node_fn
    if fn is None:  # defensive — jobs should not have been queued
        ctx.mapping_jobs.clear()
        return

    unique_kwargs: dict[tuple, dict[str, Any]] = {}
    key_to_regions: dict[tuple, list[MappedRegion]] = {}
    for region, kwargs, cache_key in ctx.mapping_jobs:
        if cache_key not in unique_kwargs:
            unique_kwargs[cache_key] = kwargs
        key_to_regions.setdefault(cache_key, []).append(region)

    workers = _resolve_worker_count()
    keys = list(unique_kwargs.keys())
    results: dict[tuple, FigmaNodeMapping | Exception] = {}

    def _call_one(
        key: tuple,
    ) -> tuple[tuple, FigmaNodeMapping | Exception]:
        try:
            return key, fn(**unique_kwargs[key])
        except Exception as exc:
            # Catch-all on purpose: one mapper failure must not abort
            # the whole walk — every other region's mapping is still
            # valid. We log + record a per-region warning further
            # down and leave the placeholder mapping in place.
            logger.warning(
                "map_figma_node raised for cache_key=%r: %s",
                key,
                exc,
            )
            return key, exc

    if workers > 1 and len(keys) > 1:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(keys)),
            thread_name_prefix="prism-map-figma",
        ) as executor:
            for key, outcome in executor.map(_call_one, keys):
                results[key] = outcome
    else:
        for key in keys:
            _, outcome = _call_one(key)
            results[key] = outcome

    cache_hits = len(ctx.mapping_jobs) - len(keys)
    failures = 0
    for key, regions in key_to_regions.items():
        outcome = results.get(key)
        if isinstance(outcome, Exception):
            failures += 1
            for region in regions:
                ctx.warnings.append(
                    f"map_figma_node failed for region {region.id} "
                    f"({region.name!r}): {outcome!r}; leaving "
                    "placeholder mapping in place"
                )
            continue
        for region in regions:
            # The mapper echoes ``node_name`` from its first input
            # in the result. When two regions share a cache key
            # they share ``node_name`` too (the key includes it),
            # so this is a no-op rewrite — kept defensively for
            # safety.
            region.mapping = outcome.model_copy(
                update={"node_name": region.name}
            )

    for region, _kwargs, _cache_key in ctx.mapping_jobs:
        _maybe_emit_low_confidence_warning(ctx, region)

    logger.info(
        "resolved %d mapping job(s): %d unique calls, %d cache hit(s), "
        "%d failure(s), %d worker(s)",
        len(ctx.mapping_jobs),
        len(keys),
        cache_hits,
        failures,
        workers,
    )
    ctx.mapping_jobs.clear()


_LOW_CONFIDENCE_THRESHOLD = 0.05
"""Top-candidate score below which the walker emits a
``low_confidence`` warning for the region.

Calibrated against the actual RRF ceiling produced by
:func:`prism_mcp.workflow.figma_mapping._build_candidates`:

    BM25 rank 0           1/(60+0+1) = 0.01639
    Hybrid rank 0         1/(60+0+1) = 0.01639
    Both rankers agree    0.01639 + 0.01639 = 0.03279
    + role bonus (+0.15)  0.183
    + shape bonus (+0.05) 0.233  ← absolute max achievable

The previous value (0.3) was 30% above this absolute ceiling,
so every non-pattern region was flagged ``low_confidence`` by
construction — the b213fac1 / 753:27069 trace surfaced 93
warnings on 50 agenda rows. With 0.05 the warning only fires
when neither ranker had a real signal (single-token hit at
rank ≥ 1, no role/shape bonus), preserving the original intent
of "tell the LLM when we're guessing" without crying wolf.

See ``docs/handoff-spatial-and-ranker.md`` §3.4 for the
original calibration discussion."""


def _maybe_emit_low_confidence_warning(
    ctx: _WalkContext,
    region: MappedRegion,
) -> None:
    """Append a ``low_confidence`` warning to ``ctx.warnings`` when
    the top candidate's score is below
    :data:`_LOW_CONFIDENCE_THRESHOLD` AND no
    :attr:`FigmaNodeMapping.primary_recommendation` overrides it.

    No-op when the region has zero candidates (typical when the
    walker is exercised with ``map_figma_node_fn=None`` from unit
    tests). The warning surfaces in the public
    :class:`FigmaTreeMapping.warnings` list so the LLM sees the
    flag in the same turn — a much softer mitigation than a hard
    fail at the safety rail.

    The ``primary_recommendation`` short-circuit prevents
    spurious warnings on pattern regions where the deterministic
    detector has already supplied a high-confidence override —
    they don't need a "low confidence" flag even when BM25
    happens to score sub-threshold.
    """
    candidates = region.mapping.candidates
    if not candidates:
        return
    if region.mapping.primary_recommendation is not None:
        return
    top = candidates[0]
    if top.score >= _LOW_CONFIDENCE_THRESHOLD:
        return
    ctx.warnings.append(
        f"low_confidence region {region.id} ({region.name!r}): "
        f"top candidate {top.name!r} score={top.score:.3f} below "
        f"threshold {_LOW_CONFIDENCE_THRESHOLD}; the LLM should "
        "disclaim the auto-pick or fall back to atomic tools."
    )


def _iter_descendant_dicts(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Iterative enumeration of all descendant dicts under ``node``."""
    out: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = list(iter_children(node))
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(iter_children(cur))
    return out


def _attach_layout_analysis(
    ctx: _WalkContext,
    parent_node: dict[str, Any],
    layout_node: LayoutNode,
    child_pairs: list[tuple[dict[str, Any], str | None]],
) -> None:
    """Run :func:`analyze_layout` on ``parent_node`` + the immediate
    child dicts, translate the returned id lists to the bubbled
    region ids that match :attr:`_WalkContext.agenda`, and stamp the
    result onto ``layout_node.layout``.

    For every immediate child id that ended up in
    :attr:`LayoutAnalysis.absolute_children`, compute an
    :class:`AbsolutePos` and write it onto the corresponding
    :class:`MappedRegion.absolute_pos` in the agenda. The ``z_order``
    is assigned by sorting absolute children by bbox area DESCENDING
    — largest gets ``z_order=0`` (bottom of stack), smallest gets the
    highest index (badge / overlay on top). Mirrors the convention
    Figma-Context-MCP uses in its emitted ``absolute`` blocks.

    No-ops when the analysis has no useful signal (zero children,
    direction None) or when ``child_pairs`` is empty.
    """
    if not child_pairs:
        return
    immediate_children = [c for c, _ in child_pairs]
    analysis = analyze_layout(parent_node, immediate_children)
    if analysis.direction is None and not analysis.absolute_children:
        return

    id_to_region: dict[str, str | None] = {}
    id_to_dict: dict[str, dict[str, Any]] = {}
    for child_dict, region_id in child_pairs:
        cid = str(child_dict.get("id", ""))
        if cid:
            id_to_region[cid] = region_id
            id_to_dict[cid] = child_dict

    translated_flow = [
        id_to_region[i]
        for i in analysis.flow_children
        if id_to_region.get(i)
    ]
    translated_abs = [
        id_to_region[i]
        for i in analysis.absolute_children
        if id_to_region.get(i)
    ]
    # ``model_copy`` keeps the rationale + scoring fields intact
    # while we rewrite the id lists.
    layout_node.layout = analysis.model_copy(
        update={
            "flow_children": [r for r in translated_flow if r is not None],
            "absolute_children": [r for r in translated_abs if r is not None],
        }
    )

    if not analysis.absolute_children:
        return

    # Largest-first sort so z_order matches the CSS convention
    # (lower z-index renders behind). The smallest absorbed bbox
    # — typically a badge/overlay — gets the highest z_order.
    sized: list[tuple[float, dict[str, Any], str]] = []
    for immediate_id in analysis.absolute_children:
        child_dict = id_to_dict.get(immediate_id)
        region_id = id_to_region.get(immediate_id)
        if child_dict is None or not region_id:
            continue
        bb = child_dict.get("absoluteBoundingBox") or {}
        try:
            area = float(bb.get("width", 0.0)) * float(bb.get("height", 0.0))
        except (TypeError, ValueError):
            area = 0.0
        sized.append((area, child_dict, region_id))
    sized.sort(key=lambda t: -t[0])

    agenda_index = {r.id: i for i, r in enumerate(ctx.agenda)}
    for z, (_area, child_dict, region_id) in enumerate(sized):
        ap = compute_absolute_pos(parent_node, child_dict, z_order=z)
        if ap is None:
            continue
        idx = agenda_index.get(region_id)
        if idx is None:
            continue
        ctx.agenda[idx].absolute_pos = ap


# --------------------------------------------------------------------------
# Internal: per-visit result.
# --------------------------------------------------------------------------


class _VisitResult:
    """Return value of :func:`_visit`.

    Kept as a tiny class (not a dataclass) for instance-creation
    speed — :func:`_visit` runs once per node, so allocating
    twice the overhead for a 5000-node tree is measurable.
    """

    __slots__ = ("captured_text", "region_id", "survived")

    def __init__(
        self,
        *,
        region_id: str | None,
        survived: bool,
        captured_text: str,
    ) -> None:
        self.region_id = region_id
        self.survived = survived
        self.captured_text = captured_text
