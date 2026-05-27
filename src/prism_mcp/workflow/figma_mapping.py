"""Figma node → Prism component composite mapper.

The Figma-MCP-and-Cursor pipeline produces *node-level* data (a
frame, an instance, a layer) — name, type, dimensions, hex
literals from variables, and (optionally) a screenshot URL plus
React+Tailwind reference code from ``get_design_context``. The
LLM then has to pick the right Prism component(s) and props to
implement that node.

Three orthogonal signals matter when matching:

1. **Lexical** — the layer name (``"OK / Cancel Modal"``) and
   the reference-code identifiers (``<Modal>``, ``<Button>``)
   are strong hints. We feed them to the slice-4 BM25 entity
   searcher.
2. **Semantic** — the prose intent (``"vertical stack of three
   buttons"``) maps better to the slice-9 hybrid example
   searcher, which is dense + RRF + cross-encoder.
3. **Compositional** — once a top component is picked, the
   slice-10 graph supplies the canonical neighbours and the
   slice-11 a11y rules supply the per-component guidance.

This module fans out all three in **one call**, mirroring the
existing :mod:`prism_mcp.workflow.reflection` pattern but with
two differences:

* The target component name is *output*, not input — the LLM
  doesn't know which Prism component to anchor on yet.
* The input is shaped to match what Figma MCP returns
  (``node_name``, ``node_type``, ``reference_code``, ``hex_colors``)
  so Cursor can forward Figma-MCP outputs without reshaping.

This is a deliberate composite — the LLM *could* call
``search_entities`` + ``search_examples`` + ``map_token`` +
``related_components`` + ``get_a11y_rules`` separately (5
tool calls at 200-500ms each + N context tokens each). The
composite collapses that to one round-trip and ships a single
opinionated bundle. Atomic tools stay available for when the
LLM wants finer-grained control.

Best granularity for the input
------------------------------

The Figma node hierarchy is **root → page → frame →
instance/group → leaf**. The two granularities where this
mapper produces useful output are:

* **FRAME** — a frame typically represents one logical UI
  surface (a modal, a card, a form). ``node_name`` like
  ``"Confirm Delete Modal"`` gives a strong lexical hint;
  the reference code is small enough to be a precise
  semantic query.
* **INSTANCE** — an instance of a published library component
  is even tighter; the instance's parent component name often
  *is* the target Prism component name (after a small token
  match).

Calling at PAGE granularity dilutes the signal (the page has
many sub-components, none of which the top-1 answer can
capture). Calling at LEAF (rectangle / vector) granularity is
also low-signal because leaves rarely have semantic names.
The Cursor agent loop should traverse the Figma tree itself
and call this mapper on each frame/instance encountered.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from prism_mcp.a11y import A11yRules
from prism_mcp.embeddings import ExampleHit
from prism_mcp.graph import CompositionGraph, GraphError
from prism_mcp.indexer import Index
from prism_mcp.tokens_index import ColorTokenIndex
from prism_mcp.workflow.reflection import extract_hex_literals

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Public data shapes.
# --------------------------------------------------------------------------


class CandidateMatch(BaseModel):
    """One candidate Prism component for a Figma node.

    Args:
        name (str): the Prism component identifier (PascalCase).
        type (str): the entity type (``component``, ``hook``,
            ``manager``, ``util``, ``token``). Components are
            the typical match; hooks/utils appear when the
            Figma node looks like behaviour rather than UI
            (rare but worth surfacing).
        score (float): the normalised fused score. Higher is
            better; only ordering is meaningful (the magnitude
            is not directly comparable across queries).
        why_matched (list[str]): query tokens that hit the
            entity's BM25 doc — the LLM's deterministic
            "explain why" anchor.
        summary (str): the entity's one-line description.
        source (str): which retrieval stage surfaced this row.
            One of ``"bm25"`` / ``"hybrid"`` / ``"both"``.
            ``"both"`` is the strongest signal — both the
            lexical and semantic ranker independently agreed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    score: float
    why_matched: list[str] = Field(default_factory=list)
    summary: str = ""
    source: str


class TokenMapping(BaseModel):
    """One Figma hex → closest Prism token mapping.

    Args:
        hex (str): the input hex literal (uppercased,
            ``#XXXXXX`` form).
        token_name (str | None): the Prism token name, or
            ``None`` when nothing matched within the loose
            ΔE2000 bucket. ``None`` is informative: the LLM
            should consider inlining the hex or adding a new
            token rather than silently aliasing to a far-off
            token.
        token_hex (str | None): the actual hex value of the
            matched token, for sanity-checking the substitution.
        bucket (str): the perceptual-distance bucket —
            ``exact`` / ``near`` / ``loose`` / ``no-match``.
            See slice-11's ``map_token`` doc for the threshold
            table.
    """

    model_config = ConfigDict(extra="forbid")

    hex: str
    token_name: str | None
    token_hex: str | None
    bucket: str


class FigmaNodeMapping(BaseModel):
    """Output shape of :func:`map_figma_node`.

    Args:
        node_name (str): echoed input — handy when batching.
        suggested_component_name (str | None): the *top* candidate's
            name (PascalCase). ``None`` only when both lexical
            and semantic searchers returned no matches at all
            — extremely rare; usually indicates Cursor passed
            an empty / nonsensical input.
        candidates (list[CandidateMatch]): up-to-5 ranked Prism
            components ordered by fused signal. Always pick
            from this list — never invent a component name
            that isn't here.
        related (list[str]): canonical co-imported components
            from the slice-10 composition graph anchored on
            the top candidate. Empty when the top candidate
            is rare in the example corpus (e.g. ``Carousel``
            standalone).
        a11y_blocks (list[str]): per-component a11y guidance
            from slice-11 anchored on the top candidate.
            Empty when the component doesn't have an a11y
            section in ``*.examples.md``.
        token_mappings (list[TokenMapping]): one row per input
            hex.
        examples (list[str]): top-3 hybrid example JSX hits
            (raw code bodies). Use as imitation patterns when
            generating the candidate JSX.
        candidate_decompositions (list[str]): up-to-2
            ``"anchor + collaborator"`` strings, mirroring
            slice-12's reflection scaffold. Each is one
            possible decomposition the LLM can explore.
    """

    model_config = ConfigDict(extra="forbid")

    node_name: str
    suggested_component_name: str | None
    candidates: list[CandidateMatch] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    a11y_blocks: list[str] = Field(default_factory=list)
    token_mappings: list[TokenMapping] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    candidate_decompositions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Structural protocols — keep this module testable without standing
# up the real fastembed encoder + ONNX runtime.
# --------------------------------------------------------------------------


class HybridSearcherLike(Protocol):
    """Minimal contract on top of slice-9's :class:`HybridSearcher`."""

    def search(self, **kwargs: object) -> list[ExampleHit]: ...


# --------------------------------------------------------------------------
# Internal: query construction.
# --------------------------------------------------------------------------


_REACT_TAG_RE = re.compile(r"<\s*([A-Z][A-Za-z0-9_]*)")
"""Match opening JSX tags (capital first letter — React components).

We use this to extract identifier hints out of the
``reference_code`` Figma's ``get_design_context`` returns. Those
identifiers are the strongest possible lexical signal — Figma's
own code-gen already picked something close to a component name
out of the design.
"""


_RRF_K = 60
"""Reciprocal-Rank-Fusion damping constant.

Standard RRF aggregation uses ``1 / (k + rank)`` to fuse rankers;
``k=60`` is the value Cormack et al. proposed in the original RRF
paper and is also what the slice-9 hybrid searcher uses
internally. Keeping the constant identical here so a future
maintainer doesn't have to reason about two different ``k``s
when debugging fusion behaviour.
"""


def _build_lexical_query(
    *,
    node_name: str,
    node_type: str | None,
    reference_code: str | None,
) -> str:
    """Compose a BM25-friendly query from the node's inputs.

    The slice-4 BM25 tokenizer (:func:`prism_mcp.search._tokenize`)
    splits camelCase and applies a light stemmer, so we can
    feed it whitespace-separated tokens without further
    preprocessing.

    We deliberately *concatenate* rather than choose: the BM25
    tokenizer ignores tokens that aren't in any entity's doc, so
    extra junk doesn't pollute the score; but missing tokens
    that *would* have matched is a hard regression.
    """
    parts: list[str] = [node_name]
    if node_type:
        parts.append(node_type)
    if reference_code:
        tags = _REACT_TAG_RE.findall(reference_code)
        parts.extend(tags)
    return " ".join(parts)


def _build_semantic_query(
    *,
    node_name: str,
    reference_code: str | None,
) -> str:
    """Compose the semantic-query for slice-9's hybrid searcher.

    The hybrid searcher's dense encoder is *code-specialised*
    (Jina v2 base-code), so the reference code is the strongest
    input here. We always include ``node_name`` as a prose
    prefix so layer names like ``"Empty State Card"`` still
    contribute.
    """
    if reference_code:
        return f"{node_name}\n\n{reference_code}"
    return node_name


# --------------------------------------------------------------------------
# Public entrypoint.
# --------------------------------------------------------------------------


def map_figma_node(
    *,
    node_name: str,
    node_type: str | None = None,
    reference_code: str | None = None,
    hex_colors: list[str] | None = None,
    index: Index,
    hybrid_searcher: HybridSearcherLike,
    composition_graph: CompositionGraph,
    color_token_index: ColorTokenIndex,
    a11y_rules: A11yRules,
    top_k: int = 5,
) -> FigmaNodeMapping:
    """Build the composite Figma-to-Prism mapping bundle.

    Three retrieval stages, fused into a single result:

    1. **BM25 entity search** over the lexical query
       (``node_name`` + ``node_type`` + JSX tags from
       ``reference_code``).
    2. **Hybrid example search** over the semantic query
       (``node_name`` + ``reference_code``). The hybrid
       searcher already fuses BM25-over-chunks with
       768-dim dense + cross-encoder rerank.
    3. **Composition / a11y / tokens** anchored on the top
       candidate, mirroring the slice-12 reflection scaffold.

    All three stages are non-destructive — every output array
    can be empty without raising. The LLM should fall back to
    atomic tools (``search_entities``, ``map_token``, ...) when
    the composite returns empty.

    Args:
        node_name (str): the Figma layer / frame name.
        node_type (str | None): the Figma type (``FRAME``,
            ``INSTANCE``, ``GROUP``, etc.). Optional.
        reference_code (str | None): the React+Tailwind
            reference snippet from ``get_design_context``.
            Optional.
        hex_colors (list[str] | None): hex literals already
            parsed from the design's variables, or ``None`` to
            let this function extract them from
            ``reference_code``.
        index (Index): the slice-3..6 entity index.
        hybrid_searcher (HybridSearcherLike): slice-9 hybrid
            searcher.
        composition_graph (CompositionGraph): slice-10 graph.
        color_token_index (ColorTokenIndex): slice-11 tokens.
        a11y_rules (A11yRules): slice-11 a11y aggregator.
        top_k (int): max candidates to return (default 5).

    Returns:
        FigmaNodeMapping: structured bundle. ``candidates`` is
        ordered best-first; ``suggested_component_name`` is the
        top candidate's name (or ``None`` when nothing matched).
    """
    lexical_query = _build_lexical_query(
        node_name=node_name,
        node_type=node_type,
        reference_code=reference_code,
    )
    semantic_query = _build_semantic_query(
        node_name=node_name,
        reference_code=reference_code,
    )

    candidates = _build_candidates(
        index=index,
        hybrid_searcher=hybrid_searcher,
        lexical_query=lexical_query,
        semantic_query=semantic_query,
        top_k=top_k,
    )
    top = candidates[0] if candidates else None
    related = _gather_related(
        graph=composition_graph,
        component_name=top.name if top else None,
        top_k=top_k,
    )
    a11y_blocks = _gather_a11y_blocks(
        rules=a11y_rules,
        component_name=top.name if top else None,
    )
    token_mappings = _gather_token_mappings(
        index=color_token_index,
        hex_colors=hex_colors,
        reference_code=reference_code,
    )
    examples = _gather_examples(
        searcher=hybrid_searcher,
        semantic_query=semantic_query,
        top_k=3,
    )
    decompositions = _enumerate_decompositions(
        component_name=top.name if top else None,
        related=related,
    )

    logger.info(
        "mapped figma node name=%s top=%s candidates=%d related=%d "
        "tokens=%d examples=%d",
        node_name,
        top.name if top else None,
        len(candidates),
        len(related),
        len(token_mappings),
        len(examples),
    )

    return FigmaNodeMapping(
        node_name=node_name,
        suggested_component_name=top.name if top else None,
        candidates=candidates,
        related=related,
        a11y_blocks=a11y_blocks,
        token_mappings=token_mappings,
        examples=examples,
        candidate_decompositions=decompositions,
    )


# --------------------------------------------------------------------------
# Sub-gatherers (one per output field). Kept narrow so a future
# maintainer can swap any single stage independently.
# --------------------------------------------------------------------------


def _build_candidates(
    *,
    index: Index,
    hybrid_searcher: HybridSearcherLike,
    lexical_query: str,
    semantic_query: str,
    top_k: int,
) -> list[CandidateMatch]:
    """Run BM25 + hybrid in parallel, merge into ranked CandidateMatches.

    Merge rule: collect the union of names across both rankers,
    then score each by ``1 / (k + rank)`` summed over the rankers
    it appeared in (the standard RRF aggregation with ``k=60``).
    Names hit by *both* rankers get a higher fused score than
    names hit by only one — the strongest signal in design-to-
    code matching, since the BM25 catches exact identifier hits
    and the dense catches semantic similarity.

    The ``source`` field on each :class:`CandidateMatch` records
    which ranker(s) found it (``"bm25"`` / ``"hybrid"`` /
    ``"both"``) so the LLM can weight the rows accordingly.
    """
    bm25_rows = index.search(query=lexical_query, top_k=top_k * 2)

    hybrid_rows: list[ExampleHit] = []
    if semantic_query.strip():
        hybrid_rows = hybrid_searcher.search(
            query=semantic_query, top_k=top_k * 2
        )
    # Each hybrid hit is per-*example*; collapse to per-component
    # by keeping the highest-ranked hit per component_name.
    hybrid_first_rank: dict[str, int] = {}
    for rank, hit in enumerate(hybrid_rows):
        comp_name = hit.component_name
        if comp_name not in hybrid_first_rank:
            hybrid_first_rank[comp_name] = rank

    fused: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    why: dict[str, list[str]] = {}
    summaries: dict[str, str] = {}
    types: dict[str, str] = {}
    for rank, row in enumerate(bm25_rows):
        name = row["name"]
        fused[name] = fused.get(name, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources.setdefault(name, set()).add("bm25")
        why.setdefault(name, list(row.get("why_matched", [])))
        summaries.setdefault(name, row.get("summary", ""))
        types.setdefault(name, row.get("type", "component"))
    for comp_name, rank in hybrid_first_rank.items():
        fused[comp_name] = (
            fused.get(comp_name, 0.0) + 1.0 / (_RRF_K + rank + 1)
        )
        sources.setdefault(comp_name, set()).add("hybrid")
        # Hybrid hits don't carry BM25's ``why_matched`` or
        # ``summary``; fall back to whatever BM25 contributed
        # (or leave blank when only hybrid matched).
        summaries.setdefault(comp_name, "")
        types.setdefault(comp_name, "component")

    def _source_label(src: set[str]) -> str:
        return "both" if src == {"bm25", "hybrid"} else next(iter(src))

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    return [
        CandidateMatch(
            name=name,
            type=types.get(name, "component"),
            score=score,
            why_matched=why.get(name, []),
            summary=summaries.get(name, ""),
            source=_source_label(sources[name]),
        )
        for name, score in ranked
    ]


def _gather_related(
    *,
    graph: CompositionGraph,
    component_name: str | None,
    top_k: int,
) -> list[str]:
    """Return graph neighbours of the top candidate, defensively."""
    if component_name is None or not graph.has_node(component_name):
        return []
    try:
        return [n.name for n in graph.related(component_name, top_k=top_k)]
    except GraphError:
        return []


def _gather_a11y_blocks(
    *,
    rules: A11yRules,
    component_name: str | None,
) -> list[str]:
    """Return per-component a11y guidance for the top candidate."""
    if component_name is None:
        return []
    for component in rules.per_component:
        if component.component_name == component_name:
            return list(component.blocks)
    return []


def _gather_token_mappings(
    *,
    index: ColorTokenIndex,
    hex_colors: list[str] | None,
    reference_code: str | None,
) -> list[TokenMapping]:
    """Map every input hex to its closest Prism token.

    If ``hex_colors`` is ``None`` we fall back to extracting
    them from ``reference_code`` (where Figma's auto-generated
    Tailwind classes often contain bracketed arbitrary values
    like ``bg-[#1B6BCC]``).
    """
    hexes = hex_colors
    if hexes is None and reference_code:
        hexes = extract_hex_literals(reference_code)
    if not hexes:
        return []
    mappings: list[TokenMapping] = []
    for hex_value in hexes:
        matches = index.query(target_hex=hex_value, top_k=1)
        if not matches:
            mappings.append(
                TokenMapping(
                    hex=hex_value,
                    token_name=None,
                    token_hex=None,
                    bucket="no-match",
                )
            )
            continue
        top = matches[0]
        mappings.append(
            TokenMapping(
                hex=hex_value,
                token_name=top.name,
                token_hex=top.hex,
                bucket=top.bucket,
            )
        )
    return mappings


def _gather_examples(
    *,
    searcher: HybridSearcherLike,
    semantic_query: str,
    top_k: int,
) -> list[str]:
    """Return top-k JSX bodies for the semantic query."""
    if not semantic_query.strip():
        return []
    hits = searcher.search(query=semantic_query, top_k=top_k)
    return [hit.code for hit in hits]


def _enumerate_decompositions(
    *,
    component_name: str | None,
    related: list[str],
) -> list[str]:
    """Produce up-to-2 ``"anchor + collaborator"`` candidates.

    Mirrors :func:`prism_mcp.workflow.reflection._enumerate_candidates`
    so the agent loop sees the same string shape regardless of
    whether it called ``reflect_on_spec`` or ``map_figma_node``.
    """
    if component_name is None:
        return []
    candidates: list[str] = []
    for collaborator in related[:2]:
        candidates.append(f"{component_name} + {collaborator}")
    return candidates
