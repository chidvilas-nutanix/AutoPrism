"""Entrypoint for the Prism ReactJS MCP server.

This module wires up the official ``mcp`` Python SDK's ``FastMCP`` over
the stdio transport and registers the v1 tool surface.

Slice 1 added the ``echo`` liveness tool.
Slice 2 adds ``get_library_meta`` and an Artifactory acquisition path.
Slices 3-6 add ``list_entities`` / ``get_entity`` / ``search_entities``
across components, hooks, managers, utils, and tokens.
Slice 7 wires a background refresh task into the FastMCP lifespan.
Slice 8 surfaces a clear "VPN required" error on cold-start-no-cache.

Stdout is reserved for the MCP JSON-RPC framing; all logging is routed
to stderr per the project's non-functional requirements.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from prism_mcp.cache import Cache
from prism_mcp.config import ConfigError, ServerConfig
from prism_mcp.entities import EntityType
from prism_mcp.figma import (
    FigmaTreeMapping,
    MapFigmaTreeInput,
    walk_tree,
)
from prism_mcp.figma.mocks import mock_path_for, try_load_mock
from prism_mcp.figma.fetch import (
    FetchError,
    FetchErrorCode,
    _fetch_figma_tree,
    parse_figma_url,
)
from prism_mcp.library import Library, LibraryError
from prism_mcp.library_assets import find_pwspec_example
from prism_mcp.refresh import RefreshLoop, RefreshLoopConfig
from prism_mcp.registry import RegistryClient
from prism_mcp.workflow import PRISM_TASK_QUEUE
from prism_mcp.workflow.contracts import (
    SubmitInput,
    UpdateCompanionTestsInput,
    WorkflowStartInput,
    build_delivery_hint,
    build_reflection_prompt,
)
from prism_mcp.workflow.figma_mapping import (
    map_figma_node as _build_figma_node_mapping,
)
from prism_mcp.workflow.reflection import build_reflection_context
from prism_mcp.workflow.ssim import compute_ssim_from_paths, materialise_image

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
prism-mcp turns Figma designs into validated Prism React components from
the @nutanix-ui/prism-reactjs library. Read this once, then follow the
canonical flow.

CANONICAL FIGMA -> PRISM FLOW

1. For each non-trivial frame/instance from Figma MCP:
   call `map_figma_node(node_name, node_type?, reference_code?, hex_colors?)`
   to get a ranked list of Prism components, related/co-imported components,
   matching design tokens, and imitation JSX examples in one round-trip.
   Always pick from the returned `candidates` list -- never invent a
   component name.

2. For composed components (multi-element, a11y matters, visual fidelity
   required), kick off the AlphaCodium iteration loop:
   call `start_generate_component(component_name, services_root,
   figma_png_url=..., spec_text=...)`. ALWAYS pass `figma_png_url`
   (or `figma_png_base64`) when Figma MCP gave you one -- the workflow
   uses it for SSIM visual diffing on every iteration. Pass `spec_text`
   (the Figma layer dump or a 1-2 sentence brief) so the searcher's
   retrieval is targeted; defaults to `component_name`.

   READ THE RESPONSE'S `context` FIELD BEFORE WRITING ANY CODE. It
   bundles imitation examples, related components, a11y guidance, and
   the closest existing pwspec.ts the lib ships -- everything you need
   to author iteration-1 JSX AND a meaningful companion pwspec in a
   single shot. Skipping it produces thin, smoke-only tests that the
   workflow then has to nudge you to refine.

3. Iterate: call `submit_candidate(workflow_id, jsx_code,
   companion_test_code=..., companion_spec_code=...)`. Author the
   pwspec from iteration 1 by imitating `context.imitation_pwspec.code`
   -- but mount the candidate directly (the lib's `playwright-util.visitPage`
   helper depends on the styleguide build at services/www and is NOT
   available for scratch components). End the pwspec with a
   `await page.screenshot({ path: 'playwright-output/<Name>.png',
   fullPage: false })` so SSIM can run.

   Read the returned validator panel; on failure, follow
   `reflection_prompt` verbatim before regenerating. Use
   `update_companion_tests` only when behaviour evolves and the existing
   companion tests need new assertions.

4. When a candidate passes, the response carries a `delivery_hint`.
   ALWAYS honour it: call `get_final_artefact(workflow_id)` and write
   the bytes into the user's actual project tree. The workflow's
   `services/src/scratch/Generated/` directory is the validator's cache,
   not the destination.

IMPORT STYLE -- USE THE PACKAGE NAME, NOT RELATIVE PATHS

Generated JSX must use consumer-style imports:

  import { Button, Modal, FlexLayout } from '@nutanix-ui/prism-reactjs';

The validator's jest config self-resolves `@nutanix-ui/prism-reactjs`
to the lib's local source, so package-name imports work in BOTH the
client app you're delivering to AND the validator's test environment.
Do NOT switch to relative imports like `../../components/v2/Button/Button`
inside scratch components -- that breaks the artefact for the client.

WHEN TO SKIP THE WORKFLOW

Trivial single-element components (a Button wrapper, an Icon swap, a
single-prop StatTile) don't need the iteration loop. Use `search_examples`
+ `get_entity` directly and return the JSX. The workflow adds 1-3 minutes
of wall time per component for tsc + eslint + jest + playwright + axe +
SSIM; that cost is justified for non-trivial work and overkill for
one-off wrappers.

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
   Read the response. `summary.input_nodes` is your sanity check; if
   it is 0, the URL or token was wrong. `agenda` is the ordered list
   of region decisions; `layout_tree` is the nested shape; `tokens`
   maps every visible hex to its closest Prism token; `dropped` is
   the audit trail (look for `unknown_type_fallback` or extreme
   `tiny_decorative` counts as a signal that the walker missed
   something).

E. Pre-render walkthrough.
   Echo `summary` to the user (especially `agenda_size`, top three
   `dropped_<reason>` buckets, and any `warnings`) before generating
   JSX. This is the user's chance to abort if the walker absorbed
   too much.

F. Compose top-down.
   For each `MappedRegion` in `agenda`, the `mapping.candidates[0]`
   is the suggested Prism component. Pick from `candidates` (never
   invent), import via the canonical `@nutanix-ui/prism-reactjs`
   path, and respect each region's `content_slots` (title / items /
   header / value / label) and `reference_jsx_slice` when supplied.

G. Validate.
   For non-trivial pages, kick off the AlphaCodium loop via
   `start_generate_component` per logical chunk. The walker's
   `tokens` map seeds the SSIM-friendly colour mapping; the
   `agenda`'s `reference_jsx_slice` slots feed the iteration-1
   prompt context.

H. Error handling.
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
    temporal_client_factory: object | None = None,
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
        temporal_client_factory (Callable[[], Awaitable[Client]] | None):
            slice-12 hook for injecting a stub Temporal client in
            tests. When ``None``, the first slice-12 workflow-tool
            call lazily opens a real connection to the local dev
            server. Kept as an opaque ``object | None`` so importers
            of this module don't pay a ``temporalio`` import cost
            when they only use the slice-1..11 tool surface.

    Returns:
        FastMCP: configured server instance with all tools wired up.
    """
    state = _ServerState(
        config=config,
        library_factory=library_factory,
        temporal_client_factory=temporal_client_factory,
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
        ``prism-mcp-setup`` and the dev-server smoke check to
        prove that the stdio transport is alive. The LLM should
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
    # Slice 12 — AlphaCodium iteration loop on Temporal.
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

        Slice 12 composite tool. The Figma-MCP gives Cursor a
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
                and ``figma_depth`` (per-call override for the REST
                ``depth`` query parameter — defaults to the
                fetcher's tuned value of 12, enough for real
                Nutanix designs).

        Returns:
            dict: ``FigmaTreeMapping`` shape (``layout_tree``,
            ``agenda``, ``tokens``, ``dropped``, ``summary``,
            ``warnings``).

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
                    "node_id=%s path=%s",
                    parsed.file_key,
                    parsed.node_id,
                    mock_path_for(parsed),
                )
                return mocked.model_dump()

        try:
            fetch_kwargs: dict[str, Any] = {
                "parsed": parsed,
                "figma_token": input.figma_token,
                "bypass_cache": input.bypass_cache,
            }
            if input.figma_depth is not None:
                fetch_kwargs["depth"] = max(1, int(input.figma_depth))
            tree_json = await _fetch_figma_tree(**fetch_kwargs)
        except FetchError as exc:
            raise ValueError(_fetch_error_to_mcp(exc)) from exc

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

        result: FigmaTreeMapping = walk_tree(
            tree_json=tree_json,
            reference_jsx=input.reference_jsx,
            variable_defs=input.variable_defs,
            max_depth=input.max_depth,
            max_nodes=input.max_nodes,
            max_agenda=input.max_agenda,
            map_figma_node_fn=_bound_map_figma_node,
        )
        return result.model_dump()

    @server.tool()
    def get_pwspec_example(
        component_name: str,
        services_root: str,
    ) -> dict[str, Any]:
        """INTERNAL (operator/debug only). Return the Prism ``<Name>.pwspec.ts``.

        Not part of the canonical Figma->Prism flow — the
        ``start_generate_component`` workflow auto-scaffolds a
        pwspec at iteration 1, so the LLM doesn't need to read
        the library's existing pwspec to imitate it. This tool
        is kept for human operators inspecting the test
        patterns the workflow's scaffold is based on.

        The Prism npm tarball *excludes* ``.pwspec.ts`` files
        (see ``services/package.json`` ``files`` field's
        ``!**/*.pwspec.ts`` exclusion), so the slice-1..11
        indices never see them. But those pwspecs are the
        **canonical Playwright + axe-core pattern** the
        AlphaCodium AI-test stage should imitate when writing
        a companion pwspec for a candidate.

        We read directly from the operator-supplied
        ``services_root`` instead of the tarball cache. The
        glob walks
        ``services/src/components/v2/<group>/<Name>.pwspec.ts``
        for any group; the first match wins.

        Caveat surfaced in the response's ``note`` field: the
        library pwspecs use Prism's ``playwright-util``
        helpers (``visitPage``, ``themes``, ``auditScreenshotHelper``)
        which assume the styleguide build at ``services/www``.
        Candidate pwspecs cannot use those helpers — they
        must mount the component directly. The structure
        (test.describe per theme, locator + visual + axe in
        one spec) is what the agent should copy.

        Args:
            component_name (str): the Prism component
                identifier (PascalCase, case-sensitive).
            services_root (str): absolute path to the Prism
                library's ``services/`` directory.

        Returns:
            dict: ``PwspecExample`` shape with ``component_name``,
            ``found``, ``path``, ``code`` (capped at 6 KB), and
            ``note``.
        """
        logger.info(
            "get_pwspec_example tool invoked component=%s services_root=%s",
            component_name,
            services_root,
        )
        return find_pwspec_example(
            services_root=services_root,
            component_name=component_name,
        ).model_dump()

    @server.tool()
    def compare_to_figma(
        rendered_png_path: str,
        figma_png_path: str | None = None,
        figma_png_url: str | None = None,
        figma_png_base64: str | None = None,
    ) -> dict[str, Any]:
        """Compute SSIM between a Figma export and a rendered screenshot.

        Slice 12 Tier 2 visual diff. Tolerates anti-aliasing,
        catches structural changes. Score >= 0.95 = pass,
        0.85..0.95 = warn, < 0.85 = fail. Per the screenshot-
        testing-2026 survey this is the right tier for component-
        granularity visual regression; LPIPS / DINOv2 add a 200MB+
        torch dep for marginal accuracy gain at our scale.

        Figma source flexibility
        ------------------------

        The Figma MCP returns screenshots as **short-lived signed
        URLs** by default (lowest token cost) and only emits inline
        base64 when the agent explicitly sets
        ``enableBase64Response: true``. Neither is a path. We accept
        all three input shapes so the LLM can forward whichever
        Figma MCP gave it without an intermediate "download to disk"
        step:

        * ``figma_png_path`` — pre-existing local PNG (back-compat).
        * ``figma_png_url`` — Figma signed URL; downloaded with
          a 30s timeout into a temp file before SSIM.
        * ``figma_png_base64`` — raw base64 PNG OR an RFC-2397
          ``data:image/png;base64,...`` data URL. Decoded into
          a temp file.

        Exactly one of the three Figma inputs must be set.

        Args:
            rendered_png_path (str): absolute path to the rendered
                screenshot PNG (produced by Playwright, on disk).
            figma_png_path (str | None): pre-existing local PNG.
            figma_png_url (str | None): HTTPS URL returned by the
                Figma MCP ``get_screenshot`` tool.
            figma_png_base64 (str | None): inline base64 / data URL.

        Returns:
            dict: ``{"score", "region", "bucket", "ok",
            "figma_png_resolved_path"}``. ``region`` is the 3x3 cell
            label of where the SSIM map is weakest (e.g.
            ``"top-left"``), or ``None`` when the score is already
            in the ``pass`` bucket. ``figma_png_resolved_path``
            echoes the on-disk path that was actually compared so
            the agent can re-use it (e.g. to feed
            ``start_generate_component(figma_png_path=...)``
            without re-downloading).
        """
        logger.info(
            "compare_to_figma tool invoked path=%s url=%s base64=%s rendered=%s",
            figma_png_path,
            "<set>" if figma_png_url else None,
            "<set>" if figma_png_base64 else None,
            rendered_png_path,
        )
        figma_resolved = materialise_image(
            path=figma_png_path,
            url=figma_png_url,
            base64_data=figma_png_base64,
        )
        verdict = compute_ssim_from_paths(
            figma_png=figma_resolved,
            rendered_png=Path(rendered_png_path),
        )
        payload = verdict.model_dump()
        payload["bucket"] = verdict.bucket
        payload["ok"] = verdict.ok
        payload["figma_png_resolved_path"] = str(figma_resolved)
        return payload

    @server.tool()
    async def start_generate_component(
        component_name: str,
        services_root: str,
        max_iterations: int = 3,
        figma_png_path: str | None = None,
        figma_png_url: str | None = None,
        figma_png_base64: str | None = None,
        spec_text: str | None = None,
    ) -> dict[str, Any]:
        """Kick off a Temporal workflow for the AlphaCodium iteration loop.

        Slice 12. Returns the workflow ID so subsequent
        ``submit_candidate`` calls can reference the same
        execution. Canonical Figma->Prism flow:

        1. ``map_figma_node`` → get candidate Prism components.
        2. ``start_generate_component`` (this tool) → reserve a
           workflow ID. **Always pass ``figma_png_url`` (or
           ``figma_png_base64``) when Figma MCP gave you one** —
           the workflow uses it for SSIM visual diffing on every
           iteration.
        3. ``submit_candidate`` repeatedly until ``all_passed``
           or ``max_iterations`` exhausted.
        4. On pass, honour the ``delivery_hint`` and call
           ``get_final_artefact``.

        Figma reference channels (any one works)
        ----------------------------------------

        Pass at most one of these (priority order: path > url >
        base64). When all three are ``None`` the workflow skips
        the SSIM stage entirely and the reflection prompt nudges
        the LLM to supply a reference on the next start.

        * ``figma_png_path`` — local PNG already on disk. Cheapest.
        * ``figma_png_url`` — HTTPS URL (Figma signed link).
          Downloaded once at workflow start, cached for every
          iteration's SSIM. **This is the typical Figma MCP shape.**
        * ``figma_png_base64`` — inline base64 PNG. Decoded once
          at workflow start. Useful when Figma MCP returned the
          raw image bytes.

        Up-front context (``context`` field in the response)
        ----------------------------------------------------

        The response carries a ``context`` bundle the LLM should
        read **before** generating iteration-1 code. It is built
        synchronously from the slice-9..11 indices that already
        sit on the Library:

        * ``examples`` — top-3 imitation JSX snippets retrieved
          via the hybrid (BM25 + dense) searcher.
        * ``related`` — composition-graph neighbours so the LLM
          knows which sibling components the design system
          typically composes alongside ``component_name``.
        * ``token_hints`` — design-token hints for any hex
          literals in ``spec_text``.
        * ``a11y_blocks`` — per-component a11y guidance pulled
          from the colocated ``.examples.md`` file.
        * ``imitation_pwspec`` — the closest-matching existing
          pwspec from the lib (``services/src/components/v2/<X>/<X>.pwspec.ts``)
          including its truncated body. Use this as the
          authoring template when refining the auto-scaffold via
          ``update_companion_tests`` — note its ``visitPage``
          helper does NOT work for scratch components and the
          scaffold must mount directly.

        Surfacing this at workflow start eliminates the
        previous "iteration-1 had no a11y context, scaffolds
        were trivial, refinements were guesses" failure mode
        the May-2026 testing surfaced.

        Args:
            component_name (str): PascalCase identifier.
            services_root (str): absolute path to the Prism
                library's ``services/`` directory (where the
                validators run).
            max_iterations (int): bounded loop cap. Per
                AlphaCodium's ablation, gains plateau by
                iteration 3-4.
            figma_png_path (str | None): on-disk Figma PNG path.
            figma_png_url (str | None): HTTPS URL to download.
            figma_png_base64 (str | None): inline base64 PNG body.
            spec_text (str | None): optional free-form spec body
                (Figma layer dump, ticket text, etc.). Used as
                the hybrid searcher's query and for hex-literal
                extraction. Defaults to ``component_name`` when
                omitted, which still yields useful retrieval for
                callers who don't have a spec.

        Returns:
            dict: ``{"workflow_id", "component_name", "task_queue", "context"}``
            where ``context`` mirrors :class:`ReflectionContext`'s
            fields plus an ``imitation_pwspec`` sub-dict.
        """
        logger.info(
            "start_generate_component tool invoked name=%s services=%s "
            "figma_path=%s figma_url=%s figma_b64=%s spec_chars=%d",
            component_name,
            services_root,
            figma_png_path is not None,
            figma_png_url is not None,
            figma_png_base64 is not None,
            len(spec_text) if spec_text else 0,
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        workflow_id = f"prism-gen-{component_name}-{uuid.uuid4()}"
        handle = await client.start_workflow(  # type: ignore[attr-defined]
            GenerateComponentWorkflow.run,
            WorkflowStartInput(
                component_name=component_name,
                services_root=services_root,
                max_iterations=max_iterations,
                figma_png_path=figma_png_path,
                figma_png_url=figma_png_url,
                figma_png_base64=figma_png_base64,
            ),
            id=workflow_id,
            task_queue=PRISM_TASK_QUEUE,
        )
        # Build the up-front context bundle. Failures here must NOT
        # block the workflow start — the LLM can still iterate without
        # it. We log + return an empty context shape on failure so the
        # response schema stays stable.
        context = _build_start_context(
            state=state,
            component_name=component_name,
            services_root=services_root,
            spec_text=spec_text,
        )
        return {
            "workflow_id": handle.id,
            "component_name": component_name,
            "task_queue": PRISM_TASK_QUEUE,
            "context": context,
        }

    @server.tool()
    async def submit_candidate(
        workflow_id: str,
        jsx_code: str,
        companion_test_code: str | None = None,
        companion_spec_code: str | None = None,
    ) -> dict[str, Any]:
        """Send a candidate to the workflow's update handler.

        Slice 12. Synchronously waits for the workflow to run the
        validator chain (typecheck → eslint → jest → playwright
        + axe → SSIM if applicable) and returns the
        :class:`CandidateResult`. On failure the response also
        carries the ReflexiCoder-style 3-question reflection
        prompt — Cursor's next code-gen step gets the prompt
        verbatim, which is the SOTA pattern that pushed
        open-source 8B models to 94.51% HumanEval pass@1.

        Companion test files
        --------------------

        At iteration 1 the workflow auto-scaffolds a minimal
        Playwright pwspec + Jest spec under the candidate's
        scratch dir, so neither field is required for the
        validator chain to run. Supply them only when you have
        behaviour-specific assertions to add. The dedicated
        :func:`update_companion_tests` tool is usually a
        cleaner channel for refinement than wedging tests
        into every ``submit_candidate`` payload.

        Args:
            workflow_id (str): the ID returned by
                ``start_generate_component``.
            jsx_code (str): the candidate JSX body. Written to
                ``services/src/scratch/Generated/<Name>/<Name>.jsx``.
            companion_test_code (str | None): optional pwspec.ts
                body. ``None`` triggers the auto-scaffold (first
                iteration) or preserves prior content (later
                iterations).
            companion_spec_code (str | None): optional jest
                spec.tsx body. Same write-once-then-preserve
                semantics.

        Returns:
            dict: the :class:`CandidateResult` payload plus three
            agent-facing helper fields:

            * ``all_passed`` / ``failing_kinds``: derived aggregates
              of the per-validator list, surfaced explicitly so the
              LLM doesn't have to recompute them.
            * ``reflection_prompt``: ReflexiCoder-style 3-question
              prompt when the candidate failed; ``""`` on pass.
            * ``delivery_hint``: when the candidate passed, a
              concrete instruction telling the agent to call
              :func:`get_final_artefact` next and write the bytes
              into the user's actual project tree. ``""`` on
              fail so a wrapping client can use it as a falsy
              guard. **Always honour this hint on pass** — the
              scratch dir is the validator's cache, not the
              destination the user expects.
        """
        logger.info(
            "submit_candidate tool invoked workflow_id=%s jsx_len=%d "
            "pwspec_supplied=%s spec_supplied=%s",
            workflow_id,
            len(jsx_code),
            companion_test_code is not None,
            companion_spec_code is not None,
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(
                jsx_code=jsx_code,
                companion_test_code=companion_test_code,
                companion_spec_code=companion_spec_code,
            ),
        )
        payload = result.model_dump()
        # ``all_passed`` and ``failing_kinds`` are @property fields
        # — Pydantic ``model_dump`` skips them. Surface them
        # explicitly so the LLM doesn't have to recompute the
        # aggregate from the per-validator list.
        payload["all_passed"] = result.all_passed
        payload["failing_kinds"] = result.failing_kinds
        payload["reflection_prompt"] = build_reflection_prompt(result)
        payload["delivery_hint"] = (
            build_delivery_hint(
                workflow_id=workflow_id,
                component_name=result.component_name,
            )
            if result.all_passed
            else ""
        )
        return payload

    @server.tool()
    async def update_companion_tests(
        workflow_id: str,
        pwspec_code: str | None = None,
        spec_code: str | None = None,
    ) -> dict[str, Any]:
        """Refine the auto-scaffolded Playwright + Jest tests in place.

        At iteration 1 the workflow auto-scaffolds a minimal
        pwspec.ts and spec.tsx so the validator chain has
        something to run. Once a candidate's behaviour is stable,
        call this tool to upgrade the scaffolds with
        behaviour-specific assertions:

        * **pwspec.ts** is the place for ``@axe-core/playwright``
          a11y assertions, screenshot diffs, and interaction
          flow tests.
        * **spec.tsx** is the place for unit-level
          render/event/state-transition assertions using
          ``@testing-library/react``.

        Either argument can be ``None`` to leave that file
        untouched. Refining tests does *not* re-run the validator
        chain — call ``submit_candidate`` afterwards (with the
        same JSX, or new JSX) to validate against the refined
        tests.

        Args:
            workflow_id (str): the running workflow's ID.
            pwspec_code (str | None): full pwspec.ts body, or
                ``None`` to leave the existing pwspec alone.
            spec_code (str | None): full spec.tsx body, or
                ``None`` to leave the existing spec alone.

        Returns:
            dict: the :class:`UpdateCompanionTestsResult` payload —
            ``component_name``, ``wrote_pwspec``, ``wrote_spec``,
            ``pwspec_path``, ``spec_path``, ``next_step_hint``.
        """
        logger.info(
            "update_companion_tests tool invoked workflow_id=%s "
            "pwspec_supplied=%s spec_supplied=%s",
            workflow_id,
            pwspec_code is not None,
            spec_code is not None,
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
        result = await handle.execute_update(
            GenerateComponentWorkflow.update_companion_tests,
            UpdateCompanionTestsInput(
                pwspec_code=pwspec_code,
                spec_code=spec_code,
            ),
        )
        return result.model_dump()

    @server.tool()
    async def get_component_status(workflow_id: str) -> dict[str, Any]:
        """INTERNAL (operator/demo-UI only). Poll a workflow's status.

        Not part of the canonical Figma->Prism flow — the LLM
        gets the validator panel synchronously from
        ``submit_candidate``, so it never needs to poll. This
        tool is kept for the demo UI and human operators
        watching iteration progress.

        Slice 12. Useful for the demo UI (or a watchful agent) to
        observe iteration progress without sending an update.

        **When ``final_state == "passed"``, the returned payload's
        ``delivery_hint`` field is non-empty and instructs the
        agent to call :func:`get_final_artefact` next.** The
        scratch dir is the validator's working cache — the
        artefact tool is how the LLM retrieves the validated code
        for placement into the user's actual project.

        Args:
            workflow_id (str): the ID returned by
                ``start_generate_component``.

        Returns:
            dict: the :class:`WorkflowStatus` payload —
            ``{"workflow_id", "component_name", "services_root",
            "iteration", "max_iterations", "last_result",
            "final_state", "delivery_hint"}``.
        """
        logger.info(
            "get_component_status tool invoked workflow_id=%s", workflow_id
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
        status = await handle.query(GenerateComponentWorkflow.status)
        return status.model_dump()

    @server.tool()
    async def get_final_artefact(workflow_id: str) -> dict[str, Any]:
        """Retrieve the validated artefact for a passing workflow.

        Slice 12.5 — closes the "delivery gap" in the iteration
        loop: ``services/src/scratch/Generated/<Name>/`` is the
        validator's working directory, not a destination the user
        expects. This tool reads the validated JSX (and any
        companion pwspec + the scoped tsconfig) from that scratch
        tree and returns the bytes so the LLM agent can write
        them into the **user's actual project**.

        **Required agent flow** (the LLM must follow this):

        1. After ``submit_candidate`` returns ``all_passed=True``
           (or ``get_component_status`` returns
           ``final_state="passed"``), call this tool.
        2. Take the returned ``jsx_code`` (and ``companion_test_code``
           if present) and write them into the user's project tree
           at a path the user controls — typically
           ``<user-project>/src/components/<Name>/<Name>.jsx``.
        3. Do **not** point the user at the scratch dir; treat
           it as ephemeral validator state. Adding
           ``services/src/scratch/`` to ``.gitignore`` is the
           recommended companion change.

        Args:
            workflow_id (str): the ID returned by
                ``start_generate_component``. The workflow must
                be in ``final_state="passed"``; calls against
                non-passed workflows succeed but include a
                ``warning`` field so the agent knows the artefact
                may not reflect a clean validation.

        Returns:
            dict:

            * ``workflow_id``: echoed.
            * ``component_name``: from the workflow status.
            * ``final_state``: current workflow state for safety
              cross-check.
            * ``services_root``: the root the artefacts live under.
            * ``scratch_dir``: absolute path to the scratch tree
              for this component (the cache, *not* the
              destination).
            * ``jsx_code``: the JSX body the validators accepted.
            * ``companion_test_code`` (``str | None``): the pwspec
              body if Cursor produced one, else ``None``.
            * ``tsconfig_json``: the scoped tsconfig the
              validator chain used.
            * ``suggested_target_path``: a *suggestion* (not a
              policy decision) of where in a typical React
              project the artefact would live; the agent should
              defer to whatever path the user specifies.
            * ``warning`` (``str | None``): non-empty when the
              workflow hasn't reached ``passed`` yet.
        """
        logger.info(
            "get_final_artefact tool invoked workflow_id=%s", workflow_id
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
        status = await handle.query(GenerateComponentWorkflow.status)

        scratch_dir = Path(
            status.services_root,
            "src",
            "scratch",
            "Generated",
            status.component_name,
        )
        jsx_path = scratch_dir / f"{status.component_name}.jsx"
        pwspec_path = scratch_dir / f"{status.component_name}.pwspec.ts"
        tsconfig_path = scratch_dir / "tsconfig.json"

        if not jsx_path.is_file():
            # Hard failure — the workflow's history says a
            # submission was accepted, but the JSX isn't on disk.
            # Most likely cause: scratch dir was wiped between
            # submission and this call (e.g. operator ran
            # ``rm -rf scratch``). Surface a typed error rather
            # than silently returning ``None``.
            raise FileNotFoundError(
                f"No scratch JSX found at {jsx_path}. The workflow "
                f"status reports component_name={status.component_name!r}, "
                f"services_root={status.services_root!r}, "
                f"final_state={status.final_state!r}. Either the "
                "scratch dir was cleared between submission and "
                "this call, or the workflow never reached a "
                "submit_candidate that wrote files."
            )

        warning: str | None = None
        if status.final_state != "passed":
            warning = (
                f"Workflow is in state {status.final_state!r}, not 'passed'. "
                "The returned artefact reflects the most recent submission "
                "but may not have passed the full validator chain. Re-check "
                "`last_result.failing_kinds` before delivering to the user."
            )

        return {
            "workflow_id": workflow_id,
            "component_name": status.component_name,
            "final_state": status.final_state,
            "services_root": status.services_root,
            "scratch_dir": str(scratch_dir),
            "jsx_code": jsx_path.read_text(encoding="utf-8"),
            "companion_test_code": (
                pwspec_path.read_text(encoding="utf-8")
                if pwspec_path.is_file()
                else None
            ),
            "tsconfig_json": (
                tsconfig_path.read_text(encoding="utf-8")
                if tsconfig_path.is_file()
                else None
            ),
            "suggested_target_path": (
                f"<user-project>/src/components/"
                f"{status.component_name}/{status.component_name}.jsx"
            ),
            "warning": warning,
        }

    return server


# --------------------------------------------------------------------------
# start_generate_component context bundling. Lives at module scope (not as
# a closure inside build_server) so the test suite can exercise the
# fallback behaviour without standing up a real FastMCP.
# --------------------------------------------------------------------------


def _build_start_context(
    *,
    state: _ServerState,
    component_name: str,
    services_root: str,
    spec_text: str | None,
) -> dict[str, Any]:
    """Build the up-front context bundle for ``start_generate_component``.

    Synchronously fans out to the slice-9..11 indices on the
    Library plus the slice-12 ``find_pwspec_example`` helper, so
    the LLM has imitation examples, a11y blocks, design tokens,
    and an authoring template for the companion pwspec before it
    generates iteration-1 code.

    Failure modes are *soft*: if the Library can't be acquired
    (e.g. cold-start no-cache, or VPN missing) or the pwspec
    glob misses, we return an empty bundle rather than aborting
    the workflow start. The validator chain itself still runs
    fine without context — the LLM just iterates with thinner
    inputs.

    Args:
        state (_ServerState): the server's lazy state holder.
            Provides the Library reference.
        component_name (str): the workflow target.
        services_root (str): absolute path to the Prism library's
            ``services/`` directory. Used by
            :func:`find_pwspec_example` to locate the imitation
            template on disk.
        spec_text (str | None): free-form spec body for the
            searcher. ``None`` falls back to ``component_name``
            as the query so retrieval still produces hits.

    Returns:
        dict: a JSON-serialisable bundle with keys
        ``examples``, ``related``, ``token_hints``, ``a11y_blocks``,
        ``candidate_decompositions``, and ``imitation_pwspec``.
        Each list defaults to empty on lookup failure;
        ``imitation_pwspec`` is always present and reports
        ``found=False`` when no pwspec matches.
    """
    query_text = (
        spec_text if spec_text and spec_text.strip() else component_name
    )
    empty_context = {
        "component_name": component_name,
        "examples": [],
        "related": [],
        "token_hints": [],
        "a11y_blocks": [],
        "candidate_decompositions": [],
    }
    pwspec_dict: dict[str, Any]
    try:
        pwspec = find_pwspec_example(
            services_root=services_root,
            component_name=component_name,
        )
        pwspec_dict = pwspec.model_dump()
    except Exception:
        logger.exception(
            "find_pwspec_example failed component=%s services_root=%s",
            component_name,
            services_root,
        )
        pwspec_dict = {
            "component_name": component_name,
            "found": False,
            "path": None,
            "code": None,
            "note": "imitation pwspec lookup failed (see server logs)",
        }
    try:
        library = state.library()
        reflection = build_reflection_context(
            component_name=component_name,
            spec_text=query_text,
            hybrid_searcher=library.hybrid_searcher(),
            composition_graph=library.composition_graph(),
            color_token_index=library.color_token_index(),
            a11y_rules=library.a11y_rules(),
        )
        context = reflection.model_dump()
    except Exception:
        logger.exception(
            "build_reflection_context failed component=%s; returning empty bundle",
            component_name,
        )
        context = empty_context
    context["imitation_pwspec"] = pwspec_dict
    logger.info(
        "built start-context component=%s examples=%d related=%d "
        "tokens=%d a11y=%d pwspec_found=%s",
        component_name,
        len(context.get("examples", [])),
        len(context.get("related", [])),
        len(context.get("token_hints", [])),
        len(context.get("a11y_blocks", [])),
        pwspec_dict.get("found", False),
    )
    return context


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
        temporal_client_factory: object | None = None,
    ) -> None:
        self._config = config
        self._library_factory = library_factory
        self._library: Library | None = None
        self._refresh_loop: RefreshLoop | None = None
        self._temporal_client_factory = temporal_client_factory
        # Cached Temporal client. Built lazily on first slice-12
        # workflow tool call so importing this module never opens
        # a network connection.
        self._temporal_client: object | None = None

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

    async def temporal_client(self) -> object:
        """Return the cached Temporal client, building it lazily.

        Slice 12. Importing :mod:`temporalio.client` inside this
        method keeps the slice 1..11 import path (which doesn't
        need Temporal) free of the dependency. The factory hook
        lets tests inject a stub :class:`MagicMock` shaped like
        the real :class:`temporalio.client.Client`.
        """
        if self._temporal_client is not None:
            return self._temporal_client
        if self._temporal_client_factory is not None:
            self._temporal_client = await self._temporal_client_factory()  # type: ignore[misc, operator]
            return self._temporal_client
        # Lazy production import so slice 1..11 importers aren't
        # forced to install temporalio just to load the server.
        from temporalio.client import Client
        from temporalio.contrib.pydantic import pydantic_data_converter

        from prism_mcp.workflow.worker import DEFAULT_SERVER_ADDRESS

        logger.info(
            "connecting Temporal client target=%s", DEFAULT_SERVER_ADDRESS
        )
        self._temporal_client = await Client.connect(
            DEFAULT_SERVER_ADDRESS,
            data_converter=pydantic_data_converter,
        )
        return self._temporal_client


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
