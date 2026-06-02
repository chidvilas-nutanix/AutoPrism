"""Slice 12 reflection scaffold — the AlphaCodium pre-process step.

This module builds a :class:`ReflectionContext` from a free-form
spec and a component name, fanning out to the slice-9..11 indices
that the rest of the MCP server already maintains:

* **Hybrid searcher** (slice 9) → top-k example JSX snippets.
* **Composition graph** (slice 10) → top-k related components +
  candidate decompositions.
* **Color token index** (slice 11) → token names matching the hex
  literals in the spec.
* **A11y rules** (slice 11) → per-component a11y guidance blocks.

The result is the structured context the Cursor agent loop reads
*before* generating JSX — the LLM's "self-reflection" stage from
AlphaCodium gets concrete inputs instead of having to invent
context out of thin air.

We accept the four sub-indices as explicit kwargs (not a whole
:class:`Library`) for two reasons:

1. **Testability.** This module can be exercised without standing
   up a real Library + tarball + encoder.
2. **Determinism.** Each sub-index is already side-effect-free at
   the query boundary; co-mingling them with a fat Library object
   would force this module to inherit Library's lifecycle complexity.

The thin facade is built by the MCP tool layer (Step 8) where the
Library object lives anyway.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from prism_mcp.a11y import A11yRules
from prism_mcp.embeddings import ExampleHit
from prism_mcp.graph import CompositionGraph
from prism_mcp.tokens_index import ColorTokenIndex
from prism_mcp.workflow.contracts import ReflectionContext

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Hex-literal extraction. Spec text comes from Figma layer names /
# free-form prose; both can mention design colours.
# --------------------------------------------------------------------------


_HEX_RE = re.compile(r"#(?P<digits>[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
"""Conservative hex regex: 3 or 6 hex digits, word-boundary anchored.

We require the ``\\b`` because ``#header-1`` and other identifier-
style markdown anchors would otherwise pollute the matches with
false positives like ``#header`` or ``#fff`` (which IS a hex).
The ``\\b`` rules out the anchor case (``#header-1`` doesn't end
at a word boundary after ``#header``).
"""


def extract_hex_literals(spec_text: str) -> list[str]:
    """Return de-duplicated hex literals from ``spec_text``.

    Three-digit shorthand (``#FFF``) is expanded to six digits
    (``#FFFFFF``) so the token index can compare uniformly.

    Args:
        spec_text (str): the free-form spec (Figma layer dump,
            ticket text, etc.).

    Returns:
        list[str]: 6-character ``#XXXXXX`` hex codes, uppercased,
        in first-seen order, deduplicated.
    """
    seen: dict[str, None] = {}
    for match in _HEX_RE.finditer(spec_text):
        digits = match.group("digits")
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        normalised = f"#{digits.upper()}"
        if normalised not in seen:
            seen[normalised] = None
    return list(seen.keys())


# --------------------------------------------------------------------------
# Searcher protocol — lets the test suite plug in a stub without
# importing the real fastembed model.
# --------------------------------------------------------------------------


class HybridSearcherLike(Protocol):
    """Structural protocol matching :class:`HybridSearcher.search`.

    We don't import the concrete class because the MCP tool layer
    is the only place that pays the encoder-init cost. Tests pass
    a stub.
    """

    def search(self, **kwargs: object) -> list[ExampleHit]: ...


# --------------------------------------------------------------------------
# build_reflection_context — the public API.
# --------------------------------------------------------------------------


def build_reflection_context(
    *,
    component_name: str,
    spec_text: str,
    hybrid_searcher: HybridSearcherLike,
    composition_graph: CompositionGraph,
    color_token_index: ColorTokenIndex,
    a11y_rules: A11yRules,
    hex_colors: list[str] | None = None,
    top_k_examples: int = 3,
    top_k_related: int = 5,
) -> ReflectionContext:
    """Build the AlphaCodium-flavored reflection bundle for ``component_name``.

    Args:
        component_name (str): the spec's target component (case-
            sensitive; matches Prism's identifier convention).
        spec_text (str): the free-form spec body. Used as the
            hybrid-searcher query and (when ``hex_colors`` is
            ``None``) as the source of hex literals for token
            hinting.
        hybrid_searcher (HybridSearcherLike): the slice-9 searcher.
        composition_graph (CompositionGraph): the slice-10 graph.
        color_token_index (ColorTokenIndex): the slice-11 token index.
        a11y_rules (A11yRules): the slice-11 a11y aggregator.
        hex_colors (list[str] | None): explicit hex list; when
            supplied this overrides ``spec_text`` hex extraction.
        top_k_examples (int): cap on retrieved JSX snippets.
        top_k_related (int): cap on graph-neighbour components.

    Returns:
        ReflectionContext: a fully populated bundle. Empty
        sub-lists indicate that the corresponding index had no
        relevant data — never an error.
    """
    examples = _gather_examples(
        searcher=hybrid_searcher,
        spec_text=spec_text,
        top_k=top_k_examples,
    )
    related = _gather_related(
        graph=composition_graph,
        component_name=component_name,
        top_k=top_k_related,
    )
    token_hints = _gather_token_hints(
        index=color_token_index,
        spec_text=spec_text,
        hex_colors=hex_colors,
    )
    a11y_blocks = _gather_a11y_blocks(
        rules=a11y_rules,
        component_name=component_name,
    )
    candidate_decompositions = _enumerate_candidates(
        component_name=component_name,
        related=related,
    )
    logger.info(
        "built reflection context name=%s examples=%d related=%d tokens=%d a11y=%d",
        component_name,
        len(examples),
        len(related),
        len(token_hints),
        len(a11y_blocks),
    )
    return ReflectionContext(
        component_name=component_name,
        examples=examples,
        related=related,
        token_hints=token_hints,
        a11y_blocks=a11y_blocks,
        candidate_decompositions=candidate_decompositions,
    )


# --------------------------------------------------------------------------
# Sub-gatherers — kept small so each is independently understandable.
# --------------------------------------------------------------------------


def _gather_examples(
    *,
    searcher: HybridSearcherLike,
    spec_text: str,
    top_k: int,
) -> list[str]:
    """Pull the top-k JSX hits from the hybrid searcher."""
    if not spec_text.strip():
        return []
    hits = searcher.search(query=spec_text, top_k=top_k)
    return [hit.code for hit in hits]


def _gather_related(
    *,
    graph: CompositionGraph,
    component_name: str,
    top_k: int,
) -> list[str]:
    """Pull graph neighbours, swallowing unknown-component errors.

    The scaffold's contract: never raise on unknown names. The
    LLM should hear an empty list and adjust, not crash the
    workflow.
    """
    if not graph.has_node(component_name):
        return []
    return [n.name for n in graph.related(component_name, top_k=top_k)]


def _gather_token_hints(
    *,
    index: ColorTokenIndex,
    spec_text: str,
    hex_colors: list[str] | None,
) -> list[str]:
    """Map every spec-hex to its closest token name.

    Each input hex yields zero or one token (the top match). We
    deduplicate across hexes so a spec that mentions the same
    colour twice doesn't surface the same token twice.
    """
    hexes = (
        hex_colors
        if hex_colors is not None
        else extract_hex_literals(spec_text)
    )
    seen: dict[str, None] = {}
    for hex_value in hexes:
        matches = index.query(target_hex=hex_value, top_k=1)
        if not matches:
            continue
        name = matches[0].name
        if name not in seen:
            seen[name] = None
    return list(seen.keys())


def _gather_a11y_blocks(
    *,
    rules: A11yRules,
    component_name: str,
) -> list[str]:
    """Return the a11y-block bodies for ``component_name``, if any.

    Uses :meth:`A11yRules.find_by_component` (O(1) cached dict
    lookup); shares the cache with the Figma walker's per-region
    lookups since both paths run against the same
    :class:`A11yRules` instance held on :class:`Library`.
    """
    component = rules.find_by_component(component_name)
    if component is None:
        return []
    return list(component.blocks)


def _enumerate_candidates(
    *,
    component_name: str,
    related: list[str],
) -> list[str]:
    """Produce 2 candidate decompositions per the slice-12 trim.

    Each candidate is a short ``"<anchor> + <collaborator>"``
    string. Returns an empty list when there are no related
    neighbours — the LLM should hear "I couldn't enumerate
    candidates" and fall back to single-component generation.

    Args:
        component_name (str): the anchor component.
        related (list[str]): graph neighbours, descending by edge
            weight.

    Returns:
        list[str]: up to 2 candidates. ``[]`` when ``related`` is
        empty.
    """
    candidates: list[str] = []
    for collaborator in related[:2]:
        candidates.append(f"{component_name} + {collaborator}")
    return candidates
