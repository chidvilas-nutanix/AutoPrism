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

from prism_mcp.a11y import get_a11y_for_component
from prism_mcp.cache import Cache
from prism_mcp.color import apca_contrast, hex_to_rgb, wcag_contrast_ratio
from prism_mcp.config import ConfigError, ServerConfig
from prism_mcp.entities import EntityType
from prism_mcp.library import Library, LibraryError
from prism_mcp.refresh import RefreshLoop, RefreshLoopConfig
from prism_mcp.registry import RegistryClient
from prism_mcp.workflow import PRISM_TASK_QUEUE
from prism_mcp.workflow.contracts import (
    SubmitInput,
    WorkflowStartInput,
    build_delivery_hint,
    build_reflection_prompt,
)
from prism_mcp.workflow.reflection import build_reflection_context
from prism_mcp.workflow.ssim import compute_ssim_from_paths

logger = logging.getLogger(__name__)

SERVER_NAME = "prism-mcp"
ECHO_REPLY = "prism-mcp: alive"


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

    server = FastMCP(SERVER_NAME, lifespan=lifespan)

    @server.tool()
    def echo() -> str:
        """Return a fixed liveness string.

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
    def list_entities(
        type: EntityType | None = None,
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        """List indexed Prism entities, optionally filtered by ``type``.

        Args:
            type (EntityType | None): when set, restrict to
                ``"component"`` / ``"hook"`` / ``"manager"`` /
                ``"util"`` / ``"token"``. Slice 3 only populates
                components; later slices add the rest.
            include_deprecated (bool): include entities flagged
                ``deprecated``. Default ``False`` because LLMs rarely
                want those.

        Returns:
            dict: ``{"entities": [{"name": ..., "type": ...,
            "summary": ..., "deprecated": ...}, ...], "version": ...}``.
            The list is a *summary* projection — call ``get_entity`` for
            the full signature and examples.
        """
        logger.info(
            "list_entities tool invoked type=%s deprecated=%s",
            type,
            include_deprecated,
        )
        index = state.library().index()
        entries = index.list(type=type, include_deprecated=include_deprecated)
        return {
            "version": index.version,
            "entities": [
                {
                    "name": entity.name,
                    "type": entity.type,
                    "summary": entity.summary,
                    "category": entity.category,
                    "deprecated": entity.deprecated,
                }
                for entity in entries
            ],
        }

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

    @server.tool()
    def map_token(
        hex: str,
        top_k: int = 3,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Find the closest Prism color tokens to ``hex`` (slice 11 SOTA).

        Slice 11 SOTA: perceptual color-distance search over the
        ``Colors.less`` palette. Used by the agent loop when Cursor
        sees a hex from Figma and needs to know which design-system
        token to reference instead of inlining the literal.

        Ranking:

        * **Primary**: Oklab Euclidean distance (Ottosson 2020,
          screen-tuned, the same metric Tailwind v4 / Radix Colors /
          shadcn/ui use).
        * **Tiebreak**: CIEDE2000 ΔE in CIE Lab (industry-standard
          25-year-old perceptual threshold metric).
        * **Bucket**: the ΔE2000 distance is bucketed as
          ``exact`` (≤2) / ``near`` (≤5) / ``loose`` (≤10) /
          ``no-match`` (>10).

        Optional ``role`` narrows the candidate set to tokens whose
        name contains the role's keywords (surface, text, interactive,
        success, warning, danger, focus). If the narrowed set's best
        ΔE > 5 we fall back to the global ranking — the LLM is never
        left empty-handed by an unhelpful role hint.

        Args:
            hex (str): target color, e.g. ``"#1B6BCC"``.
            top_k (int): max matches to return (default 3).
            role (str | None): optional semantic-role hint.

        Returns:
            dict: ``{"version": ..., "matches": [{...}]}`` where each
            match carries ``name``, ``hex``, ``source_file``,
            ``distance_oklab``, ``distance_de2000``, ``bucket``.
        """
        logger.info(
            "map_token tool invoked hex=%s top_k=%d role=%s",
            hex,
            top_k,
            role,
        )
        index = state.library().color_token_index()
        matches = index.query(target_hex=hex, top_k=top_k, role=role)
        return {
            "version": index.version,
            "matches": [m.model_dump() for m in matches],
        }

    @server.tool()
    def check_contrast(fg_hex: str, bg_hex: str) -> dict[str, Any]:
        """Return WCAG 2.1 + APCA Lc contrast for a fg/bg color pair.

        Slice 11 SOTA: the accessibility guardrail. Returns both
        metrics in one call so Cursor's generation loop can pick the
        right one:

        * **WCAG 2.1 ratio** (1..21) — what axe-core, Lighthouse, and
          most current CI a11y validators check against. Reports
          ``aa_normal_text`` (≥4.5:1) and ``aa_large_text`` (≥3:1).
        * **APCA Lc** (~-108..+106, polarity-sensitive) — the
          modern WCAG 3 draft metric. Recommended thresholds:
          ``|Lc| >= 75`` body text, ``>= 60`` headlines,
          ``>= 45`` fluent, ``>= 30`` non-content / icons.

        APCA's polarity (positive = dark text on light bg, negative
        = light text on dark bg) means the same pair gets two
        different magnitudes when reversed — that's intentional;
        the eye reads dark-on-light differently from light-on-dark
        at small sizes.

        Args:
            fg_hex (str): foreground color (text, icon).
            bg_hex (str): background color (panel, page).

        Returns:
            dict: contrast metrics and pass/fail flags.
        """
        logger.info(
            "check_contrast tool invoked fg=%s bg=%s",
            fg_hex,
            bg_hex,
        )
        fg = hex_to_rgb(fg_hex)
        bg = hex_to_rgb(bg_hex)
        ratio = float(wcag_contrast_ratio(fg, bg))
        lc = float(apca_contrast(fg, bg))
        return {
            "fg_hex": fg_hex,
            "bg_hex": bg_hex,
            "wcag_21_ratio": round(ratio, 4),
            "wcag_aa_normal_text": ratio >= 4.5,
            "wcag_aa_large_text": ratio >= 3.0,
            "wcag_aaa_normal_text": ratio >= 7.0,
            "wcag_aaa_large_text": ratio >= 4.5,
            "apca_lc": round(lc, 2),
            "apca_passes_body_text": abs(lc) >= 75.0,
            "apca_passes_headline": abs(lc) >= 60.0,
            "apca_passes_fluent": abs(lc) >= 45.0,
            "apca_passes_non_content": abs(lc) >= 30.0,
        }

    @server.tool()
    def get_a11y_rules(
        component_name: str | None = None,
    ) -> dict[str, Any]:
        """Return a11y guidance for the library or a specific component.

        Slice 11: surfaces both layers of Prism's a11y prose so the
        LLM can read them as input context before generating code:

        * **Global rules** parsed from ``package/LLMS.md`` — H2/H3
          sections, in document order.
        * **Per-component blocks** extracted from
          ``*.examples.md`` chunks flagged ``is_a11y_block`` (the
          slice-9 parser already marked them).

        Args:
            component_name (str | None): when supplied, narrow the
                ``per_component`` slice to just that component. The
                ``global_rules`` are always returned (they apply to
                every component). Case-sensitive — matches Prism's
                identifier convention.

        Returns:
            dict: ``{"title", "global_rules", "per_component"}``.
        """
        logger.info("get_a11y_rules tool invoked component=%s", component_name)
        rules = state.library().a11y_rules()
        if component_name is None:
            return rules.model_dump()
        match = get_a11y_for_component(component_name, rules)
        return {
            "title": rules.title,
            "global_rules": [s.model_dump() for s in rules.global_rules],
            "per_component": (
                [match.model_dump()] if match is not None else []
            ),
        }

    @server.tool()
    def related_components(
        name: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Return components most often co-imported with ``name``.

        Slice 10 SOTA — the *local* half of dual-level (LightRAG-
        flavored) retrieval. The composition graph counts how often
        any two components appear together in a single ``*.examples.md``
        ``jsx`` fence; this tool returns the top-``k`` neighbours
        of ``name`` ranked by that co-occurrence weight.

        Use this when the agent has chosen one component and needs
        to know the canonical partners — e.g. "I picked ``Modal``;
        what does Prism's example corpus show me to compose with
        it?" → ``Button``, ``StackingLayout``, ``FormItemInput``.

        Ranking:

        * **Primary**: edge weight (the count of example chunks
          co-importing both ``name`` and the neighbour).
        * **Tiebreak**: alphabetical, for deterministic output.

        Args:
            name (str): the component identifier to anchor the
                query on (case-sensitive, must match a Prism
                exported name).
            top_k (int): maximum neighbours to return (default 5).

        Returns:
            dict: ``{"version", "name", "related": [{name, weight}]}``.

        Raises:
            GraphError: when ``name`` is not present in the
                composition graph or ``top_k <= 0``. Surfaced to
                the client as an MCP tool error.
        """
        logger.info(
            "related_components tool invoked name=%s top_k=%d",
            name,
            top_k,
        )
        graph = state.library().composition_graph()
        neighbours = graph.related(name=name, top_k=top_k)
        return {
            "version": graph.version,
            "name": name,
            "related": [n.model_dump() for n in neighbours],
        }

    @server.tool()
    def get_component_cluster(name: str) -> dict[str, Any]:
        """Return the Louvain community ``name`` belongs to.

        Slice 10 SOTA — the *global* half of dual-level retrieval.
        Louvain (with ``seed=42`` for determinism) partitions the
        composition graph into communities of components that
        frequently compose with one another. This tool returns
        ``name``'s community: full member list, the up-to-three
        most-central members (highest weighted degree inside the
        community), and a one-string ``label`` that's the single
        most-central member.

        Use this when the agent wants context about what *kind* of
        component family ``name`` belongs to — e.g. "is ``Modal``
        in the form-composition family or the navigation family?".

        The ``central_members`` are the LLM's hook for narrating
        the cluster ("the form-composition layer: ``FormItemInput``,
        ``Button``, ``StackingLayout``") without us paying the cost
        of an LLM call at index time.

        Args:
            name (str): the component identifier (case-sensitive).

        Returns:
            dict: ``{"version", "name", "cluster_id", "label",
            "central_members", "members"}``.

        Raises:
            GraphError: when ``name`` is not in the composition
                graph. Surfaced to the client as an MCP tool error.
        """
        logger.info("get_component_cluster tool invoked name=%s", name)
        graph = state.library().composition_graph()
        info = graph.cluster(name)
        return {
            "version": graph.version,
            "name": name,
            "cluster_id": info.cluster_id,
            "label": info.label,
            "central_members": info.central_members,
            "members": info.members,
        }

    # ------------------------------------------------------------------
    # Slice 12 — AlphaCodium iteration loop on Temporal.
    # ------------------------------------------------------------------

    @server.tool()
    def reflect_on_spec(
        component_name: str,
        spec_text: str,
        hex_colors: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pre-process scaffold for AlphaCodium-style code generation.

        Slice 12. Fans out to the existing slice-9 hybrid searcher,
        slice-10 composition graph, and slice-11 color-token +
        a11y indices to build a structured
        :class:`ReflectionContext` the LLM reads *before*
        generating JSX. Per the AlphaCodium paper, this self-
        reflection stage is one of the two largest contributors
        to the iteration loop's pass@k gain (along with
        AI-generated tests).

        Args:
            component_name (str): the spec's target component
                (PascalCase, case-sensitive).
            spec_text (str): the free-form spec body. Used as the
                hybrid-searcher query and as the source of hex
                literals for token hinting when ``hex_colors``
                isn't supplied.
            hex_colors (list[str] | None): explicit hex colours.
                When supplied this overrides ``spec_text`` hex
                extraction — useful when Cursor has already
                parsed a Figma export and knows the exact
                colours up front.

        Returns:
            dict: ``{"component_name", "examples", "related",
            "token_hints", "a11y_blocks", "candidate_decompositions"}``.
        """
        logger.info(
            "reflect_on_spec tool invoked name=%s spec_len=%d",
            component_name,
            len(spec_text),
        )
        library = state.library()
        context = build_reflection_context(
            component_name=component_name,
            spec_text=spec_text,
            hybrid_searcher=library.hybrid_searcher(),
            composition_graph=library.composition_graph(),
            color_token_index=library.color_token_index(),
            a11y_rules=library.a11y_rules(),
            hex_colors=hex_colors,
        )
        return context.model_dump()

    @server.tool()
    def compare_to_figma(
        figma_png_path: str,
        rendered_png_path: str,
    ) -> dict[str, Any]:
        """Compute SSIM between a Figma export and a rendered screenshot.

        Slice 12 Tier 2 visual diff. Tolerates anti-aliasing,
        catches structural changes. Score >= 0.95 = pass,
        0.85..0.95 = warn, < 0.85 = fail. Per the screenshot-
        testing-2026 survey this is the right tier for component-
        granularity visual regression; LPIPS / DINOv2 add a 200MB+
        torch dep for marginal accuracy gain at our scale.

        Args:
            figma_png_path (str): absolute path to the design
                reference PNG.
            rendered_png_path (str): absolute path to the rendered
                screenshot PNG.

        Returns:
            dict: ``{"score", "region", "bucket", "ok"}``.
            ``region`` is the 3x3 cell label of where the SSIM
            map is weakest (e.g. ``"top-left"``), or ``None``
            when the score is already in the ``pass`` bucket.
        """
        logger.info(
            "compare_to_figma tool invoked figma=%s rendered=%s",
            figma_png_path,
            rendered_png_path,
        )
        verdict = compute_ssim_from_paths(
            figma_png=Path(figma_png_path),
            rendered_png=Path(rendered_png_path),
        )
        payload = verdict.model_dump()
        payload["bucket"] = verdict.bucket
        payload["ok"] = verdict.ok
        return payload

    @server.tool()
    async def start_generate_component(
        component_name: str,
        services_root: str,
        max_iterations: int = 3,
        figma_png_path: str | None = None,
    ) -> dict[str, Any]:
        """Kick off a Temporal workflow for the AlphaCodium iteration loop.

        Slice 12. Returns the workflow ID so subsequent
        ``submit_candidate`` / ``get_component_status`` calls can
        reference the same execution. Cursor's typical flow:

        1. ``reflect_on_spec`` → get retrieved context.
        2. ``start_generate_component`` → reserve a workflow ID.
        3. ``submit_candidate`` repeatedly until ``all_passed``
           or ``max_iterations`` exhausted.

        Args:
            component_name (str): PascalCase identifier.
            services_root (str): absolute path to the Prism
                library's ``services/`` directory (where the
                validators run).
            max_iterations (int): bounded loop cap. Per
                AlphaCodium's ablation, gains plateau by
                iteration 3-4.
            figma_png_path (str | None): when set, the workflow
                runs SSIM at the end of every all-pass iteration
                against this Figma reference. When ``None``, the
                SSIM stage is skipped.

        Returns:
            dict: ``{"workflow_id", "component_name", "task_queue"}``.
        """
        logger.info(
            "start_generate_component tool invoked name=%s services=%s",
            component_name,
            services_root,
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
            ),
            id=workflow_id,
            task_queue=PRISM_TASK_QUEUE,
        )
        return {
            "workflow_id": handle.id,
            "component_name": component_name,
            "task_queue": PRISM_TASK_QUEUE,
        }

    @server.tool()
    async def submit_candidate(
        workflow_id: str,
        jsx_code: str,
        companion_test_code: str | None = None,
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

        Args:
            workflow_id (str): the ID returned by
                ``start_generate_component``.
            jsx_code (str): the candidate JSX body. Written to
                ``services/src/scratch/Generated/<Name>/<Name>.jsx``.
            companion_test_code (str | None): optional pwspec.ts
                body produced by the AlphaCodium AI-test stage.

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
            "submit_candidate tool invoked workflow_id=%s jsx_len=%d",
            workflow_id,
            len(jsx_code),
        )
        client = await state.temporal_client()
        from prism_mcp.workflow.workflow import GenerateComponentWorkflow

        handle = client.get_workflow_handle(workflow_id)  # type: ignore[attr-defined]
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(
                jsx_code=jsx_code,
                companion_test_code=companion_test_code,
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
    async def get_component_status(workflow_id: str) -> dict[str, Any]:
        """Poll a workflow's current status (read-only query).

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
