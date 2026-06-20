"""Pydantic shapes for the Figma → Prism page walker.

All four boundary shapes (`MapFigmaTreeInput`, `MappedRegion`,
`LayoutNode`, `DroppedNode`, `FigmaTreeMapping`) use
``ConfigDict(extra="forbid")`` to match the style of
:mod:`prism_mcp.figma_mapping` and to make schema drift loud:
adding an unrecognised field at the MCP boundary produces a clear
``ValidationError`` instead of silently ignored input.

See design doc §4.1 (input), §4.2 (output), and §10.1.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from prism_mcp.figma_mapping import FigmaNodeMapping

# --------------------------------------------------------------------------
# Input shape — what the MCP tool boundary accepts.
# --------------------------------------------------------------------------


class MapFigmaTreeInput(BaseModel):
    """Input to the public ``map_figma_tree`` MCP tool.

    Provided by the caller (typically the ``figma-page-to-prism``
    Cursor skill). All optional fields default such that omitting
    them still yields a useful result — just with weaker signal.

    Args:
        node_url (str): Figma node URL of the form
            ``https://www.figma.com/design/:fileKey/.../?node-id=:nodeId``.
            URL form uses ``-`` between id parts (``"624-6826"``);
            internal/JSON form uses ``:`` (``"624:6826"``). We
            accept both and normalise to colon form internally.
        reference_jsx (str | None): React+Tailwind JSX that the
            caller already obtained from the Figma plugin's
            ``get_design_context`` for **this** ``node_url``.
            Optional but strongly recommended — it is the
            strongest semantic signal we have. The walker
            mechanically slices it by node-ID comments and passes
            per-region snippets to ``map_figma_node``.
        variable_defs (dict[str, str] | None): hex → token-name
            map the caller optionally obtained from the Figma
            plugin's ``get_variable_defs``. Improves the
            ``tokens`` output by using designer-named tokens
            instead of just colour-distance approximations.
        figma_token (str | None): personal access token for the
            REST fetch. Defaults to env ``FIGMA_TOKEN``. The MCP
            schema declares it as a secret so Cursor never logs
            it back to the user.
        max_depth (int): hard cap on traversal depth. Default 20.
        max_nodes (int): hard cap on total nodes visited.
            Default 5000.
        max_agenda (int): soft cap on agenda size. Default 100.
            When exceeded the walker emits a warning and groups
            the smallest siblings into a generic "container" row.
            The default was bumped from 50 → 100 after the
            b213fac1 / 753:27069 trace showed real X-Ray pages
            routinely truncating 50+ semantically-meaningful
            regions (Status/Tag, Subpage, Icon/Actions/Edit, …);
            losing them forced the LLM to hand-roll their
            equivalents. Library callers of
            :func:`prism_mcp.figma.walk_tree` still default to
            50 — the bump is tool-input-only so the unit-fixture
            tests stay pinned at their existing baselines.
        bypass_cache (bool): if True, skip the disk cache for the
            REST fetch (useful when the user has just edited the
            design and wants a fresh pull). Default False.
        figma_depth (int | None): override the ``depth`` query
            parameter the fetcher sends to Figma's REST API.
            ``None`` (default) uses the fetcher's tuned default
            (12 — enough for nearly every Nutanix design we have
            inspected). Set to a higher value for unusually deeply
            nested files; set lower to reduce payload size when
            you know the subtree is shallow. Values < 1 are
            clamped to 1 by the fetcher.
        response_detail (Literal["lean", "full"]): how much of the
            walker's output to ship back over the MCP boundary.
            Defaults to ``"lean"`` — a trimmed agenda that keeps the
            cheap *descriptive* fields (id / name / role / bbox /
            box_style / content_slots / structural_hints /
            hex_colors / …) but drops the heavy per-row *retrieval*
            payload (raw JSX ``examples``, ``a11y_blocks``, the full
            ``candidates`` rows, ``token_mappings``, …) and replaces
            the full ``dropped`` audit list with a per-reason
            ``dropped_summary`` count map. The LLM can recover the
            full detail for any single region on demand via
            ``map_figma_node``. Pass ``"full"`` to get today's
            complete :class:`FigmaTreeMapping` payload in one shot
            (byte-for-byte identical to the pre-lean behaviour) —
            useful for debugging, golden captures, or offline
            batch processing where context budget is not a concern.
            See :meth:`FigmaTreeMapping.to_lean_response` for the
            exact lean shape.
    """

    model_config = ConfigDict(extra="forbid")

    node_url: str
    reference_jsx: str | None = None
    variable_defs: dict[str, str] | None = None
    figma_token: str | None = None
    max_depth: int = 20
    max_nodes: int = 5000
    max_agenda: int = 100
    bypass_cache: bool = False
    figma_depth: int | None = None
    response_detail: Literal["lean", "full"] = "lean"


# --------------------------------------------------------------------------
# Output shapes — the FigmaTreeMapping aggregate and its parts.
# --------------------------------------------------------------------------


class DroppedNode(BaseModel):
    """One entry in the audit trail.

    Every node the walker discards lands here so the LLM (and the
    human reviewer) can see *why* something didn't surface as a
    mapping decision. See design doc §4.8 for the enumerated
    ``reason`` values.

    Args:
        id (str): Figma node id (colon form).
        name (str): the layer name (often auto-generated, e.g.
            ``"Rectangle 2"``).
        type (str): the Figma SceneNode type (``"RECTANGLE"``,
            ``"GROUP"``, etc.).
        reason (str): machine-readable enum — one of
            ``"explicit_hidden"``, ``"invisible_decoration"``,
            ``"non_design_type"``,
            ``"same_bbox_passthrough_collapsed"``,
            ``"icon_internal"``, ``"redundant_inner_instance"``,
            ``"tiny_decorative"``, ``"captured_as_content_slot"``,
            ``"folded_into_pattern"``, ``"unknown_type_fallback"``.
        detail (str): human-readable elaboration. Empty when the
            reason itself is fully self-describing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: str
    reason: str
    detail: str = ""


class LayoutAnalysis(BaseModel):
    """Deterministic flex / grid / stack inference for one parent
    node's immediate children.

    The walker fills this from
    :func:`prism_mcp.figma.layout_inference.analyze_layout` whenever a
    :class:`LayoutNode` has two or more children. The downstream
    generator reads the field to render the parent with the right
    ``flexDirection`` / ``gap`` / ``justifyContent`` / ``alignItems``
    instead of having to re-derive geometry from raw bboxes.

    Two trust sources feed this:

    * Figma's own ``layoutMode`` / ``itemSpacing`` /
      ``primaryAxisAlignItems`` / ``counterAxisAlignItems`` /
      ``paddingTop|Right|Bottom|Left`` (the auto-layout fast path).
      When present we trust them verbatim and emit ``confidence=1.0``
      with ``rationale="figma_auto_layout"``.
    * The Figma-Context-MCP layout-detection algorithm
      (https://github.com/1yhy/Figma-Context-MCP/blob/main/docs/en/layout-detection.md)
      for the absolute-positioned fallback. We score row-vs-column
      from bbox geometry and require the winner to exceed 0.4, else
      collapse to ``direction="stack"``.

    Args:
        direction (Literal[...] | None): one of ``"row"`` /
            ``"column"`` / ``"grid"`` / ``"stack"`` / ``"single"`` or
            ``None`` when the parent has no children. ``"stack"``
            means the children overlap enough that flex flow does not
            make sense — every child needs ``position: absolute``.
            ``"single"`` means exactly one child, no flow required.
        justify_content (Literal[...] | None): main-axis alignment in
            CSS terms (``"start"`` / ``"end"`` / ``"center"`` /
            ``"space-between"`` / ``"space-around"`` /
            ``"space-evenly"``). ``None`` when not confidently
            inferable.
        align_items (Literal[...] | None): cross-axis alignment in
            CSS terms (``"start"`` / ``"end"`` / ``"center"`` /
            ``"stretch"`` / ``"baseline"``). ``None`` when not
            confidently inferable.
        gap (float | None): spacing between siblings in pixels,
            rounded to the nearest 4-px grid. ``None`` when
            ``gap_consistent`` is False (per-child margins should be
            used instead).
        gap_consistent (bool): True when the sibling gap standard
            deviation is within 20% of the mean — i.e. the children
            are evenly spaced. False signals "use per-child margins".
        confidence (float): 0-1 from the row/column scoring formula.
            ``1.0`` for the auto-layout fast path; ``0.0`` for
            children with no detectable structure.
        absolute_children (list[str]): ids of children that overlap
            another sibling (IoU > 0.1 measured with
            ``min(area_a, area_b)`` denominator) and must render
            with ``position: absolute``. ``MappedRegion.absolute_pos``
            on each of these ids carries the per-child offset.
        flow_children (list[str]): ids of children that flow in
            ``direction`` order. Empty when ``direction="stack"``.
        rationale (str): one-line human-readable explanation
            (``"row score 0.78 (3 children left→right with 16 px gaps,
            top-aligned within 2 px)"``). Empty for auto-layout
            fast-path nodes since the source is self-describing.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Literal["row", "column", "grid", "stack", "single"] | None = (
        None
    )
    justify_content: (
        Literal[
            "start",
            "end",
            "center",
            "space-between",
            "space-around",
            "space-evenly",
        ]
        | None
    ) = None
    align_items: (
        Literal["start", "end", "center", "stretch", "baseline"] | None
    ) = None
    gap: float | None = None
    gap_consistent: bool = True
    confidence: float = 0.0
    absolute_children: list[str] = Field(default_factory=list)
    flow_children: list[str] = Field(default_factory=list)
    rationale: str = ""


class AbsolutePos(BaseModel):
    """Per-child absolute-position offset within its parent.

    The walker attaches this to :class:`MappedRegion.absolute_pos`
    when (and only when) the region's id appears in some parent's
    :attr:`LayoutAnalysis.absolute_children`. The generator should
    render the parent with ``position: relative`` and the child with
    ``position: absolute; top: ...; left: ...``.

    Args:
        top (float): vertical offset from the parent's bbox top in
            pixels. Always non-negative; clamped to 0 if the child
            overflows above the parent.
        left (float): horizontal offset from the parent's bbox left
            in pixels. Always non-negative; clamped to 0 if the child
            overflows left of the parent.
        width (float): child bbox width in pixels.
        height (float): child bbox height in pixels.
        z_order (int): stacking order rank — 0 is the bottom-most
            (largest area), higher integers are on top (smaller area
            wins because designers stack badges / overlays on top of
            their host). Mirrors the Figma-Context-MCP convention.
    """

    model_config = ConfigDict(extra="forbid")

    top: float
    left: float
    width: float
    height: float
    z_order: int


class LayoutNode(BaseModel):
    """One node in the pruned layout tree.

    The layout tree answers the question *"how do these regions
    nest in JSX?"* — just enough structure for the LLM to write
    ``<Parent>{children}</Parent>``. The full per-region detail
    lives in the agenda (see :class:`MappedRegion`).

    Args:
        id (str): matches a :class:`MappedRegion.id` in the
            agenda. Always the colon-form Figma id.
        name (str): the chosen layer name.
        role (str): one of the role strings emitted by the
            walker (see :class:`MappedRegion.role`).
        bbox (tuple[float, float, float, float]): absolute
            bounding box ``(x, y, w, h)`` in Figma's design
            coordinate system. Stored as a tuple (not a dict)
            so JSON round-trips compactly.
        children_ids (list[str]): ids of sub-regions that nest
            inside this one. Order is the walker's DFS order,
            which mirrors the Figma layer order.
        layout (LayoutAnalysis | None): deterministic flex / grid /
            stack inference over the immediate children's bboxes.
            ``None`` when the node has fewer than two children OR
            when the inference produced no useful signal. See
            :class:`LayoutAnalysis` for the full field reference.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    role: str
    bbox: tuple[float, float, float, float]
    children_ids: list[str] = Field(default_factory=list)
    layout: LayoutAnalysis | None = None


class BoxStyle(BaseModel):
    """The CSS-aligned box style of one :class:`MappedRegion`.

    The walker fills this from
    :func:`prism_mcp.figma.utils.extract_box_style` at region-
    emission time, so the LLM downstream receives background colour,
    border, corner radius, padding, gap and shadow as structured
    facts instead of having to re-derive them from raw Figma JSON.

    All fields are optional and absent by default — the model is
    constructed with whichever keys the helper decided to emit, and
    Pydantic's default JSON serialisation will drop ``None`` values
    so empty styles round-trip as ``{}``.

    Property names are deliberately CSS-aligned (``background_color``
    rather than ``fills``, ``corner_radius`` rather than
    ``cornerRadius``, ``padding`` as a ``(T, R, B, L)`` tuple in CSS
    shorthand order) to maximise overlap with the conventions the
    LLM already knows. This mirrors the approach used by the
    open-source Figma-Context-MCP and figma-to-code-mcp projects.

    Args:
        background_color (str | None): hex of the first visible
            SOLID fill (``"#EDF0F2"``). ``None`` when the node has
            no visible solid fill — gradients and image fills are
            intentionally not surfaced here (they'd need richer
            structure than a single hex).
        border_color (str | None): hex of the first visible SOLID
            stroke.
        border_width (float | None): stroke weight in pixels,
            present only when ``border_color`` is set and the
            REST API reports a positive ``strokeWeight``.
        corner_radius (float | list[float] | None): single float
            when all corners share the same radius;
            ``[tl, tr, br, bl]`` when corners differ. ``None`` for
            sharp-cornered FRAMEs (cleanest agenda output).
        padding (tuple[float, float, float, float] | None): inset
            in ``(top, right, bottom, left)`` order, matching CSS
            shorthand. Auto-layout FRAMEs pull from Figma's own
            ``paddingTop`` / ``paddingRight`` / ``paddingBottom`` /
            ``paddingLeft`` fields; absolute-positioned FRAMEs
            have padding inferred from parent-child bbox offsets
            per :func:`prism_mcp.figma.utils.infer_padding`.
        gap (float | None): ``itemSpacing`` for auto-layout
            FRAMEs. Inferred-padding regions never set this — we
            don't try to infer flex gap from absolute coordinates
            because the chance of a wrong call is high.
        layout_mode (str | None): one of ``"HORIZONTAL"`` /
            ``"VERTICAL"`` / ``"GRID"`` when the FRAME uses
            Figma auto-layout. ``None`` for absolute-positioned
            FRAMEs (the walker still infers padding for those).
        has_shadow (bool): True iff the node has at least one
            visible drop / inner shadow effect. Kept as a bool
            (rather than a full effect spec) because the
            downstream generator usually only needs the binary
            "is this card elevated?".
        opacity (float | None): node-level opacity in
            ``(0, 1.0)`` when meaningfully transparent. ``None``
            when fully opaque (the common case) so the agenda
            stays compact.
    """

    model_config = ConfigDict(extra="forbid")

    background_color: str | None = None
    border_color: str | None = None
    border_width: float | None = None
    corner_radius: float | list[float] | None = None
    padding: tuple[float, float, float, float] | None = None
    gap: float | None = None
    layout_mode: str | None = None
    has_shadow: bool = False
    opacity: float | None = None


class MappedRegion(BaseModel):
    """One row in the agenda — one logical Prism component decision.

    The walker emits exactly one ``MappedRegion`` per node it has
    decided is a candidate for a Prism component. Everything else
    is either folded into this region's ``content_slots`` /
    ``structural_hints`` / ``hex_colors`` or pushed to the
    ``dropped`` audit list.

    Args:
        id (str): primary Figma node id (colon form). This is the
            id the LLM should anchor on; ``aliased_ids`` carries
            the ids of nodes that were collapsed into it.
        aliased_ids (list[str]): ids of ancestors / siblings
            collapsed into this region by Pass 4 (same-bbox
            passthrough) or by pattern detection.
        name (str): the chosen layer name. Usually the
            top-of-stack name; patterns may rewrite it (e.g.
            ``"Top Shares stat-list"`` instead of the raw
            ``"Cluster Details"``).
        role (str): one of the enumerated role strings the
            walker emits. Common values: ``"component-instance"``,
            ``"composed-region"``, ``"layout-container"``,
            ``"stat-list"``, ``"table-column"``, ``"tab-strip"``,
            ``"button-group"``, ``"kpi-tile"``, ``"icon"``.
        bbox (tuple[float, float, float, float]): absolute
            ``(x, y, w, h)`` from
            ``node.absoluteBoundingBox``.
        parent_chain (list[str]): ancestor names, root-first.
            Truncated to the most recent few by the caller.
        content_slots (dict[str, str | list[str]]): captured
            text / item lists. Keys we emit today: ``"title"``,
            ``"items"``, ``"header"``, ``"label"``, ``"value"``,
            ``"icon_name_hint"``, ``"cell_count"``,
            ``"header_icon"``, ``"first_cell_sample"``.
        structural_hints (list[str]): heuristic strings like
            ``"320x309 ~square"``, ``"3-row vertical stack"``,
            ``"icon-leading"``. Cheap to compute, high signal
            for shape-based components.
        children_summary (str): one-line description of immediate
            descendant types, e.g. ``"FRAME Header(1 TEXT)"`` or
            ``"3 FRAME Row"``. Feeds the BM25 query.
        hex_colors (list[str]): unique visible fill hexes
            (``"#XXXXXX"``, uppercased), in first-seen order.
        box_style (BoxStyle): CSS-aligned style snapshot of the
            FRAME — background colour, border, corner radius,
            padding (auto-layout or inferred), gap, shadow,
            opacity. Empty when the node has no styling worth
            surfacing. Crucially this is how visual containers
            (``Status/Alert Banner``, cards, panels) carry their
            grey-fill-and-rounded-corner identity through to the
            generator instead of vanishing as bare layout wrappers.
        reference_jsx_slice (str | None): the per-region slice
            of the input ``reference_jsx``, matched by Figma
            node-id comments. ``None`` when the caller did not
            supply ``reference_jsx`` or no slice could be
            extracted.
        mapping (FigmaNodeMapping): the in-process call result
            of :func:`prism_mcp.figma_mapping.map_figma_node`
            on the enriched signals above. Contains
            ``candidates`` (the top-k Prism component picks),
            ``related``, ``a11y_blocks``, ``token_mappings``,
            ``examples``, and ``candidate_decompositions``.
        absolute_pos (AbsolutePos | None): per-child offset to apply
            when this region's id appears in some parent's
            :attr:`LayoutAnalysis.absolute_children`. The generator
            should set ``position: absolute`` on the rendered element
            and use ``top`` / ``left`` from this field. ``None`` for
            regions that flow normally (the common case).
        shape_bucket (str): coarse-grained geometric category for
            this region's bbox (``"tile"`` / ``"card"`` /
            ``"banner"`` / ``"icon"`` / ``"sidebar"`` /
            ``"modal"`` / ``"page"`` / ``"block"``; ``""`` when
            the bbox is empty). Produced by
            :func:`prism_mcp.figma.utils.shape_bucket`. The ranker
            in :mod:`prism_mcp.figma_mapping` uses this
            to apply a tiny shape-aware bonus.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    aliased_ids: list[str] = Field(default_factory=list)
    name: str
    role: str
    bbox: tuple[float, float, float, float]
    parent_chain: list[str] = Field(default_factory=list)
    content_slots: dict[str, str | list[str] | int] = Field(
        default_factory=dict
    )
    structural_hints: list[str] = Field(default_factory=list)
    children_summary: str = ""
    hex_colors: list[str] = Field(default_factory=list)
    box_style: BoxStyle = Field(default_factory=BoxStyle)
    reference_jsx_slice: str | None = None
    mapping: FigmaNodeMapping
    absolute_pos: AbsolutePos | None = None
    shape_bucket: str = ""


_LEAN_PARENT_CHAIN_CAP = 3
"""Lean agenda rows keep only the most recent ancestors.

The full ``parent_chain`` is root-first; the closest few ancestors
carry the strongest context while the deeper ones add tokens for
little signal. Three mirrors the per-region context window the
ranker already favours (see
:func:`prism_mcp.figma_mapping._build_lexical_query`, which appends
only ``parent_chain[-2:]`` to the BM25 query)."""


_LEAN_CANDIDATES_CAP = 3
"""Lean agenda rows surface only the top-3 candidate picks.

Each is reduced to ``{name, score}`` so the LLM can see the chosen
component plus its two nearest alternatives without paying for the
``why_matched`` / ``summary`` / ``source`` fields. The LLM drills
into ``map_figma_node`` when it needs the full candidate rationale
for a specific region."""


class FigmaTreeMapping(BaseModel):
    """The walker's full output.

    Two derived views over the same DFS pass:

    * ``layout_tree`` answers *"how do regions nest?"* — small
      enough to fit into a first-turn prompt.
    * ``agenda`` answers *"for each region, which Prism component?"*
      — the full per-region detail, including the per-region
      :class:`FigmaNodeMapping`.

    Plus three audit / summary fields:

    * ``tokens`` — every hex literal we saw mapped to its closest
      Prism token name. ``"#XXXXXX" → token-name``.
    * ``dropped`` — every node we discarded, with reason.
    * ``summary`` — quick counts to sanity-check the run.
    * ``warnings`` — non-fatal observations (e.g. safety-rail
      trips, suspicious drop distributions).

    This is the *complete* output the walker computes. The MCP
    boundary ships it through :func:`leanify_tree_mapping`, which by
    default trims it to the smaller :meth:`to_lean_response` shape so
    the client's context window is not flooded; callers that need
    everything pass ``response_detail="full"``.

    Args:
        layout_tree (list[LayoutNode]): pruned spatial structure.
        agenda (list[MappedRegion]): ordered Prism decisions.
        tokens (dict[str, str]): hex → token-name.
        dropped (list[DroppedNode]): audit trail.
        summary (dict[str, int]): counters — see design doc §4.2.
        warnings (list[str]): non-fatal observations.
    """

    model_config = ConfigDict(extra="forbid")

    layout_tree: list[LayoutNode] = Field(default_factory=list)
    agenda: list[MappedRegion] = Field(default_factory=list)
    tokens: dict[str, str] = Field(default_factory=dict)
    dropped: list[DroppedNode] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def to_lean_response(self) -> dict[str, Any]:
        """Project this mapping into the trimmed "lean" wire shape.

        ``map_figma_tree`` defaults to this shape so the Cursor LLM
        is not flooded with the heavy per-row *retrieval* payload
        (raw JSX ``examples``, ``a11y_blocks``, the full
        ``candidates`` rows, ``token_mappings``) and the full
        ``dropped`` audit list — which on X-Ray-scale pages reaches
        hundreds-to-thousands of rows and dominates the response. The
        LLM can pull the full detail for any single region on demand
        via ``map_figma_node``; it does NOT need to ship up front.

        This is a *pure transform* over the already-computed
        :class:`FigmaTreeMapping` — the walker and ``map_figma_node``
        are untouched and still compute everything. Only the bytes
        shipped to the client change, so every walker / golden test
        stays valid.

        The lean shape is::

            {
              "layout_tree": [...],            # unchanged
              "agenda": [                      # slimmed rows
                {
                  "id", "name", "role", "bbox",
                  "parent_chain",              # capped to last 3
                  "shape_bucket", "children_summary",
                  "content_slots", "structural_hints",
                  "box_style", "hex_colors", "absolute_pos",
                  "mapping": {                 # slim recommendation
                    "suggested_component_name",
                    "primary_recommendation",
                    "primary_recommendation_confidence",
                    "description",             # top candidate summary
                    "candidates": [            # top-3 {name, score}
                      {"name", "score"}, ...
                    ]
                  }
                }, ...
              ],
              "tokens": {...},                 # unchanged
              "dropped_summary": {reason: count, ...},  # replaces list
              "summary": {...},                # unchanged
              "warnings": [...],               # unchanged
              "reduction": {                   # Prismify-style telemetry
                "input_nodes", "agenda_size", "dropped_count",
                "response_chars_full", "response_chars_lean"
              }
            }

        Compared with the full :meth:`model_dump`, the lean agenda
        row drops ``aliased_ids`` and ``reference_jsx_slice`` and
        replaces the full :class:`FigmaNodeMapping` with the slim
        recommendation object above.

        Returns:
            dict[str, Any]: the lean wire payload. ``model_dump`` is
            NOT round-trippable back into :class:`FigmaTreeMapping`
            (the shape intentionally differs); it is a terminal
            serialisation for the MCP boundary only.
        """
        full = self.model_dump()
        chars_full = len(json.dumps(full, ensure_ascii=False, default=str))

        lean_agenda: list[dict[str, Any]] = []
        for region in full["agenda"]:
            node_mapping = region.get("mapping") or {}
            candidates = node_mapping.get("candidates") or []
            top = candidates[0] if candidates else None
            slim_mapping = {
                "suggested_component_name": node_mapping.get(
                    "suggested_component_name"
                ),
                "primary_recommendation": node_mapping.get(
                    "primary_recommendation"
                ),
                "primary_recommendation_confidence": node_mapping.get(
                    "primary_recommendation_confidence", 0.0
                ),
                "description": top.get("summary", "") if top else "",
                "candidates": [
                    {"name": c.get("name"), "score": c.get("score")}
                    for c in candidates[:_LEAN_CANDIDATES_CAP]
                ],
            }
            lean_agenda.append(
                {
                    "id": region["id"],
                    "name": region["name"],
                    "role": region["role"],
                    "bbox": region["bbox"],
                    "parent_chain": region.get("parent_chain", [])[
                        -_LEAN_PARENT_CHAIN_CAP:
                    ],
                    "shape_bucket": region.get("shape_bucket", ""),
                    "children_summary": region.get("children_summary", ""),
                    "content_slots": region.get("content_slots", {}),
                    "structural_hints": region.get("structural_hints", []),
                    "box_style": region.get("box_style", {}),
                    "hex_colors": region.get("hex_colors", []),
                    "absolute_pos": region.get("absolute_pos"),
                    "mapping": slim_mapping,
                }
            )

        dropped_summary = dict(Counter(d["reason"] for d in full["dropped"]))

        lean: dict[str, Any] = {
            "layout_tree": full["layout_tree"],
            "agenda": lean_agenda,
            "tokens": full["tokens"],
            "dropped_summary": dropped_summary,
            "summary": full["summary"],
            "warnings": full["warnings"],
        }
        # ``response_chars_lean`` is measured on the payload *before*
        # the small ``reduction`` object is attached, so it is an
        # approximation (off by the telemetry block's own size). That
        # is fine — the value exists to show the order-of-magnitude
        # win versus ``response_chars_full``, not to be byte-exact.
        chars_lean = len(json.dumps(lean, ensure_ascii=False, default=str))
        lean["reduction"] = {
            "input_nodes": full["summary"].get("input_nodes", 0),
            "agenda_size": len(lean_agenda),
            "dropped_count": len(full["dropped"]),
            "response_chars_full": chars_full,
            "response_chars_lean": chars_lean,
        }
        return lean


def leanify_tree_mapping(
    mapping: FigmaTreeMapping,
    detail: Literal["lean", "full"],
) -> dict[str, Any]:
    """Serialise ``mapping`` for the MCP boundary honouring ``detail``.

    The single choke point both the live-walker path and the curated
    mock path in :mod:`prism_mcp.server` route through, so the
    ``response_detail`` contract is enforced in exactly one place.

    Args:
        mapping (FigmaTreeMapping): the walker's (or a mock's) full
            output.
        detail (Literal["lean", "full"]): ``"full"`` returns
            ``mapping.model_dump()`` verbatim — byte-for-byte
            identical to the pre-lean behaviour, for regression
            safety. ``"lean"`` returns
            :meth:`FigmaTreeMapping.to_lean_response`.

    Returns:
        dict[str, Any]: the JSON-serialisable wire payload.
    """
    if detail == "full":
        return mapping.model_dump()
    return mapping.to_lean_response()
