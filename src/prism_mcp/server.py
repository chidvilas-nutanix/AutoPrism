"""Entrypoint for the Prism ReactJS MCP server.

This module wires up the official ``mcp`` Python SDK's ``FastMCP`` over
the stdio transport and registers the v1 tool surface.

The v1 tool surface is seven tools: ``echo``, ``get_library_meta``,
``search_entities``, ``search_examples``, ``get_entity``,
``map_figma_node``, and ``map_figma_tree`` — covering Artifactory
library acquisition, indexing/search across components, hooks,
managers, utils, and tokens, and Figma->Prism mapping. A background
refresh task is wired into the FastMCP lifespan; cold-start with no
cache surfaces a clear "VPN required" error. Validating generated
code is the client's (Cursor's) responsibility, not this server's.

Stdout is reserved for the MCP JSON-RPC framing; all logging is routed
to stderr per the project's non-functional requirements.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from prism_mcp.cache import Cache
from prism_mcp.config import ConfigError, ServerConfig
from prism_mcp.entities import EntityType
from prism_mcp.figma import (
    FigmaTreeMapping,
    MapFigmaTreeInput,
    build_icon_index,
    leanify_tree_mapping,
    walk_tree,
)
from prism_mcp.figma.fetch import (
    FetchError,
    FetchErrorCode,
    _fetch_figma_tree_full,
    parse_figma_url,
)
from prism_mcp.figma.mocks import mock_path_for, try_load_mock
from prism_mcp.figma_mapping import (
    map_figma_node as _build_figma_node_mapping,
)
from prism_mcp.library import Library, LibraryError
from prism_mcp.refresh import RefreshLoop, RefreshLoopConfig
from prism_mcp.registry import RegistryClient

logger = logging.getLogger(__name__)

SERVER_NAME = "prism-mcp"
ECHO_REPLY = "prism-mcp: alive"


def _fetch_error_to_mcp(exc: FetchError) -> str:
    """Render a :class:`FetchError` as a Cursor-facing message.

    Format per design doc §7.4:

        ``[<code>] <message>  Hint: <hint>``

    The ``[<code>]`` prefix is machine-readable so the Cursor
    skill can route per-code; the hint nudges the user toward the
    fix. We deliberately raise as ``ValueError`` from the tool —
    FastMCP wraps it into a tool-result error block.
    """
    parts = [f"[{exc.code}] {exc.message}"]
    if exc.hint:
        parts.append(f"Hint: {exc.hint}")
    return "  ".join(parts)


# Re-export FetchErrorCode at module scope so tests can reference
# the canonical taxonomy without reaching into the private fetch
# module. The walker's MCP wrapper is the only legitimate user of
# the underlying FetchError class.
__all__ = ["SERVER_INSTRUCTIONS", "FetchErrorCode", "build_server"]


SERVER_INSTRUCTIONS = """\
prism-mcp exposes the @nutanix-ui/prism-reactjs component library to the
agent: it indexes every component, hook, manager, util, and design token
from the published package and maps Figma designs onto real Prism
components. Use it so you generate correct, non-deprecated, type-safe
Prism JSX instead of inventing component names or props. This server
does NOT build or validate code -- you (Cursor) run tsc / eslint / tests
in your own loop.

CANONICAL FIGMA -> PRISM FLOW

1. For each non-trivial frame/instance from Figma MCP:
   call `map_figma_node(node_name, node_type?, reference_code?, hex_colors?)`
   to get a ranked list of Prism components, related/co-imported components,
   matching design tokens, and imitation JSX examples in one round-trip.
   Always pick from the returned `candidates` list -- never invent a
   component name.

2. Compose the JSX from the returned candidates, examples, related
   components, a11y blocks, and token mappings. When a candidate is thin
   or ambiguous, drill down with `search_examples` (semantic JSX
   retrieval), `search_entities` (BM25 lexical), and `get_entity` (full
   signature + examples) before writing code.

3. Validate in your own loop. After writing the JSX, typecheck / lint /
   test it with your normal tools and iterate. The MCP's job is to hand
   you correct Prism building blocks; it does not run a build for you.

IMPORT STYLE -- USE THE PACKAGE NAME, NOT RELATIVE PATHS

Generated JSX must use consumer-style imports:

  import { Button, Modal, FlexLayout } from '@nutanix-ui/prism-reactjs';

Every entity's `import_path` is the ready-to-paste import statement.
Do NOT switch to relative imports like `../../components/v2/Button/Button`
-- that breaks the component for the consuming app.

ATOMIC TOOLS FOR DRILL-DOWN

`search_entities` (BM25 lexical), `search_examples` (semantic JSX
retrieval), `get_entity` (full record with signature + examples), and
`get_library_meta` (version + index status) cover the cases where
`map_figma_node` returned thin results.

PAGE-LEVEL FIGMA -> PRISM FLOW

When the user pastes a whole-page Figma URL (e.g. a screen, dashboard,
or modal that obviously contains many components), prefer the
page-level flow over manually iterating `map_figma_node` per child.

A. Trigger detection.
   The Cursor `figma-page-to-prism` skill activates when the user
   pastes a `figma.com/design/.../?node-id=...` URL and says any of
   "build this", "implement this page", "convert to Prism", or when
   the referenced node is a whole frame (not a single instance).

B. Read the URL.
   The skill extracts `node_url` verbatim. URL form uses `-` between
   id parts (e.g. `node-id=624-6826`); the tool normalises to `:`.

C. Capture optional plugin signals.
   If the user has the Figma plugin connected, call:
   - `get_design_context(nodeId)` once and pass the body as
     `reference_jsx`.
   - `get_variable_defs(nodeId)` once and pass the dict as
     `variable_defs`.
   Both are optional; the walker degrades gracefully.

D. Call `map_figma_tree(input=MapFigmaTreeInput(...))`.
   By default this returns a LEAN agenda so it does not flood your
   context: each `agenda` row carries the descriptive fields plus a
   chosen component, a one-line `description`, and the top-3
   candidates as `{name, score}` only. Read the response:
   `summary.input_nodes` is your sanity check; if it is 0, the URL
   or token was wrong. `agenda` is the ordered list of region
   decisions; `layout_tree` is the nested shape; `tokens` maps every
   visible hex to its closest Prism token; `dropped_summary` is the
   per-reason audit count map (look for `unknown_type_fallback` or
   extreme `tiny_decorative` counts as a signal that the walker
   missed something); `reduction` reports how much was trimmed.
   map_figma_tree returns a lean agenda by default; call
   `map_figma_node(node_name, role, hex_colors, reference_code?)`
   for full candidates / examples / a11y on a specific region, or
   pass `response_detail="full"` to get everything (including the
   full `dropped` list and per-row `mapping`) in one shot. For
   deterministic generation prefer `response_detail="codespec"`:
   it returns a single render-ready `PrismCodeSpec` tree (each node
   already carries its resolved Prism `tag` / `import_from` / typed
   `props` / `children`-or-`text` / `tokens`), plus deduped
   `imports`. Render it VERBATIM — do not re-pick components. Nodes
   that could not resolve appear as `<div>` with a `notes` reason;
   only those need a `map_figma_node` drill-down.

E. Pre-render walkthrough.
   Echo `summary` to the user (especially `agenda_size`, the top
   `dropped_summary` buckets, and any `warnings`) before generating
   JSX. This is the user's chance to abort if the walker absorbed
   too much.

F. Compose top-down.
   For each `MappedRegion` in `agenda`, `mapping.suggested_component_name`
   (or `mapping.candidates[0].name`) is the suggested Prism component.
   Pick from `candidates` (never invent), import via the canonical
   `@nutanix-ui/prism-reactjs` path, and respect each region's
   `content_slots` (title / items / header / value / label). In the
   default lean response the candidates carry only `{name, score}`
   and the heavy per-row detail (full candidates, imitation JSX
   `examples`, `a11y_blocks`, `token_mappings`, `reference_jsx_slice`)
   is omitted; when a region is ambiguous or you need an imitation
   snippet, call `map_figma_node(node_name, role, hex_colors,
   reference_code?)` for just that region (or re-run map_figma_tree
   with `response_detail="full"`). Then typecheck / lint / test the
   composed page in your own loop.

G. Error handling.
   `map_figma_tree` surfaces a structured `FetchError` for every
   recoverable failure: `missing_token` (no FIGMA_TOKEN env),
   `invalid_token` (rejected by Figma), `file_not_found`,
   `node_not_found`, `rate_limited` (after 3 retries),
   `network_timeout`, `tree_too_large` (over 10MB cap),
   `transport_error`, `invalid_url`. The skill recovers per-code per
   §7.4 of the design doc; never silently retry on
   `missing_token` / `invalid_token` — surface the fix hint to the
   user.
"""


def build_server(
    config: ServerConfig | None = None,
    library_factory: object | None = None,
    refresh_config: RefreshLoopConfig | None = None,
    enable_refresh_loop: bool = True,
) -> FastMCP:
    """Construct the MCP server with the v1 tool surface registered.

    Args:
        config (ServerConfig | None): pre-resolved config, or ``None``
            to defer resolution until ``get_library_meta`` is first
            called. Tests pass a fixed config in.
        library_factory (Callable[[], Library] | None): override the
            :class:`Library` constructor. Used by tests to inject a
            fake. If ``None``, a real :class:`Library` is built from
            ``config`` on first use.
        refresh_config (RefreshLoopConfig | None): tunables for the
            background refresh driver. Defaults to the PRD's daily
            cadence; tests override to a tight interval. Ignored when
            ``enable_refresh_loop`` is ``False``.
        enable_refresh_loop (bool): start the periodic refresh task
            under the FastMCP lifespan. Defaults to ``True`` for
            production; the in-process tool tests set this to
            ``False`` because they exercise ``call_tool`` directly
            without ``server.run()``.

    Returns:
        FastMCP: configured server instance with all tools wired up.
    """
    state = _ServerState(
        config=config,
        library_factory=library_factory,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        """Start/stop the daily refresh task across server lifetime.

        We eagerly construct the library here so the cold-start refresh
        runs once before the first tool call lands, matching the
        Slice 7 demo. On cold-start-no-cache the resulting
        :class:`LibraryError` propagates out and FastMCP shuts the
        server down — Slice 8's "connect to VPN" failure mode.
        """
        loop = state.start_refresh_loop(
            refresh_config=refresh_config,
            enabled=enable_refresh_loop,
        )
        try:
            yield
        finally:
            if loop is not None:
                await loop.stop()

    server = FastMCP(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool()
    def echo() -> str:
        """INTERNAL (operator/health-check only). Return a liveness string.

        Not part of the canonical Figma->Prism flow. Used by
        ``scripts/verify_server.py`` and health checks to prove
        that the stdio transport is alive. The LLM should
        ignore this tool when planning code-gen tasks.

        Returns:
            str: the constant defined by :data:`ECHO_REPLY`.
        """
        logger.info("echo tool invoked")
        return ECHO_REPLY

    @server.tool()
    def get_library_meta() -> dict[str, Any]:
        """Return metadata about the currently indexed Prism library.

        Triggers a registry fetch on cold call; subsequent calls reuse
        the in-process state (and the on-disk cache survives restarts).
        On Artifactory failure with a populated cache, returns the
        cached state with ``from_cache=True``.

        Returns:
            dict: keys ``package_name``, ``version``, ``last_indexed_at``,
            ``source_url``, ``cache_path``, ``from_cache``.
        """
        logger.info("get_library_meta tool invoked")
        library = state.library()
        meta = library.acquire_latest()
        return meta.to_dict()

    @server.tool()
    def search_entities(
        query: str,
        top_k: int = 5,
        type: EntityType | None = None,
    ) -> dict[str, Any]:
        """Rank indexed Prism entities by relevance to ``query``.

        Powered by BM25 over a synthetic doc per entity:
        ``name + type + category + summary + example-titles`` (PRD
        section 5). camelCase identifiers are tokenized into their
        parts so ``useFocusTrap`` matches a query for ``focus trap``.

        Args:
            query (str): free-text prose query from the LLM.
            top_k (int): max rows to return (default 5).
            type (EntityType | None): optional type filter, same
                literals as :meth:`list_entities`.

        Returns:
            dict: ``{"version": ..., "results": [{...}]}`` where each
            row carries ``name``, ``type``, ``score``, ``summary``,
            ``import_path``, and ``why_matched`` (the query tokens
            that hit the entity's doc). Empty ``results`` means no
            entity scored above zero.
        """
        logger.info(
            "search_entities tool invoked query=%r top_k=%d type=%s",
            query,
            top_k,
            type,
        )
        index = state.library().index()
        rows = index.search(query=query, top_k=top_k, type=type)
        return {"version": index.version, "results": rows}

    @server.tool()
    def search_examples(
        query: str,
        top_k: int = 5,
        filter_components: list[str] | None = None,
        reranker: bool = True,
    ) -> dict[str, Any]:
        """Hybrid-rank example code snippets for ``query`` (slice 9 SOTA).

        Slice 9 SOTA: replaces the original pure-dense ranker with a
        three-stage retrieval pipeline that matches early-2026 RAG
        production practice:

        1. **BM25** over each chunk's identifiers, imports, and code
           prefix — catches exact symbol hits like ``useFocusTrap``.
        2. **Dense embeddings** via Jina v2 base-code (768-dim,
           code-specialised) — catches semantic intent like
           "tooltip that survives a layout shift".
        3. **Reciprocal Rank Fusion (k=60)** of the two ranked lists —
           combines them without score normalisation, so the LLM never
           has to call two tools and merge manually.
        4. **Cross-encoder rerank** with ms-marco-MiniLM-L-12-v2 over
           the fused top-50, refining to ``top_k``.

        The full corpus is rebuilt + cached lazily on first call and
        persisted alongside the extracted tarball; subsequent calls
        on the same version skip the embed pass entirely.

        Args:
            query (str): free-text query from the LLM.
            top_k (int): max hits to return (default 5).
            filter_components (list[str] | None): when supplied, only
                return hits whose ``component_name`` is in this set.
                Useful for narrowing to e.g. ``["Modal", "Form"]``.
            reranker (bool): when ``True`` (default), apply the
                cross-encoder rerank stage. Set ``False`` for
                latency-sensitive batch calls — the fused RRF
                ranking is already strong; the reranker buys the
                last ~10 NDCG points at ~80 ms/50-pair extra cost.

        Returns:
            dict: ``{"version": ..., "results": [{...}]}`` where each
            row carries ``component_name``, ``example_id``, ``title``,
            ``code``, ``imports``, and ``score``. The score is the
            *rerank* score when ``reranker=True``, the *RRF fused*
            score otherwise; either way, larger is better.
        """
        logger.info(
            "search_examples tool invoked query=%r top_k=%d filter=%s "
            "reranker=%s",
            query,
            top_k,
            filter_components,
            reranker,
        )
        library = state.library()
        searcher = library.hybrid_searcher()
        hits = searcher.search(
            query=query,
            top_k=top_k,
            filter_components=filter_components,
            use_reranker=reranker,
        )
        version = library.examples_index().version
        return {
            "version": version,
            "results": [hit.model_dump() for hit in hits],
        }

    @server.tool()
    def get_entity(name: str, type: EntityType) -> dict[str, Any]:
        """Return the full record for one entity.

        Args:
            name (str): exact case-sensitive identifier
                (e.g. ``"Button"``).
            type (EntityType): one of the five entity-type literals.

        Returns:
            dict: the full :class:`Entity` projection including
            ``signature`` (props) and ``examples``.

        Raises:
            LibraryError: if no entity matches; surfaced to the client
            as an MCP tool error.
        """
        logger.info("get_entity tool invoked name=%s type=%s", name, type)
        index = state.library().index()
        entity = index.get(name=name, type=type)
        if entity is None:
            raise LibraryError(
                f"no entity found for type={type!r} name={name!r}"
            )
        return entity.model_dump()

    # ------------------------------------------------------------------
    # Figma -> Prism mapping tools (per-node + page-level).
    # ------------------------------------------------------------------

    @server.tool()
    def map_figma_node(
        node_name: str,
        node_type: str | None = None,
        reference_code: str | None = None,
        hex_colors: list[str] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Map a Figma node to candidate Prism components in one call.

        Composite tool. The Figma-MCP gives Cursor a
        node's *name*, *type*, *reference React+Tailwind code*,
        and *variable hex literals*; this tool fans those out
        across the slice-4 BM25 entity index, the slice-9 hybrid
        example index, the slice-10 composition graph, the
        slice-11 color tokens, and the slice-11 a11y rules — and
        returns a single ranked bundle.

        Why a composite tool
        --------------------

        The LLM *could* call ``search_entities`` +
        ``search_examples`` + ``map_token`` + ``related_components``
        + ``get_a11y_rules`` separately to reach the same answer.
        That's 5+ tool calls at 200-500ms each + 5 context-token
        budgets. This tool collapses that to one round-trip
        with a single opinionated fusion:

        * **Reciprocal Rank Fusion** of the BM25 entity ranks
          (lexical signal: layer name + JSX tags from reference
          code) and the hybrid example ranks (semantic signal:
          intent + code body). RRF without score normalisation
          per the same ``k=60`` constant the slice-9 hybrid
          searcher uses internally.
        * **Per-candidate ``source`` label** so the LLM can
          weight ``"both"`` rows (BM25 *and* dense agreed)
          higher than single-source rows.
        * **Top-candidate anchored** related / a11y /
          decompositions so the LLM doesn't have to re-call
          three more tools to flesh out the top match.

        When to use which input granularity
        ------------------------------------

        Call at **FRAME** or **INSTANCE** granularity, where the
        node represents one logical UI element. Page-level
        inputs dilute the signal (a whole screen ≠ one
        component); leaf-level inputs (rectangles, vectors)
        rarely have semantic names. The Cursor agent loop
        should traverse the Figma tree itself and call this
        mapper on each frame/instance encountered.

        Args:
            node_name (str): the Figma layer / frame name. The
                strongest lexical signal — e.g.
                ``"Confirm Delete Modal"`` produces strong
                hits on ``Modal`` + ``ConfirmModal`` +
                ``Button``.
            node_type (str | None): Figma type (``FRAME``,
                ``INSTANCE``, ``GROUP``, ``TEXT``, ...).
                Optional; helps when the layer name is generic.
            reference_code (str | None): the React+Tailwind
                snippet from Figma MCP's ``get_design_context``.
                The semantic ranker keys on it heavily —
                identifiers like ``<Button variant="primary">``
                in the code are strong hits.
            hex_colors (list[str] | None): explicit hex
                literals. When omitted, this tool extracts hex
                values from ``reference_code`` (e.g.
                ``bg-[#1B6BCC]`` arbitrary-value classes).
            top_k (int): cap on candidate matches returned
                (default 5).

        Returns:
            dict: ``FigmaNodeMapping`` shape with fields
            ``node_name``, ``suggested_component_name``,
            ``candidates`` (top-``top_k`` Prism components with
            score + why_matched + source), ``related`` (graph
            neighbours of the top candidate), ``a11y_blocks``,
            ``token_mappings`` (one per input hex), ``examples``
            (top-3 imitation JSX snippets), and
            ``candidate_decompositions``.
        """
        logger.info(
            "map_figma_node tool invoked node=%s type=%s code_len=%s "
            "hex_count=%s top_k=%d",
            node_name,
            node_type,
            len(reference_code) if reference_code else 0,
            len(hex_colors) if hex_colors else 0,
            top_k,
        )
        library = state.library()
        mapping = _build_figma_node_mapping(
            node_name=node_name,
            node_type=node_type,
            reference_code=reference_code,
            hex_colors=hex_colors,
            index=library.index(),
            hybrid_searcher=library.hybrid_searcher(),
            composition_graph=library.composition_graph(),
            color_token_index=library.color_token_index(),
            a11y_rules=library.a11y_rules(),
            top_k=top_k,
        )
        return mapping.model_dump()

    @server.tool()
    async def map_figma_tree(
        input: MapFigmaTreeInput,
    ) -> dict[str, Any]:
        """Walk a whole Figma page into a structured Prism agenda.

        The page-level companion to ``map_figma_node``. Given a
        Figma node URL, this tool:

        1. Parses the URL into ``(file_key, node_id)``.
        2. Fetches the raw Figma SceneNode tree via the REST API
           (with retries + a 1-hour disk cache).
        3. Walks the tree through a 7-pass noise filter, role
           classifier, and 6 pattern detectors — collapsing
           the typical 200-400-node page payload into a 5-40
           row "agenda" of Prism component decisions.
        4. Calls ``map_figma_node`` per agenda row with the
           enriched signals (text_content, children_summary,
           structural_hints, parent_chain) so the BM25 + dense
           rankers see strictly more signal than per-node
           callers can supply on their own.

        Read the canonical PAGE-LEVEL FIGMA -> PRISM FLOW section
        of the server instructions before invoking. The Cursor
        `figma-page-to-prism` skill is the recommended driver.

        Args:
            input (MapFigmaTreeInput): structured input with
                ``node_url`` (required), ``reference_jsx``,
                ``variable_defs``, ``figma_token``, ``max_depth``,
                ``max_nodes``, ``max_agenda``, ``bypass_cache``,
                ``figma_depth`` (per-call override for the REST
                ``depth`` query parameter — defaults to the
                fetcher's tuned value of 12, enough for real
                Nutanix designs), and ``response_detail``
                (``"lean"`` by default, ``"full"`` for the complete
                payload, ``"codespec"`` for the P8 render-ready tree
                — see Returns).

        Returns:
            dict: by default (``response_detail="lean"``) a trimmed
            payload so the agent's context window is not flooded:
            ``layout_tree``, ``tokens``, ``summary``, ``warnings``,
            a ``dropped_summary`` per-reason count map (replacing the
            potentially-thousands-of-rows ``dropped`` list), a
            ``reduction`` telemetry block, and an ``agenda`` whose
            rows keep the descriptive fields (id / name / role /
            bbox / box_style / content_slots / structural_hints /
            hex_colors / …) but carry only a slim ``mapping``
            ``{suggested_component_name, primary_recommendation,
            primary_recommendation_confidence, description,
            candidates:[{name, score}]}``. Call ``map_figma_node`` on
            any single region (pass its ``node_name``, ``role`` and
            ``hex_colors``, plus ``reference_code`` if you have it)
            to get that region's full candidates / examples /
            a11y_blocks / token_mappings on demand. Pass
            ``response_detail="full"`` to get the complete
            :class:`FigmaTreeMapping` shape (``layout_tree``,
            ``agenda`` with full per-row ``mapping``, ``tokens``,
            ``dropped``, ``summary``, ``warnings``) in one shot. Pass
            ``response_detail="codespec"`` for the roadmap-P8
            render-ready :class:`PrismCodeSpec` — a single nested tree
            of JSX nodes (each with its resolved Prism ``tag`` /
            ``import_from`` / typed ``props`` / ``children`` or
            ``text`` / ``tokens`` / ``confidence``), plus deduped
            ``imports``, the ``tokens`` map, ``stats``, and
            ``warnings`` — so the skill renders the page *verbatim*
            instead of re-deriving each component. The fuzzy mapper
            still feeds it, so unresolved regions surface as ``<div>``
            nodes flagged in ``notes`` (drill in with
            ``map_figma_node``).

        Raises:
            ValueError: with a structured ``[<code>] <message>``
                prefix when the underlying fetcher hits one of
                ``missing_token`` / ``invalid_token`` /
                ``file_not_found`` / ``node_not_found`` /
                ``rate_limited`` / ``network_timeout`` /
                ``tree_too_large`` / ``transport_error`` /
                ``invalid_url``. The skill maps each code to a
                user-facing hint per design doc §7.4.

        Curated mocks (offline / instant-response mode):
            Before hitting Figma the tool looks for a hand-curated
            ``FigmaTreeMapping`` JSON file at
            ``mocks/figma_tree/<file_key>__<node_id_with_underscore>.json``
            (or, when set, at ``$PRISM_MCP_FIGMA_TREE_MOCKS_DIR``).
            If present the mock is returned verbatim and the REST
            fetch + walker pipeline is skipped — perfect for demos
            and for hermetic CI runs. Pass ``bypass_cache=True`` to
            ignore the mock and force a live walk. See
            :mod:`prism_mcp.figma.mocks` for the filename convention.
        """
        logger.info(
            "map_figma_tree tool invoked url=%s reference_jsx=%s "
            "variable_defs=%d max_depth=%d max_nodes=%d max_agenda=%d "
            "bypass_cache=%s figma_depth=%s",
            input.node_url,
            "present" if input.reference_jsx else "absent",
            len(input.variable_defs or {}),
            input.max_depth,
            input.max_nodes,
            input.max_agenda,
            input.bypass_cache,
            input.figma_depth if input.figma_depth is not None else "default",
        )

        try:
            parsed = parse_figma_url(input.node_url)
        except FetchError as exc:
            raise ValueError(_fetch_error_to_mcp(exc)) from exc

        # Curated-mock short-circuit. When a hand-authored mapping for
        # this exact (file_key, node_id) lives under
        # ``mocks/figma_tree/<file_key>__<node_id>.json`` (or the
        # ``PRISM_MCP_FIGMA_TREE_MOCKS_DIR`` env override), return it
        # verbatim and skip the live REST fetch + walker entirely.
        # ``bypass_cache=True`` is the documented escape hatch for
        # callers that want to force a fresh REST + walker run even
        # when a mock exists. See ``prism_mcp.figma.mocks`` for the
        # filename convention and resolution rules.
        if not input.bypass_cache:
            mocked = try_load_mock(parsed)
            if mocked is not None:
                logger.info(
                    "map_figma_tree mock short-circuit file_key=%s "
                    "node_id=%s path=%s response_detail=%s",
                    parsed.file_key,
                    parsed.node_id,
                    mock_path_for(parsed),
                    input.response_detail,
                )
                # Curated mocks respect ``response_detail`` too — a
                # mock returned to the LLM should be trimmed on the
                # way out exactly like a live walk, so demos see the
                # same lean payload production does.
                return leanify_tree_mapping(mocked, input.response_detail)

        try:
            fetch_kwargs: dict[str, Any] = {
                "parsed": parsed,
                "figma_token": input.figma_token,
                "bypass_cache": input.bypass_cache,
            }
            if input.figma_depth is not None:
                fetch_kwargs["depth"] = max(1, int(input.figma_depth))
            # P1 fetch fix: pull the document AND the sibling
            # components / componentSets / styles maps so the walker can
            # resolve each instance's exact componentId -> componentKey.
            fetched = await _fetch_figma_tree_full(**fetch_kwargs)
        except FetchError as exc:
            raise ValueError(_fetch_error_to_mcp(exc)) from exc

        logger.info(
            "map_figma_tree fetched maps components=%d component_sets=%d "
            "styles=%d",
            len(fetched.components),
            len(fetched.component_sets),
            len(fetched.styles),
        )

        library = state.library()

        def _bound_map_figma_node(**kwargs: Any) -> Any:
            return _build_figma_node_mapping(
                index=library.index(),
                hybrid_searcher=library.hybrid_searcher(),
                composition_graph=library.composition_graph(),
                color_token_index=library.color_token_index(),
                a11y_rules=library.a11y_rules(),
                **kwargs,
            )

        # Build the P6 icon vocabulary from the library's ``*Icon`` exports
        # (the ~206 Prism icon components). ``index()`` is cached, so this is
        # an O(components) dict build per call, not a re-acquisition.
        entity_index = library.index()
        icon_index = build_icon_index(
            [
                e.name
                for e in entity_index.all()
                if e.type == "component" and e.name.endswith("Icon")
            ],
            version=entity_index.version,
        )

        result: FigmaTreeMapping = walk_tree(
            tree_json=fetched.document,
            reference_jsx=input.reference_jsx,
            variable_defs=input.variable_defs,
            components=fetched.components,
            component_sets=fetched.component_sets,
            styles=fetched.styles,
            max_depth=input.max_depth,
            max_nodes=input.max_nodes,
            max_agenda=input.max_agenda,
            map_figma_node_fn=_bound_map_figma_node,
            color_token_index=library.color_token_index(),
            icon_index=icon_index,
        )
        # Output-shaping only: the walker computed everything above;
        # ``leanify_tree_mapping`` decides how much of it ships to the
        # client based on ``response_detail`` (lean by default).
        return leanify_tree_mapping(result, input.response_detail)

    return server


class _ServerState:
    """Lazy holder for runtime objects shared across tool invocations.

    Keeps tool decorators free of import-time side effects so that
    ``import prism_mcp.server`` never touches the network or
    filesystem. Construction errors surface on first tool call instead
    of preventing the server from booting at all — important because
    Cursor spawns the process before the user types anything.

    Args:
        config (ServerConfig | None): caller-provided config, or
            ``None`` to resolve from env on first use.
        library_factory (Callable[[], Library] | None): override the
            real :class:`Library` constructor for tests.
    """

    def __init__(
        self,
        config: ServerConfig | None,
        library_factory: object | None,
    ) -> None:
        self._config = config
        self._library_factory = library_factory
        self._library: Library | None = None
        self._refresh_loop: RefreshLoop | None = None

    @property
    def refresh_loop(self) -> RefreshLoop | None:
        """Return the running refresh loop, if any."""
        return self._refresh_loop

    def start_refresh_loop(
        self,
        refresh_config: RefreshLoopConfig | None,
        enabled: bool,
    ) -> RefreshLoop | None:
        """Eagerly build the library and start the refresh task.

        Called from the FastMCP lifespan startup. Letting library
        construction happen here (instead of on the first tool call)
        is what gives Slice 8 its clear "cold start with no cache"
        failure mode: the :class:`LibraryError` raised by the cold
        refresh propagates out of the lifespan and FastMCP refuses to
        serve.

        Args:
            refresh_config (RefreshLoopConfig | None): tunables passed
                through to :class:`RefreshLoop`.
            enabled (bool): when ``False``, skip starting the loop and
                return ``None``. The in-process tool tests use this
                because they call ``call_tool`` directly without
                running the server lifecycle.

        Returns:
            RefreshLoop | None: the started loop, or ``None`` when
            disabled.
        """
        if not enabled:
            return None
        library = self.library()
        loop = RefreshLoop(library=library, config=refresh_config)
        loop.start()
        self._refresh_loop = loop
        return loop

    def library(self) -> Library:
        """Return the shared :class:`Library`, building it lazily."""
        if self._library is not None:
            return self._library
        if self._library_factory is not None:
            built = self._library_factory()  # type: ignore[operator]
            if not isinstance(built, Library):
                raise LibraryError(
                    "library_factory returned a non-Library value"
                )
            self._library = built
            return built

        config = self._config or ServerConfig.from_env()
        if config.auth_header is None:
            logger.warning(
                "no Artifactory credentials configured; set JFROG_AUTH "
                "or JFROG_EMAIL+JFROG_API_KEY"
            )
        cache = Cache(config.cache_dir)
        registry = RegistryClient(
            base_url=config.registry_base_url,
            auth_header=config.auth_header,
            verify=_tls_verify_value(config),
        )
        self._library = Library(config=config, registry=registry, cache=cache)
        return self._library


def _tls_verify_value(config: ServerConfig) -> bool | str:
    """Translate ``ServerConfig`` TLS knobs into an httpx ``verify=`` arg.

    Precedence:

    1. If a CA bundle path is set, use it (and ignore the insecure
       flag — explicit trust beats opt-out every time).
    2. Else if ``insecure_tls`` is True, return ``False`` so httpx
       skips verification entirely. A warning lands on stderr from the
       :class:`RegistryClient` constructor.
    3. Else return ``True`` so certifi's default bundle is used.

    Args:
        config (ServerConfig): resolved server configuration.

    Returns:
        bool | str: value suitable for ``httpx.Client(verify=...)``.
    """
    if config.ca_bundle is not None:
        return str(config.ca_bundle)
    return not config.insecure_tls


def _configure_logging() -> None:  # pragma: no cover - transport layer
    """Route all logging to stderr.

    Stdout is reserved for the MCP protocol on the stdio transport.
    Writing anything else to stdout would corrupt the JSON-RPC stream
    and silently break clients (PRD section 9, Security).
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:  # pragma: no cover - transport layer
    """Console-script entrypoint: serve MCP over stdio."""
    _configure_logging()
    logger.info("starting %s on stdio transport", SERVER_NAME)
    try:
        server = build_server()
    except ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        sys.exit(2)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
