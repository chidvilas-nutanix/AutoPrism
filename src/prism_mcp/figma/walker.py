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
import re
from collections import Counter
from collections.abc import Callable
from typing import Any, TypeAlias

from prism_mcp.figma.filter import (
    DropReason,
    pass_1_visible,
    pass_2_invisible_decoration,
    pass_3_mappable_type,
    pass_4_collapse_passthrough,
    pass_6_tiny_decorative,
)
from prism_mcp.figma.models import (
    DroppedNode,
    FigmaTreeMapping,
    LayoutNode,
    MappedRegion,
)
from prism_mcp.figma.patterns import PATTERNS, PatternMatch
from prism_mcp.figma.routing import (
    FrameRole,
    RouterDecision,
    classify_frame_role,
    route_node,
)
from prism_mcp.figma.types import MAPPABLE_TYPES
from prism_mcp.figma.utils import (
    bbox_tuple_from_dict,
    extract_visible_hexes,
    get_characters,
    iter_children,
)
from prism_mcp.workflow.figma_mapping import FigmaNodeMapping

logger = logging.getLogger(__name__)


class WalkerError(RuntimeError):
    """Raised on safety-rail trips (``max_depth`` / ``max_nodes``).

    The walker bails fast on these rather than silently producing
    truncated output. See design doc §4.7.
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

    # Trim agenda if over soft cap.
    if len(ctx.agenda) > ctx.max_agenda:
        ctx.warnings.append(
            f"agenda_size={len(ctx.agenda)} exceeded max_agenda={ctx.max_agenda}; "
            "consider raising max_agenda or grouping sub-regions in composition"
        )

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
    """Shared mutable state for a single :func:`walk_tree` invocation."""

    __slots__ = (
        "_jsx_slice_cache",
        "agenda",
        "dropped",
        "input_nodes",
        "layout_tree",
        "map_figma_node_fn",
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
    if icon_match is not None:
        return _emit_pattern_region(node, ctx, parent_chain, icon_match)

    # ---- Pattern detection at the cluster level (stat-list, etc.)
    # ALSO runs before recursing — the matched pattern absorbs the
    # descendants.
    cluster_match = _try_cluster_patterns(node)
    if cluster_match is not None:
        return _emit_pattern_region(node, ctx, parent_chain, cluster_match)

    # ---- Recurse into children. Capture their results.
    own_name = str(node.get("name", ""))
    new_parent_chain = [*parent_chain, own_name] if own_name else parent_chain

    significant_children: list[dict[str, Any]] = []
    child_region_ids: list[str] = []
    captured_text_parts: list[str] = []
    for child in iter_children(node):
        result = _visit(
            child, ctx, depth=depth + 1, parent_chain=new_parent_chain
        )
        if not result.survived:
            continue
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
        )

    if node_type == "FRAME" and role_hint is FrameRole.composed_region:
        return _emit_simple_region(
            node,
            ctx,
            parent_chain,
            child_region_ids,
            captured_text_parts,
            role="composed-region",
        )

    # Layout containers + pattern_cluster fallback + generic GROUPs
    # don't emit a region themselves; they just pass through.
    text_for_parent = "\n".join(captured_text_parts)

    # Add this node to the layout tree IF it has child regions —
    # keeps the layout tree free of empty wrappers.
    if child_region_ids:
        ctx.layout_tree.append(
            LayoutNode(
                id=str(node.get("id", "")),
                name=own_name or node_type or "node",
                role="layout-container",
                bbox=bbox_tuple_from_dict(node.get("absoluteBoundingBox")),
                children_ids=child_region_ids,
            )
        )

    return _VisitResult(
        region_id=None,
        survived=True,
        captured_text=text_for_parent,
    )


# --------------------------------------------------------------------------
# Region emission helpers.
# --------------------------------------------------------------------------


def _emit_simple_region(
    node: dict[str, Any],
    ctx: _WalkContext,
    parent_chain: list[str],
    child_region_ids: list[str],
    captured_text_parts: list[str],
    *,
    role: str,
) -> _VisitResult:
    """Emit a non-pattern :class:`MappedRegion` for ``node``."""
    node_id = str(node.get("id", ""))
    name = str(node.get("name", "")) or node.get("type", "") or "region"

    text_content_for_region = "\n".join(captured_text_parts).strip() or None

    hex_colors = extract_visible_hexes(node)
    _seed_tokens(ctx, hex_colors)

    children_summary = _summarise_children(node)
    structural_hints = _structural_hints_for(node)

    mapping = _invoke_mapping_fn(
        ctx,
        node_name=name,
        node_type=str(node.get("type", "")),
        text_content=text_content_for_region,
        children_summary=children_summary,
        structural_hints=structural_hints,
        parent_chain=list(parent_chain),
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
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
        bbox=bbox_tuple_from_dict(node.get("absoluteBoundingBox")),
        parent_chain=list(parent_chain),
        content_slots=content_slots,
        structural_hints=structural_hints,
        children_summary=children_summary,
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        mapping=mapping,
    )
    ctx.agenda.append(region)
    ctx.layout_tree.append(
        LayoutNode(
            id=node_id or name,
            name=name,
            role=role,
            bbox=region.bbox,
            children_ids=list(child_region_ids),
        )
    )
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

    # Patterns provide their own text_content via content_slots —
    # the walker forwards the slot strings into map_figma_node.
    text_content_for_region = _content_slots_to_text(match.content_slots)

    mapping = _invoke_mapping_fn(
        ctx,
        node_name=name,
        node_type=str(node.get("type", "")),
        text_content=text_content_for_region,
        children_summary=match.children_summary,
        structural_hints=match.structural_hints,
        parent_chain=list(parent_chain),
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
    )

    region = MappedRegion(
        id=node_id or name,
        name=name,
        role=match.kind,
        bbox=bbox_tuple_from_dict(node.get("absoluteBoundingBox")),
        parent_chain=list(parent_chain),
        content_slots=dict(match.content_slots),
        structural_hints=list(match.structural_hints),
        children_summary=match.children_summary,
        hex_colors=hex_colors,
        reference_jsx_slice=ctx.slice_reference_jsx(node_id),
        mapping=mapping,
    )
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
    """
    for predicate in PATTERNS[1:]:
        match = predicate(node)
        if match is not None:
            return match
    return None


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


def _structural_hints_for(node: dict[str, Any]) -> list[str]:
    """Generate cheap structural hints for the BM25 query."""
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
) -> FigmaNodeMapping:
    """Invoke the curried :func:`map_figma_node` or return a stub.

    A ``None`` ``map_figma_node_fn`` is the fast-path for filter /
    pattern unit tests that don't want to stand up the library
    index.
    """
    if ctx.map_figma_node_fn is None:
        return FigmaNodeMapping(
            node_name=node_name,
            suggested_component_name=None,
        )
    return ctx.map_figma_node_fn(
        node_name=node_name,
        node_type=node_type or None,
        reference_code=reference_jsx_slice,
        text_content=text_content,
        children_summary=children_summary or None,
        structural_hints=structural_hints or None,
        parent_chain=parent_chain or None,
        hex_colors=hex_colors or None,
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
