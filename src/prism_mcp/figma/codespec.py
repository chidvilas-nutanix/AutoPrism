"""Figma → Prism **code spec** assembly (roadmap P8).

P2-P6 resolve each region independently — identity (L1), props (L2), layout
(L3), tokens (L4), content (L5) — and hang the answers off
:class:`~prism_mcp.figma.models.MappedRegion` / :class:`LayoutNode`. The LLM
still has to *assemble* those scattered answers into nested JSX, which is
exactly where improvisation (and bugs) creep in.

P8 closes that gap: :func:`build_code_spec` folds the whole
:class:`~prism_mcp.figma.models.FigmaTreeMapping` into one **render-ready
tree** of :class:`PrismCodeNode`\\s — each carrying its final JSX tag, import
module, typed props, children/text, referenced tokens, and a confidence +
provenance + fallback note. The Cursor skill then renders it *verbatim*
instead of re-deriving the component for every node.

The assembler is **pure + deterministic** (no I/O, no network) and mirrors the
P5/P6 module shape:

* `_element_for(region, node)` — the **tag cascade** that decides which Prism
  element a node becomes (icon → catalog identity → high-conf pattern pick →
  page shell → layout primitive → mapper suggestion → ``<div>`` fallback).
* `build_code_spec(mapping)` — joins the agenda + layout forest by id, runs a
  conservative bbox-containment **re-parent** pass to recover the single page
  tree the walker flattens at pure-container boundaries, then collects deduped
  imports + stats.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from prism_mcp.figma.models import (
        FigmaTreeMapping,
        LayoutNode,
        MappedRegion,
    )

logger = logging.getLogger(__name__)

PRISM_MODULE = "@nutanix-ui/prism-reactjs"
"""The single import all Prism components / icons / layouts come from."""

_HIGH_CONF = 0.8
"""A ``primary_recommendation`` at/above this beats the layout primitive."""

# Host elements that carry no import. ``div`` is the explicit "no Prism
# component resolved" fallback; ``Fragment`` is a synthetic multi-root wrapper.
_HOST_TAGS: frozenset[str] = frozenset({"div", "Fragment"})


# --------------------------------------------------------------------------
# Models.
# --------------------------------------------------------------------------


class PrismProp(BaseModel):
    """One JSX prop on a code-spec node.

    Args:
        name (str): the Prism prop name (``type`` / ``label`` / ``itemGap``).
        value (str): the JSX-ready value.
        value_kind (Literal): how to emit it — ``expr`` → ``prop={value}``;
            ``string`` → ``prop="value"``; ``bool`` → ``prop`` / ``prop={false}``;
            ``slot`` → ``prop={<ChildNode/>}`` (``value`` is the child's
            ``figma_id``, rendered from the matching child in ``children``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    value_kind: Literal["expr", "string", "bool", "slot"]


class PrismCodeNode(BaseModel):
    """One render-ready JSX element in the code spec (roadmap P8).

    The fully-resolved join of every per-region layer: the chosen Prism
    ``tag`` + ``import_from``, typed ``props``, nested ``children`` (or
    ``text``), referenced design ``tokens``, and the provenance
    (``source`` / ``confidence``) + ``notes`` an LLM needs to render it
    verbatim and to know when to double-check.

    Args:
        figma_id (str): the source Figma node id (colon form) — the join key
            back to the agenda / layout tree.
        tag (str): the JSX element name (``Button`` / ``FlexLayout`` /
            ``MenuIcon`` / ``div`` / ``Fragment``).
        import_from (str | None): module to import ``tag`` from
            (``@nutanix-ui/prism-reactjs``); ``None`` for host/synthetic tags.
        props (list[PrismProp]): typed props to emit on the element.
        text (str | None): literal text children (when the region's content
            binds to ``children`` as a string); ``None`` otherwise.
        children (list[PrismCodeNode]): nested child elements, in render order.
        slot (str | None): when this node fills a *named* parent prop (a page
            shell's ``header`` / ``body`` / …), the prop name; ``None`` for a
            normal flow child.
        flex_grow (bool): render wrapped in ``<FlexItem flexGrow="1">`` (the
            parent flex container marked this child as filling — P4).
        source (str): how ``tag`` was chosen — ``icon`` / ``catalog`` /
            ``pattern`` / ``shell`` / ``layout`` / ``mapper`` / ``fallback``.
        confidence (float): 0-1 trust in ``tag``.
        tokens (list[str]): Prism design-token names this node references
            (background / border color + typography style).
        notes (list[str]): short flags (a ``<div>`` fallback reason, a
            composite that warrants a ``map_figma_node`` example, …).
    """

    model_config = ConfigDict(extra="forbid")

    figma_id: str
    tag: str
    import_from: str | None = None
    props: list[PrismProp] = Field(default_factory=list)
    text: str | None = None
    children: list[PrismCodeNode] = Field(default_factory=list)
    slot: str | None = None
    flex_grow: bool = False
    source: str = "fallback"
    confidence: float = 0.0
    tokens: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PrismImport(BaseModel):
    """One deduped import line for the generated file.

    Args:
        component (str): the imported name (``Button``).
        module (str): the module path (``@nutanix-ui/prism-reactjs``).
    """

    model_config = ConfigDict(extra="forbid")

    component: str
    module: str


class PrismCodeSpec(BaseModel):
    """The P8 deliverable — a render-ready tree for one Figma page.

    Args:
        roots (list[PrismCodeNode]): top-level elements in render order. One
            after the containment re-parent pass for a well-formed page; more
            when the page has spatially disjoint top-level frames.
        imports (list[PrismImport]): deduped, sorted import lines covering
            every Prism ``tag`` referenced in the tree.
        tokens (dict[str, str]): ``hex → token-name`` (passed through from the
            walker) so the generator substitutes tokens for raw hexes.
        stats (dict[str, int]): counts — ``nodes`` / ``resolved`` /
            ``fallbacks`` / ``roots`` / ``imports`` / ``max_depth``.
        warnings (list[str]): walker warnings + spec-assembly observations.
    """

    model_config = ConfigDict(extra="forbid")

    roots: list[PrismCodeNode] = Field(default_factory=list)
    imports: list[PrismImport] = Field(default_factory=list)
    tokens: dict[str, str] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


PrismCodeNode.model_rebuild()


# --------------------------------------------------------------------------
# Element resolution — the tag cascade.
# --------------------------------------------------------------------------


class _Element(BaseModel):
    """Internal: the tag-level decision for one node (pre-children)."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    import_from: str | None
    source: str
    confidence: float
    props: list[PrismProp] = Field(default_factory=list)
    text: str | None = None
    tokens: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# Composite families whose canonical JSX is non-trivial (config-driven
# columns / items / sections, not nested children) — flag them so the skill
# pulls an idiomatic example via ``map_figma_node`` instead of nesting the
# decomposed sub-parts as raw children. Covers both the catalog's v2 family
# names (``Tables`` / ``Navigation``) and the prop-schema names (``Table`` …).
_COMPOSITE_FAMILIES: frozenset[str] = frozenset(
    {
        "Table",
        "Tables",
        "Navigation",
        "Menu",
        "Modal",
        "Tabs",
        "Form",
        "FormSection",
        "Select",
        "Dropdown",
        "Accordion",
    }
)


def _region_props(region: MappedRegion) -> tuple[list[PrismProp], list[str]]:
    """Typed props (P3) + content-binding attribute props (P6) for a region.

    Returns ``(props, referenced_tokens)``. ``children``-kind content bindings
    are handled by the caller as ``text``, not here.
    """
    props: list[PrismProp] = []
    for rp in region.prism_props:
        props.append(
            PrismProp(name=rp.prop, value=rp.value, value_kind=rp.value_kind)
        )
    binding = region.content_binding
    if binding is not None and binding.value_kind != "children":
        props.append(
            PrismProp(name=binding.prop, value=binding.value, value_kind="string")
        )
    return props, []


def _region_tokens(region: MappedRegion) -> list[str]:
    """The Prism design tokens a region references (P5 color + typography)."""
    tokens: list[str] = []
    box = region.box_style
    if box.background_token:
        tokens.append(box.background_token)
    if box.border_token:
        tokens.append(box.border_token)
    if region.typography and region.typography.style_token:
        tokens.append(region.typography.style_token)
    return tokens


def _layout_props(node: LayoutNode) -> list[PrismProp]:
    """String-valued props for a Prism layout / container primitive (P4)."""
    layout = node.prism_layout
    if layout is None:
        return []
    return [
        PrismProp(name=k, value=v, value_kind="string")
        for k, v in layout.props.items()
    ]


def _element_for(
    region: MappedRegion | None, node: LayoutNode | None
) -> _Element:
    """Resolve the JSX element for one node (the P8 tag cascade).

    Priority (highest trust first):

    1. **icon** — a resolved Prism ``*Icon`` (P6).
    2. **catalog** — the Tier-1 ``componentKey`` identity (P2/P3, authoritative).
    3. **pattern** — a ``primary_recommendation`` at confidence ≥ 0.8 (a
       deterministic role/pattern pick, e.g. ``kpi-tile`` → ``Tile``).
    4. **shell** — a page-level ``MainPageLayout`` / … (P4 follow-up).
    5. **layout** — a ``FlexLayout`` / ``StackingLayout`` / ``ContainerLayout``
       primitive (P4).
    6. **mapper** — the fuzzy ranker's ``suggested_component_name`` (any conf).
    7. **fallback** — ``<div>`` (carries a note so the gap is visible).

    Args:
        region (MappedRegion | None): the agenda row, when this id is a region.
        node (LayoutNode | None): the layout-tree node, when present.

    Returns:
        _Element: the tag-level decision (children resolved by the caller).
    """
    tokens = _region_tokens(region) if region is not None else []

    # 1. Icon — a leaf glyph; no children, no text.
    if region is not None and region.prism_icon is not None:
        return _Element(
            tag=region.prism_icon.prism_component,
            import_from=PRISM_MODULE,
            source="icon",
            confidence=region.prism_icon.confidence,
            tokens=tokens,
        )

    props: list[PrismProp] = []
    text: str | None = None
    notes: list[str] = []
    if region is not None:
        props, _ = _region_props(region)
        binding = region.content_binding
        if binding is not None and binding.value_kind == "children":
            text = binding.value

    # 2. Catalog identity (Tier-1, authoritative).
    res = region.prism_resolution if region is not None else None
    if res is not None and res.is_mapped:
        if res.prism_component in _COMPOSITE_FAMILIES:
            notes.append(
                f"composite '{res.prism_component}' — pull canonical JSX via "
                "map_figma_node"
            )
        return _Element(
            tag=res.prism_component,
            import_from=PRISM_MODULE,
            source="catalog",
            confidence=res.confidence,
            props=props,
            text=text,
            tokens=tokens,
            notes=notes,
        )

    # 3. High-confidence pattern recommendation.
    mapping = region.mapping if region is not None else None
    if (
        mapping is not None
        and mapping.primary_recommendation
        and mapping.primary_recommendation_confidence >= _HIGH_CONF
    ):
        return _Element(
            tag=mapping.primary_recommendation,
            import_from=PRISM_MODULE,
            source="pattern",
            confidence=mapping.primary_recommendation_confidence,
            props=props,
            text=text,
            tokens=tokens,
            notes=notes,
        )

    # 4. Page shell (geometry-detected page skeleton).
    if node is not None and node.prism_shell is not None:
        return _Element(
            tag=node.prism_shell.component,
            import_from=PRISM_MODULE,
            source="shell",
            confidence=node.prism_shell.confidence,
            tokens=tokens,
        )

    # 5. Layout primitive (FlexLayout / StackingLayout / ContainerLayout).
    if node is not None and node.prism_layout is not None:
        return _Element(
            tag=node.prism_layout.component,
            import_from=PRISM_MODULE,
            source="layout",
            confidence=node.prism_layout.confidence,
            props=_layout_props(node),
            tokens=tokens,
        )

    # 6. Fuzzy mapper suggestion.
    if mapping is not None and mapping.suggested_component_name:
        if mapping.suggested_component_name in _COMPOSITE_FAMILIES:
            notes.append(
                f"composite '{mapping.suggested_component_name}' — pull "
                "canonical JSX via map_figma_node"
            )
        return _Element(
            tag=mapping.suggested_component_name,
            import_from=PRISM_MODULE,
            source="mapper",
            confidence=mapping.primary_recommendation_confidence,
            props=props,
            text=text,
            tokens=tokens,
            notes=notes,
        )

    # 7. Fallback — a bare host div, flagged so the gap is auditable.
    notes.append("no Prism component resolved — emitted <div> fallback")
    return _Element(
        tag="div",
        import_from=None,
        source="fallback",
        confidence=0.0,
        props=props,
        text=text,
        tokens=tokens,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Containment re-parent — recover the single page tree.
# --------------------------------------------------------------------------


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2]) * max(0.0, bbox[3])


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tol: float = 1.0,
) -> bool:
    """``True`` when ``outer`` fully contains ``inner`` (with a px tolerance)."""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tol
        and iy >= oy - tol
        and ix + iw <= ox + ow + tol
        and iy + ih <= oy + oh + tol
        and _area(outer) > _area(inner)
    )


def _reparent_roots(
    roots: list[str],
    nodes: dict[str, LayoutNode],
    child_to_parent: dict[str, str],
) -> list[str]:
    """Re-nest orphan roots into their tightest containing node.

    The walker flattens the tree at pure-container boundaries (a container that
    emits no region returns no id to its parent), leaving several spatially
    nested nodes as flat roots. This pass restores the dropped links
    deterministically: each root is attached to the **smallest** other node
    whose bbox strictly contains it, mutating ``child_to_parent`` and the
    parent's children order. Conservative — a root with no unambiguous
    container stays a root (no data is ever lost).

    Args:
        roots (list[str]): candidate root ids, in reading order.
        nodes (dict[str, LayoutNode]): id → layout node.
        child_to_parent (dict[str, str]): the live parent map (mutated).

    Returns:
        list[str]: the surviving top-level roots, in reading order.
    """
    survivors: list[str] = []
    for rid in roots:
        node = nodes.get(rid)
        if node is None:
            survivors.append(rid)
            continue
        best: str | None = None
        best_area = float("inf")
        for cand_id, cand in nodes.items():
            if cand_id == rid:
                continue
            if not _contains(cand.bbox, node.bbox):
                continue
            area = _area(cand.bbox)
            if area < best_area:
                best, best_area = cand_id, area
        if best is None:
            survivors.append(rid)
        else:
            child_to_parent[rid] = best
    return survivors


# --------------------------------------------------------------------------
# Assembly.
# --------------------------------------------------------------------------

_MAX_SPEC_DEPTH = 40
"""Defensive recursion guard (the walker's own ``max_depth`` is ≤ 20)."""


def build_code_spec(mapping: FigmaTreeMapping) -> PrismCodeSpec:
    """Fold a walker mapping into a render-ready :class:`PrismCodeSpec` (P8).

    Pure transform over the already-computed
    :class:`~prism_mcp.figma.models.FigmaTreeMapping` — the walker and
    ``map_figma_node`` are untouched. Joins the agenda + layout forest by id,
    re-parents orphan roots by bbox containment, resolves each node's JSX
    element via :func:`_element_for`, then collects deduped imports + stats.

    Args:
        mapping (FigmaTreeMapping): the walker's full output.

    Returns:
        PrismCodeSpec: the render-ready tree + imports + tokens + stats.
    """
    regions: dict[str, MappedRegion] = {r.id: r for r in mapping.agenda}
    nodes: dict[str, LayoutNode] = {n.id: n for n in mapping.layout_tree}

    # Parent map from the layout forest's children_ids (each id is referenced
    # by at most one node — the walker never builds a diamond).
    child_to_parent: dict[str, str] = {}
    children_order: dict[str, list[str]] = {}
    for n in mapping.layout_tree:
        kept = [c for c in n.children_ids if c in nodes or c in regions]
        children_order[n.id] = kept
        for c in kept:
            child_to_parent.setdefault(c, n.id)

    referenced = set(child_to_parent)
    roots = [n.id for n in mapping.layout_tree if n.id not in referenced]
    roots = _reparent_roots(roots, nodes, child_to_parent)

    # Rebuild children order after re-parenting (re-attached roots append to
    # their new parent, preserving each parent's original reading order).
    reparented: dict[str, list[str]] = {k: list(v) for k, v in children_order.items()}
    for cid, pid in child_to_parent.items():
        if cid not in reparented.get(pid, []):
            reparented.setdefault(pid, []).append(cid)

    def _build(node_id: str, depth: int, seen: frozenset[str]) -> PrismCodeNode:
        region = regions.get(node_id)
        layout = nodes.get(node_id)
        element = _element_for(region, layout)

        spec_node = PrismCodeNode(
            figma_id=node_id,
            tag=element.tag,
            import_from=element.import_from,
            props=element.props,
            text=element.text,
            source=element.source,
            confidence=round(element.confidence, 3),
            tokens=element.tokens,
            notes=element.notes,
        )

        # Children — recurse the (possibly re-parented) child order, guarding
        # against cycles and the depth cap.
        if depth < _MAX_SPEC_DEPTH:
            next_seen = seen | {node_id}
            fill_ids = (
                set(layout.prism_layout.fill_child_ids)
                if layout is not None and layout.prism_layout is not None
                else set()
            )
            slot_of: dict[str, str] = {}
            if layout is not None and layout.prism_shell is not None:
                slot_of = {v: k for k, v in layout.prism_shell.slots.items()}
            for child_id in reparented.get(node_id, []):
                if child_id in next_seen:
                    continue
                child = _build(child_id, depth + 1, next_seen)
                if child_id in fill_ids:
                    child.flex_grow = True
                if child_id in slot_of:
                    child.slot = slot_of[child_id]
                spec_node.children.append(child)
            # Text leaves don't keep a stray child list; once a node has real
            # children its text (if any) was a mis-capture — drop it.
            if spec_node.children:
                spec_node.text = None

        return spec_node

    root_nodes = [_build(rid, 0, frozenset()) for rid in roots]
    # Collapse pointless ``<div>`` wrappers so the output honours P8's
    # "zero extra divs" metric (a fallback div with one child + no identity is
    # not a real element). Done after the build so it sees the final children.
    root_nodes = _prune_redundant_wrappers(root_nodes)

    stats = _count_nodes(root_nodes)
    imports = _collect_imports(root_nodes)
    stats["roots"] = len(root_nodes)
    stats["imports"] = len(imports)
    stats["max_depth"] = _tree_depth(root_nodes)

    warnings = list(mapping.warnings)
    if stats["fallbacks"]:
        warnings.append(
            f"{stats['fallbacks']} node(s) had no Prism component and fell "
            "back to <div> — review or drill in with map_figma_node"
        )
    if stats["roots"] > 1:
        warnings.append(
            f"{stats['roots']} top-level roots — wrap them in the page shell "
            "or a fragment in render order"
        )

    return PrismCodeSpec(
        roots=root_nodes,
        imports=imports,
        tokens=dict(mapping.tokens),
        stats=stats,
        warnings=warnings,
    )


def _is_bare_fallback(node: PrismCodeNode) -> bool:
    """``True`` when a fallback ``<div>`` carries no identity of its own.

    No props, text, tokens, slot duty, or flexGrow — it contributes nothing but
    a tag. Such a node is collapsible (when it has one child) or droppable (when
    it has none).
    """
    return (
        node.tag in _HOST_TAGS
        and node.source == "fallback"
        and not node.props
        and not node.text
        and not node.tokens
        and node.slot is None
        and not node.flex_grow
    )


def _prune_redundant_wrappers(
    roots: list[PrismCodeNode],
) -> list[PrismCodeNode]:
    """Remove scaffolding ``<div>``\\s so the output honours "zero extra divs".

    Bottom-up, two collapses on a bare fallback div (one with no identity of its
    own — see :func:`_is_bare_fallback`):

    * **0 children** → dropped entirely (an empty ``<div/>`` renders nothing).
    * **1 child** → collapsed into that child (the child keeps its own slot /
      flexGrow).

    A bare fallback with ≥2 children is kept — it is a real anonymous grouping
    the walker could not name, and dropping it would re-flatten its children.
    Done after the build so it sees the final child lists; cascades naturally
    (a wrapper that loses all but one child this pass collapses next).
    """

    def _prune_children(node: PrismCodeNode) -> PrismCodeNode:
        kept: list[PrismCodeNode] = []
        for child in node.children:
            pruned = _prune_children(child)
            if _is_bare_fallback(pruned) and not pruned.children:
                continue  # drop empty scaffolding
            if _is_bare_fallback(pruned) and len(pruned.children) == 1:
                kept.append(pruned.children[0])  # collapse single-child wrapper
                continue
            kept.append(pruned)
        node.children = kept
        return node

    pruned_roots = [_prune_children(r) for r in roots]
    survivors: list[PrismCodeNode] = []
    for root in pruned_roots:
        if _is_bare_fallback(root) and not root.children:
            continue
        if _is_bare_fallback(root) and len(root.children) == 1:
            survivors.append(root.children[0])
            continue
        survivors.append(root)
    return survivors


def _count_nodes(roots: list[PrismCodeNode]) -> dict[str, int]:
    """Tally ``nodes`` / ``resolved`` / ``fallbacks`` over the final tree."""
    counts = {"nodes": 0, "resolved": 0, "fallbacks": 0}

    def _visit(node: PrismCodeNode) -> None:
        counts["nodes"] += 1
        if node.source == "fallback":
            counts["fallbacks"] += 1
        else:
            counts["resolved"] += 1
        for child in node.children:
            _visit(child)

    for root in roots:
        _visit(root)
    return counts


def _collect_imports(roots: list[PrismCodeNode]) -> list[PrismImport]:
    """Walk the tree and return deduped, sorted Prism imports."""
    seen: dict[tuple[str, str], PrismImport] = {}

    def _visit(node: PrismCodeNode) -> None:
        if node.import_from and node.tag not in _HOST_TAGS:
            key = (node.tag, node.import_from)
            if key not in seen:
                seen[key] = PrismImport(
                    component=node.tag, module=node.import_from
                )
        for child in node.children:
            _visit(child)

    for root in roots:
        _visit(root)
    return [seen[k] for k in sorted(seen)]


def _tree_depth(roots: list[PrismCodeNode]) -> int:
    """Return the maximum nesting depth of the spec tree (roots = depth 1)."""

    def _depth(node: PrismCodeNode) -> int:
        if not node.children:
            return 1
        return 1 + max(_depth(c) for c in node.children)

    return max((_depth(r) for r in roots), default=0)
