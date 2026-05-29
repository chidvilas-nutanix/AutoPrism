"""Pydantic shapes for the Figma → Prism page walker.

All four boundary shapes (`MapFigmaTreeInput`, `MappedRegion`,
`LayoutNode`, `DroppedNode`, `FigmaTreeMapping`) use
``ConfigDict(extra="forbid")`` to match the style of
:mod:`prism_mcp.workflow.figma_mapping` and to make schema drift loud:
adding an unrecognised field at the MCP boundary produces a clear
``ValidationError`` instead of silently ignored input.

See design doc §4.1 (input), §4.2 (output), and §10.1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from prism_mcp.workflow.figma_mapping import FigmaNodeMapping

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
        max_agenda (int): soft cap on agenda size. Default 50.
            When exceeded the walker emits a warning and groups
            the smallest siblings into a generic "container" row.
        bypass_cache (bool): if True, skip the disk cache for the
            REST fetch (useful when the user has just edited the
            design and wants a fresh pull). Default False.
    """

    model_config = ConfigDict(extra="forbid")

    node_url: str
    reference_jsx: str | None = None
    variable_defs: dict[str, str] | None = None
    figma_token: str | None = None
    max_depth: int = 20
    max_nodes: int = 5000
    max_agenda: int = 50
    bypass_cache: bool = False


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
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    role: str
    bbox: tuple[float, float, float, float]
    children_ids: list[str] = Field(default_factory=list)


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
        reference_jsx_slice (str | None): the per-region slice
            of the input ``reference_jsx``, matched by Figma
            node-id comments. ``None`` when the caller did not
            supply ``reference_jsx`` or no slice could be
            extracted.
        mapping (FigmaNodeMapping): the in-process call result
            of :func:`prism_mcp.workflow.figma_mapping.map_figma_node`
            on the enriched signals above. Contains
            ``candidates`` (the top-k Prism component picks),
            ``related``, ``a11y_blocks``, ``token_mappings``,
            ``examples``, and ``candidate_decompositions``.
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
    reference_jsx_slice: str | None = None
    mapping: FigmaNodeMapping


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
