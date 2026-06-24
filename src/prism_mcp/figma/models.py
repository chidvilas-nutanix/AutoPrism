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

from prism_mcp.figma.catalog import RegionResolution
from prism_mcp.figma.props import ResolvedProp
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
            exact lean shape. Pass ``"codespec"`` for the roadmap-P8
            **render-ready tree** — a single
            :class:`prism_mcp.figma.codespec.PrismCodeSpec` (nested JSX
            nodes with their resolved Prism tag / import / typed props /
            children / tokens / confidence + deduped imports), so the
            skill renders the page verbatim instead of re-deriving each
            component. See ``improvements/08-phase8-codespec.md``.
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
    response_detail: Literal["lean", "full", "codespec"] = "lean"


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


class PrismLayout(BaseModel):
    """A Prism Layout primitive recommendation for one container (P4).

    Attached to :attr:`LayoutNode.prism_layout` by the walker for
    structural containers — the FRAMEs that would otherwise become
    hand-written ``<div style={{display:'flex',…}}>``. The generator
    renders ``<{component} {props}>`` verbatim around the node's child
    regions, so the output uses the design system's layout components
    instead of bare divs + inline CSS (roadmap P4, the "no divs" layer).

    Built by :func:`prism_mcp.figma.layout.resolve_prism_layout` from the
    CSS-aligned :class:`LayoutAnalysis`; the model lives here so it can sit
    on :class:`LayoutNode` without a circular import.

    Args:
        component (str): the Prism primitive — ``"FlexLayout"`` (the
            general flex container), ``"StackingLayout"`` (a plain
            vertical stack), or ``"ContainerLayout"`` (a styled box —
            ``backgroundColor`` / ``border`` / ``padding`` — wrapping a
            non-flow region; roadmap P4 follow-up #3).
        props (dict[str, str]): JSX-ready prop -> value, all string-valued
            (``{"flexDirection": "column", "itemGap": "M",
            "justifyContent": "space-between"}``). Default-valued props are
            intentionally omitted so the spec matches the library's own
            examples (e.g. ``flexDirection`` is dropped for a row,
            ``alignItems`` for ``stretch``). For ``ContainerLayout`` the
            keys are ``backgroundColor`` (``dark`` / ``transparent`` /
            ``white``), ``border`` (``"true"`` when a stroke is present),
            and ``padding``.
        source (str): ``"figma_auto_layout"`` when the decision came
            verbatim from Figma's own auto-layout fields (confidence 1.0),
            or ``"geometry"`` when inferred from child bounding boxes.
        confidence (float): 0-1, passed through from the underlying
            :class:`LayoutAnalysis`.
        fill_child_ids (list[str]): ids of this container's flow children
            that fill the main axis (Figma ``layoutGrow == 1`` or
            ``layoutSizing{Horizontal,Vertical} == "FILL"``). The generator
            wraps each in ``<FlexItem flexGrow="1">`` — the canonical
            "filling child" (a ``Table`` between a left menu and right
            filters; roadmap P4 follow-up #2). Empty for the common case
            where every child is hug/fixed-sized.
        notes (list[str]): short flags for non-obvious mappings
            (``"figma GRID -> FlexLayout+flexWrap (no Prism grid
            primitive)"``, ``"non-token padding (5,10,5,30) dropped"``).
    """

    model_config = ConfigDict(extra="forbid")

    component: Literal["FlexLayout", "StackingLayout", "ContainerLayout"]
    props: dict[str, str] = Field(default_factory=dict)
    source: Literal["figma_auto_layout", "geometry"]
    confidence: float = 0.0
    fill_child_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PrismPageShell(BaseModel):
    """A page-level Prism shell layout for the route-anchoring container (P4).

    Where :class:`PrismLayout` annotates the many mid-tree flow wrappers,
    this annotates the **single** page-scale frame whose top-level children
    are a header / left-nav / body / footer arrangement — so the generator
    renders ``<MainPageLayout header={...} leftPanel={...} body={...}>``
    instead of a hand-rolled flex skeleton (roadmap P4 follow-up #1).

    Attached to the root container's :attr:`LayoutNode.prism_shell` by the
    walker, and only when the geometric evidence is strong (conservative —
    one wrong call per page is worse than a missed shell, which the
    ``FlexLayout`` column fallback still covers).

    Args:
        component (str): the Prism shell — ``"MainPageLayout"`` (header? +
            left panel + body), ``"HeaderFooterLayout"`` (header + body +
            footer?), or ``"LeftNavLayout"`` (left panel + body, no header).
        slots (dict[str, str]): shell prop (node slot) -> child region id.
            Keys are the library's own slot names — ``MainPageLayout``:
            ``header`` / ``leftPanel`` / ``body``; ``HeaderFooterLayout``:
            ``header`` / ``bodyContent`` / ``footer``; ``LeftNavLayout``:
            ``leftPanel`` / ``rightBodyContent``. Each value is a
            :class:`LayoutNode` / :class:`MappedRegion` id the generator
            renders into that slot.
        source (str): always ``"geometry"`` — shells are inferred from
            top-level child bounding boxes, never from a Figma field.
        confidence (float): 0-1 geometric confidence.
        notes (list[str]): short flags (``"header full-width 64px top"``).
    """

    model_config = ConfigDict(extra="forbid")

    component: Literal["MainPageLayout", "HeaderFooterLayout", "LeftNavLayout"]
    slots: dict[str, str] = Field(default_factory=dict)
    source: Literal["geometry"] = "geometry"
    confidence: float = 0.0
    notes: list[str] = Field(default_factory=list)


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
        prism_layout (PrismLayout | None): the Prism Layout primitive
            (``FlexLayout`` / ``StackingLayout``) + token-snapped props
            this container should render as (roadmap P4). Populated by the
            walker only for **structural container** roles
            (``layout-container`` / ``composed-region``) that carry a
            flow direction — never for keyed component leaves (a
            ``Button``'s internal auto-layout is the component's concern,
            not a ``<div>`` to replace). ``None`` for single-child /
            overlap-stack containers and component instances. See
            ``improvements/05-phase4-layout.md``.
        prism_shell (PrismPageShell | None): the page-level shell
            (``MainPageLayout`` / ``HeaderFooterLayout`` / ``LeftNavLayout``)
            this container should render as, with its child regions assigned
            to header / leftPanel / body / footer slots. Populated by the
            walker only for the **route-anchoring page-scale container** when
            the top-level geometry clearly matches a shell; ``None`` for
            every other node (roadmap P4 follow-up #1). See
            ``improvements/05-phase4-layout.md`` §8.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    role: str
    bbox: tuple[float, float, float, float]
    children_ids: list[str] = Field(default_factory=list)
    layout: LayoutAnalysis | None = None
    prism_layout: PrismLayout | None = None
    prism_shell: PrismPageShell | None = None


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
        background_token (str | None): the Prism color **token name**
            the ``background_color`` hex resolves to (roadmap P5) —
            either the designer's own Figma variable name or the
            nearest Prism color token within the ``exact`` / ``near``
            perceptual bucket. ``None`` when no close token exists (the
            raw ``background_color`` hex is then the source of truth).
            Resolved by :func:`prism_mcp.figma.tokens.resolve_color_token`.
        border_token (str | None): the Prism color token name the
            ``border_color`` hex resolves to (same cascade as
            ``background_token``).
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
    background_token: str | None = None
    border_token: str | None = None


class Typography(BaseModel):
    """Resolved Prism typography for a region's representative text (P5).

    The walker picks the most prominent (largest) TEXT descendant of a region
    and maps its Figma ``style`` (font size + weight) onto the Prism type
    ramp so codegen emits a typography **token** (``<Paragraph>`` /
    ``<Title size="h2">`` + ``@title-h2-font-size``) instead of a raw
    ``fontSize: 18px`` literal — the roadmap P5 "typography as tokens" goal.

    Built by :func:`prism_mcp.figma.tokens.resolve_typography` from the
    curated Prism type ramp (``Variables.less``: ``@title-h1…h4``,
    ``@paragraph``, ``@label``, ``@label-small``, ``@link``, ``@tag``).

    Args:
        font_size (float | None): the Figma ``style.fontSize`` in px (echoed
            for traceability / fallback).
        font_weight (int | None): the Figma ``style.fontWeight`` (echoed).
        style_token (str | None): the resolved Prism named text style —
            ``"title-h1"`` … ``"title-h4"`` / ``"paragraph"`` / ``"label"`` /
            ``"label-small"`` / ``"link"`` / ``"tag"``. ``None`` when the
            font size is too far from any ramp entry (the px literal stands).
        size_token (str | None): the matching LESS size variable name without
            the ``@`` (``"title-h2-font-size"``).
        weight_token (str | None): the Prism weight name —
            ``fine`` / ``thin`` / ``regular`` / ``medium`` / ``semi-bold`` /
            ``bold``.
        confidence (float): ``1.0`` for an exact ``(size, weight)`` ramp hit,
            lower for a nearest-size match.
    """

    model_config = ConfigDict(extra="forbid")

    font_size: float | None = None
    font_weight: int | None = None
    style_token: str | None = None
    size_token: str | None = None
    weight_token: str | None = None
    confidence: float = 0.0


class PrismIcon(BaseModel):
    """Resolved Prism icon component for an icon region (roadmap P6).

    The walker collapses an icon glyph (a ``BOOLEAN_OPERATION`` / ``VECTOR``
    subtree, or an ``icon/``-named layer, or an icon-shaped INSTANCE) into a
    single region carrying the Figma icon name. This model maps that name
    onto the **exact Prism icon component** so codegen emits
    ``<ChevronDownIcon />`` instead of an inline ``<svg>`` or a guessed name.

    Built by :func:`prism_mcp.figma.content.resolve_icon` against the Prism
    icon vocabulary (the 213 ``*Icon`` components), via a normalized-name
    match plus a small curated synonym map.

    Args:
        figma_name (str): the source Figma icon name / hint (e.g.
            ``"icon/chevron-down"``, ``"Menu"``).
        prism_component (str): the resolved Prism component name
            (``"ChevronDownIcon"``). The import is ``@nutanix-ui/prism-reactjs``.
        method (str): how it resolved — ``"exact"`` (normalized name equals a
            Prism icon's normalized name), ``"synonym"`` (curated alias), or
            ``"fuzzy"`` (token-subset / contains match).
        confidence (float): ``1.0`` exact, ``0.9`` synonym, ``0.6`` fuzzy.
    """

    model_config = ConfigDict(extra="forbid")

    figma_name: str
    prism_component: str
    method: str
    confidence: float = 0.0


class ContentBinding(BaseModel):
    """Which Prism prop a region's text content binds to (roadmap P6).

    The walker captures a region's text (``content_slots["title"]`` /
    concatenated TEXT). This model records the **target prop** that text
    should be rendered into for the region's resolved component — so codegen
    emits ``<Button>Save</Button>`` (``children``) vs
    ``<Title>Overview</Title>`` vs ``<Input label="Name" />`` deterministically
    rather than guessing.

    Built by :func:`prism_mcp.figma.content.bind_text_content` from the
    component's P3 prop schema (the text-bearing prop, by name priority among
    ``node`` / ``string`` kinds) with a role-based fallback to ``children``.

    Args:
        prop (str): the target prop name (``"children"`` / ``"title"`` /
            ``"label"`` / ``"text"`` / ``"placeholder"`` …).
        value (str): the text content to render into it.
        value_kind (str): ``"children"`` when the text is the element body,
            else ``"string"`` for an attribute prop.
        source (str): provenance — ``"prop-schema"`` (a named text prop in the
            component schema), ``"role-default"`` (role/heuristic), or
            ``"children-default"`` (universal React body fallback).
    """

    model_config = ConfigDict(extra="forbid")

    prop: str
    value: str
    value_kind: str
    source: str


class FigmaComponentIdentity(BaseModel):
    """Exact design-system identity resolved from the Figma maps.

    Populated by the walker for ``INSTANCE`` (and ``COMPONENT``) nodes
    when the fetch threaded the sibling ``components`` / ``componentSets``
    maps into :func:`prism_mcp.figma.walk_tree` (the P1 "fetch fix" — see
    ``improvements/02-phase1-fetch-fix.md``). It is the **deterministic**
    identity signal that the fuzzy BM25/dense ranker cannot provide:

    * ``component_key`` is a stable, global id — every instance of the
      same published library component across every file carries it. It
      is the exact join key into the (forthcoming P2) catalog
      ``componentKey -> Prism component``.
    * ``component_name`` is the logical/variant-family name (taken from
      the component-set when the instance belongs to one, else the
      component name) — e.g. ``"Action/ ✅ Button"``.
    * ``description`` carries the canonical ``prism-styleguide`` /
      ``ds.nutanix.design`` URL on the ~41% of library nodes that have
      one; ``doc_url`` is the first such URL extracted for convenience.
      Both are consumed at catalog-build time, not page-mapping time.

    ``None`` on a :class:`MappedRegion` means the node was not an
    instance/component, or the maps were unavailable (e.g. the legacy
    document-only fetch path, or a curated mock).

    Args:
        component_id (str): the instance's node-local ``componentId``
            (for a ``COMPONENT`` node, its own ``id``). The key into the
            response's ``components`` map.
        component_key (str): the stable global ``componentKey`` from the
            ``components`` map. Empty string only if the entry lacked a
            key (should not happen for published components).
        component_name (str): logical name — the ``componentSets`` entry
            name when present, else the ``components`` entry name.
        component_set_id (str | None): the ``componentSetId`` when the
            instance belongs to a variant family, else ``None``.
        component_set_key (str | None): the global ``componentKey`` of
            the owning component-set, when the instance belongs to one.
            Variant families are frequently catalogued at the *set*
            level, so the P2 catalog can join on either this or
            ``component_key``. ``None`` for standalone components.
        remote (bool): ``True`` when the component is published by a
            *remote* library (i.e. defined in another file). On real
            product pages the design-system instances are ~all remote.
        description (str): the raw component / component-set description.
            Often contains the canonical Prism styleguide URL.
        doc_url (str | None): the first ``http(s)`` URL found in
            ``description`` (the styleguide / ds.nutanix.design link),
            or ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    component_id: str
    component_key: str = ""
    component_name: str = ""
    component_set_id: str | None = None
    component_set_key: str | None = None
    remote: bool = False
    description: str = ""
    doc_url: str | None = None


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
        figma_component (FigmaComponentIdentity | None): the exact
            design-system identity (global ``component_key`` + logical
            name + remote flag + styleguide ``doc_url``) resolved from
            the fetched ``components`` / ``componentSets`` maps for
            ``INSTANCE`` / ``COMPONENT`` regions. ``None`` for layout
            FRAMEs, text, patterns, or when the maps were unavailable
            (legacy document-only fetch / mocks). This is the
            deterministic join key the P2 catalog will resolve to a
            Prism component; see ``improvements/02-phase1-fetch-fix.md``.
        prism_resolution (RegionResolution | None): the authoritative
            Tier-1 routing outcome — the Prism component family the P2
            catalog resolved this region's :attr:`figma_component`
            identity to, with the cascade ``method``, ``confidence``,
            and ``source`` (``"catalog"`` for a precomputed
            ``componentKey`` hit, ``"page-fallback"`` for a cascade on
            the page-provided name/description). Populated by the
            walker's post-DFS routing pass only when the identity
            resolves to a real component; ``None`` for layout FRAMEs,
            patterns, unresolved keys, or when no ``components`` map was
            supplied. When set, the walker has already promoted this
            family into :attr:`mapping` (``primary_recommendation`` /
            ``suggested_component_name``) unless an audited pattern role
            already claimed a finer sub-component. See
            ``improvements/04-phase3-routing-and-props.md``.
        prism_props (list[ResolvedProp]): the typed Prism props derived
            from this instance's Figma ``componentProperties`` (P3 Part
            B). Each carries the prop name, a JSX-ready ``value`` +
            ``value_kind`` (expression / string / bool), and provenance
            (``method`` + ``confidence``). Populated by the walker's
            prop-resolution pass only for regions that routed to a Prism
            family *and* whose Figma node exposed ``componentProperties``;
            empty otherwise. The deterministic bridge is value-driven
            (a Figma value ``Primary`` -> ``ButtonTypes.PRIMARY``), with
            name- and curated-fallbacks. See
            ``improvements/04-phase3-routing-and-props.md`` Part B.
        typography (Typography | None): the resolved Prism typography token
            (size / weight) for this region's most prominent text, mapped
            from the dominant TEXT descendant's Figma ``style`` (roadmap P5).
            ``None`` for regions without text or whose font size is too far
            from the Prism type ramp. See
            ``improvements/06-phase5-tokens.md``.
        prism_icon (PrismIcon | None): the resolved Prism icon component for
            an icon region, mapped from the Figma icon name to one of the 213
            ``*Icon`` components (roadmap P6). ``None`` for non-icon regions or
            an unresolvable glyph. See ``improvements/07-phase6-content.md``.
        content_binding (ContentBinding | None): which Prism prop this
            region's text content binds to — ``children`` / ``title`` /
            ``label`` / … — chosen from the resolved component's prop schema
            (roadmap P6). ``None`` when the region carries no text or routed to
            no component. See ``improvements/07-phase6-content.md``.
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
    figma_component: FigmaComponentIdentity | None = None
    prism_resolution: RegionResolution | None = None
    prism_props: list[ResolvedProp] = Field(default_factory=list)
    typography: Typography | None = None
    prism_icon: PrismIcon | None = None
    content_binding: ContentBinding | None = None


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
                  "figma_component",           # exact DS identity (P1)
                  "prism_resolution"?,         # Tier-1 routing (P3);
                                               # present only when the
                                               # identity resolved:
                                               # {prism_component, source,
                                               #  method, confidence}
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
            lean_row: dict[str, Any] = {
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
                "figma_component": region.get("figma_component"),
                "mapping": slim_mapping,
            }
            # Tier-1 routing outcome (P3). Surfaced only when the
            # identity actually resolved to a Prism family so the
            # common no-identity row (every existing fixture / mock)
            # is byte-for-byte unchanged.
            resolution = region.get("prism_resolution")
            if resolution and resolution.get("prism_component"):
                lean_row["prism_resolution"] = {
                    "prism_component": resolution["prism_component"],
                    "source": resolution["source"],
                    "method": resolution["method"],
                    "confidence": resolution["confidence"],
                }
            # Typed props (P3 Part B). Surfaced only when at least one
            # prop resolved, reduced to the JSX-ready triple the
            # generator needs; full provenance stays in the heavy shape.
            prism_props = region.get("prism_props") or []
            if prism_props:
                lean_row["prism_props"] = [
                    {
                        "prop": p["prop"],
                        "value": p["value"],
                        "value_kind": p["value_kind"],
                    }
                    for p in prism_props
                ]
            # Typography token (P5). Surfaced only when a region's text
            # resolved to the Prism type ramp, reduced to the codegen-ready
            # triple (style / size / weight tokens); full detail in the
            # heavy shape.
            typography = region.get("typography")
            if typography and typography.get("style_token"):
                lean_row["typography"] = {
                    "style_token": typography["style_token"],
                    "size_token": typography["size_token"],
                    "weight_token": typography["weight_token"],
                }
            # Icon (P6). Surfaced as the bare codegen-ready component name.
            prism_icon = region.get("prism_icon")
            if prism_icon and prism_icon.get("prism_component"):
                lean_row["prism_icon"] = prism_icon["prism_component"]
            # Text→prop binding (P6). The prop + value the text renders into.
            content_binding = region.get("content_binding")
            if content_binding and content_binding.get("prop"):
                lean_row["content_binding"] = {
                    "prop": content_binding["prop"],
                    "value": content_binding["value"],
                    "value_kind": content_binding["value_kind"],
                }
            lean_agenda.append(lean_row)

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
    detail: Literal["lean", "full", "codespec"],
) -> dict[str, Any]:
    """Serialise ``mapping`` for the MCP boundary honouring ``detail``.

    The single choke point both the live-walker path and the curated
    mock path in :mod:`prism_mcp.server` route through, so the
    ``response_detail`` contract is enforced in exactly one place.

    Args:
        mapping (FigmaTreeMapping): the walker's (or a mock's) full
            output.
        detail (Literal["lean", "full", "codespec"]): ``"full"`` returns
            ``mapping.model_dump()`` verbatim — byte-for-byte
            identical to the pre-lean behaviour, for regression
            safety. ``"lean"`` returns
            :meth:`FigmaTreeMapping.to_lean_response`. ``"codespec"``
            returns the roadmap-P8 render-ready
            :class:`prism_mcp.figma.codespec.PrismCodeSpec` as a dict.

    Returns:
        dict[str, Any]: the JSON-serialisable wire payload.
    """
    if detail == "full":
        return mapping.model_dump()
    if detail == "codespec":
        # Lazy import: ``codespec`` imports this module's models, so a
        # top-level import here would be circular.
        from prism_mcp.figma.codespec import build_code_spec

        return build_code_spec(mapping).model_dump()
    return mapping.to_lean_response()
