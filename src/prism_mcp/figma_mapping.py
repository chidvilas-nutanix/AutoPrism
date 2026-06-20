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

This module fans out all three in **one call**, with two
notable properties:

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

logger = logging.getLogger(__name__)


# ``extract_hex_literals`` was historically shared with the (now removed)
# reflection scaffold. It is small and self-contained, so it lives here
# now that this module is its only caller.
_HEX_RE = re.compile(r"#(?P<digits>[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def extract_hex_literals(spec_text: str) -> list[str]:
    """Return de-duplicated 6-digit ``#XXXXXX`` hex literals from text.

    Three-digit shorthand (``#FFF``) is expanded to six digits so the
    colour-token index can compare uniformly. Word-boundary anchored so
    markdown anchors like ``#header-1`` do not produce false positives.
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
        primary_recommendation (str | None): deterministic
            primary component pick derived from
            :attr:`MappedRegion.role` via
            :data:`PATTERN_TO_PRIMARY`. ``None`` for regions
            whose role doesn't carry a high-confidence
            recommendation (the catch-all
            ``composed-region`` / ``layout-container`` and
            generic Figma node-type roles). The LLM should
            treat this as a soft override: when present AND
            ``primary_recommendation_confidence >= 0.8``, the
            LLM may bypass the candidates list. When the
            BM25/hybrid top-1 disagrees, the LLM should pick
            the recommendation and surface the candidates as
            alternatives. Audit committed at
            ``scripts/audit_layer_b_agreement.py``.
        primary_recommendation_rationale (str): one-line
            human-readable explanation
            (``"pattern role 'kpi-tile' → Tile"``). Empty when
            no recommendation.
        primary_recommendation_confidence (float): 0–1 score.
            ``1.0`` for the pattern-derived recommendations
            (deterministic), ``0.0`` when no recommendation.
            Reserved range for future ML-derived sources.
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
    primary_recommendation: str | None = None
    primary_recommendation_rationale: str = ""
    primary_recommendation_confidence: float = 0.0


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


_ROLE_SYNONYM_BONUS = 0.15
"""Score boost added when a candidate's normalised name matches a
synonym for the caller's ``region_role``.

The walker forwards :attr:`MappedRegion.role` here so a pattern
that the deterministic detector confidently labelled as e.g.
``"kpi-tile"`` biases the BM25 ranker toward Tile-family
components. The 0.15 value dwarfs a single RRF contribution
(max ~0.0164 per ranker) on purpose — when both rankers agree AND
the role agrees, the candidate should land at the top
deterministically. See ``docs/handoff-spatial-and-ranker.md``
§3.2 for the empirical reasoning."""


ROLE_TO_COMPONENT_SYNONYMS: dict[str, frozenset[str]] = {
    # Pattern roles emitted by :mod:`prism_mcp.figma.patterns`.
    "icon": frozenset(
        {"icon", "iconbutton", "iconwithtext", "actionicon"}
    ),
    "stat-list": frozenset(
        {
            "stat",
            "statlist",
            "statgroup",
            "list",
            "datalist",
            "keyvaluelist",
            "definitionlist",
        }
    ),
    "table-column": frozenset(
        {
            "table",
            "tablecolumn",
            "tablecell",
            "tableheader",
            "datatable",
            "grid",
            "gridcolumn",
        }
    ),
    "tab-strip": frozenset(
        {"tab", "tabs", "tabbar", "tabstrip", "tabgroup", "tabsbar"}
    ),
    "button-group": frozenset(
        {
            "button",
            "buttongroup",
            "actionbar",
            "actiongroup",
            "splitbutton",
        }
    ),
    "kpi-tile": frozenset(
        {"tile", "metric", "metriccard", "stat", "kpi", "dashboardtile"}
    ),
    # Generic Figma roles that the walker emits when no pattern
    # detector fired. The bonus is intentionally restricted to
    # layout-family components — the same regions could legitimately
    # render as Card / Panel / Modal / Section, but those names live
    # in :data:`SHAPE_BUCKET_TO_COMPONENT_SYNONYMS` (the geometric
    # signal) so we avoid double-boosting. ``frame`` / ``instance`` /
    # ``component`` / ``group`` / ``text`` deliberately remain
    # unmapped because their node names span the whole vocabulary
    # and a broad bonus would inject more false positives than it
    # would resolve. See ``docs/x-ray-walker-investigation.md`` §12.
    "layout-container": frozenset(
        {"flexlayout", "stackinglayout", "containerlayout", "gridlayout"}
    ),
    "composed-region": frozenset(
        {"containerlayout", "stackinglayout", "flexlayout"}
    ),
}


_SHAPE_BUCKET_BONUS = 0.05
"""Score boost when a candidate's normalised name matches a
synonym for the caller-supplied ``region_shape_bucket``.

A third of the role-synonym bonus on purpose — shape is a weaker
signal than role (a square 200×200 region could be a tile OR a
small modal OR a thumbnail) so we only nudge the score rather
than dominate it. The bonus stacks with the role bonus when both
agree, which is what we want — a confident pattern + matching
geometry is the strongest possible hint short of an exact name
match. See ``docs/handoff-spatial-and-ranker.md`` §3.3."""


PATTERN_TO_PRIMARY: dict[str, str] = {
    # Pattern roles emitted by :mod:`prism_mcp.figma.patterns` map
    # to a single Prism component the LLM should pick by default.
    # Audited at 100% agreement vs BM25 top-1 across the three
    # real-world fixtures — see
    # ``scripts/audit_layer_b_agreement.py``.
    "icon": "Icon",
    "stat-list": "StatList",
    "table-column": "TableColumn",
    "tab-strip": "TabBar",
    "button-group": "ButtonGroup",
    "kpi-tile": "Tile",
}
"""Deterministic ``MappedRegion.role`` → Prism component name.

This is the source of truth for
:attr:`FigmaNodeMapping.primary_recommendation`. The walker
forwards the region's role and we look it up here; the value (a
PascalCase component name) goes into ``primary_recommendation``
verbatim alongside a confidence of ``1.0`` and a rationale string.

Confidence is hard-coded to ``1.0`` because the role itself was
emitted by a deterministic detector with strong guards. Future
ML-derived recommendations should use a lower confidence value
so the LLM can disambiguate the sources."""


_PRIMARY_RECOMMENDATION_CONFIDENCE = 1.0
"""Confidence stamped on every pattern-derived
``primary_recommendation``.

Equal to 1.0 because the deterministic role detector has its own
safety rails (size caps, page-scale gates, absorb ratio limit) —
when a pattern role fires it's high-trust by construction. Lower
the value (or split it per-role) once an ML-derived source joins
the pipeline."""


SHAPE_BUCKET_TO_COMPONENT_SYNONYMS: dict[str, frozenset[str]] = {
    "icon": frozenset({"icon", "iconbutton"}),
    "banner": frozenset({"banner", "alert", "callout", "notification"}),
    "sidebar": frozenset({"sidebar", "sidenav", "navrail", "drawer"}),
    "page": frozenset({"page", "layout", "appshell"}),
    "modal": frozenset(
        {"modal", "dialog", "drawer", "popover", "sheet"}
    ),
    "tile": frozenset({"tile", "card", "kpicard", "statcard"}),
    "card": frozenset({"card", "listitem", "row"}),
    # ``"block"`` is the catch-all — no synonyms, no bonus.
}
"""Shape-bucket to Prism component-name synonyms.

Mirrors :data:`ROLE_TO_COMPONENT_SYNONYMS` but indexed by
:func:`prism_mcp.figma.utils.shape_bucket` output. The boost
(:data:`_SHAPE_BUCKET_BONUS`, ``+0.05``) is intentionally smaller
than the role bonus because shape alone has more false-positive
risk — the same 200×200 region could be a tile or a thumbnail or
a small modal depending on context the geometry can't capture.
"""
"""Region-role to Prism component-name synonyms (lower-case,
non-alphanumeric stripped).

Used by :func:`_build_candidates` to award a small absolute score
bonus when a candidate's normalised name matches one of the
synonyms for the caller's ``region_role``. The mapping is
deliberately conservative — only the pattern roles that
:mod:`prism_mcp.figma.patterns` emits with high confidence appear
here. Generic Figma node-type roles like ``frame`` / ``instance``
/ ``group`` / ``text`` and the catch-all ``composed-region`` /
``layout-container`` roles are absent on purpose: they don't carry
enough semantic signal to justify a boost without false
positives. Use :class:`frozenset` values so the lookup is O(1)
and the constant is hashable for memoisation if a future caller
wants it.
"""


def _normalise_component_name(name: str) -> str:
    """Lower-case and drop non-alphanumerics for synonym matching.

    ``"Action/Button"`` -> ``"actionbutton"``;
    ``"Stat-Card"`` -> ``"statcard"``. Centralised so the boost
    rule and the synonym table always use the same canonical
    form."""
    return "".join(ch.lower() for ch in name if ch.isalnum())


# --------------------------------------------------------------------------
# Fix E — ``Domain/Type`` Figma naming → Prism ranker query rewriting.
#
# The X-Ray Master Files use ``Action/Link``, ``Badge/Badge``,
# ``Status/Tag``, ``Table/Table Cell``, ``Modal/Fullpage`` and similar
# slash-namespaced names. The BM25 / hybrid retrievers treat the
# literal ``"Action/Link"`` as a single token that almost never appears
# verbatim in any Prism entity description, producing the
# ``0.016 / 0.032 / 0.033`` RRF "no-signal" score floor documented in
# ``docs/x-ray-walker-investigation.md`` §11.6.
#
# Fix E has two pieces, applied in :func:`_rewrite_figma_name_for_query`:
#
# 1. **Splitting**: ``"Action/Link"`` ⇒ ``"action link"`` and
#    ``"Table/Table Cell"`` ⇒ ``"table table cell"``. The original
#    literal is preserved so any description that *does* contain it
#    still matches; the split form is appended as additional tokens.
# 2. **Aliases**: a small table of Figma-name → Prism-name hints for
#    the cases where ``Foo/Bar`` and the Prism entity name are
#    spelled differently. Aliases are appended (not substituted) so
#    the rewriting is additive and never degrades a query that
#    happened to be correct under the literal name.
#
# The alias table is intentionally small and only carries the
# patterns observed in the X-Ray investigation; growing it
# unboundedly would defeat BM25's "tokens not in any doc are
# ignored" property.
# --------------------------------------------------------------------------


_FIGMA_NAME_ALIAS_TABLE: tuple[tuple[str, str], ...] = (
    ("action/link", "Link Action"),
    ("action/button", "Button Action"),
    ("action/icon button", "IconButton Button"),
    ("badge/badge", "Badge"),
    ("status/tag", "Tag Badge Status"),
    ("status/icon", "StatusIcon Status Icon"),
    ("status/alert", "Alert Banner"),
    ("table/table cell", "TableCell Cell"),
    ("table/table title", "TableHeader Header"),
    ("table/table header", "TableHeader Header"),
    ("table/column", "TableColumn Column"),
    ("table/row", "TableRow Row"),
    ("navigation/header", "NavigationHeader Nav Header"),
    ("navigation/subheader", "Subheader Nav Header"),
    ("navigation/sidebar", "Sidebar Navigation"),
    ("navigation/breadcrumb", "Breadcrumb Navigation"),
    ("modal/fullpage", "FullPageModal Modal"),
    ("modal/dialog", "Modal Dialog"),
    ("modal/toast", "Toast Notification Modal"),
    ("input/text", "TextInput Input"),
    ("input/select", "Select Dropdown Input"),
    ("input/checkbox", "Checkbox Input"),
    ("input/radio", "Radio RadioButton Input"),
    ("input/toggle", "Toggle Switch Input"),
    ("form/field", "FormField Field"),
)
"""Ordered Figma-name → Prism-search-hint table for Fix E.

Match semantics: case-insensitive substring containment on the
lower-cased layer name. The first matching row wins; we deliberately
do NOT chain multiple hints because that introduces unbounded
synthetic tokens. The table is ordered most-specific-first so
``action/icon button`` is preferred over ``action/button``.
"""


def _rewrite_figma_name_for_query(name: str) -> str:
    """Expand ``"Action/Link"``-style names with split tokens + alias.

    Returns ``name`` followed by the split tokens (whitespace-joined)
    and any matching alias hint. The original literal is always
    preserved at the start of the result so descriptions that
    happen to contain it still match. See
    ``docs/x-ray-walker-investigation.md`` §11.6 + §12 "Fix E".

    Examples:
        >>> _rewrite_figma_name_for_query("Action/Link")
        'Action/Link action link Link Action'
        >>> _rewrite_figma_name_for_query("Table/Table Cell")
        'Table/Table Cell table table cell TableCell Cell'
        >>> _rewrite_figma_name_for_query("Header")
        'Header'
    """
    stripped = name.strip()
    if not stripped:
        return ""
    parts: list[str] = [stripped]
    if "/" in stripped:
        split_lower = stripped.replace("/", " ").lower()
        parts.append(split_lower)
    lower = stripped.lower()
    for needle, hint in _FIGMA_NAME_ALIAS_TABLE:
        if needle in lower:
            parts.append(hint)
            break
    return " ".join(parts)


def _build_lexical_query(
    *,
    node_name: str,
    node_type: str | None,
    reference_code: str | None,
    text_content: str | None = None,
    children_summary: str | None = None,
    structural_hints: list[str] | None = None,
    parent_chain: list[str] | None = None,
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

    Fix E (``docs/x-ray-walker-investigation.md`` §11.6 + §12) runs
    :func:`_rewrite_figma_name_for_query` over both ``node_name``
    and every entry in ``parent_chain`` so slash-namespaced layer
    names like ``Action/Link`` contribute *individual* tokens
    (``action`` + ``link``) plus the alias hint (``Link Action``)
    instead of the literal ``Action/Link`` that almost never
    matches a Prism description.

    Args (additive, all default ``None`` for backward compatibility
    with v1 callers — see design doc §5.2):

        text_content (str | None): concatenated descendant TEXT
            characters. A tile with ``"Top 5 Shares by Connections"``
            matches ``<Tile>`` much better than the bare layer name.
        children_summary (str | None): one-line description of
            immediate children (``"FRAME Header(1 TEXT)"``). Biases
            search toward composites that contain those collaborators.
        structural_hints (list[str] | None): freeform hints like
            ``"320x309 ~square"`` or ``"3-row vertical stack"``.
        parent_chain (list[str] | None): ancestor names, root-first.
            Only the last two are appended (closer context = less
            noise).
    """
    parts: list[str] = [_rewrite_figma_name_for_query(node_name)]
    if node_type:
        parts.append(node_type)
    if text_content:
        parts.append(text_content)
    if children_summary:
        parts.append(children_summary)
    if structural_hints:
        parts.extend(structural_hints)
    if parent_chain:
        # The two most recent ancestors carry the strongest
        # context; deeper ones add noise.
        parts.extend(
            _rewrite_figma_name_for_query(anc) for anc in parent_chain[-2:]
        )
    if reference_code:
        tags = _REACT_TAG_RE.findall(reference_code)
        parts.extend(tags)
    return " ".join(parts)


def _build_semantic_query(
    *,
    node_name: str,
    reference_code: str | None,
    text_content: str | None = None,
) -> str:
    """Compose the semantic-query for slice-9's hybrid searcher.

    The hybrid searcher's dense encoder is *code-specialised*
    (Jina v2 base-code), so the reference code is the strongest
    input here. We always include ``node_name`` as a prose
    prefix so layer names like ``"Empty State Card"`` still
    contribute.

    Args (additive, design doc §5.2):

        text_content (str | None): concatenated descendant TEXT
            characters. Prism's ``examples.md`` files contain
            prose around their JSX, so prepending the visible
            text gives the dense encoder *both* signals — code if
            available, prose otherwise.
    """
    body_parts = [_rewrite_figma_name_for_query(node_name)]
    if text_content:
        body_parts.append(text_content)
    body = "\n\n".join(body_parts)
    if reference_code:
        return f"{body}\n\n{reference_code}"
    return body


# --------------------------------------------------------------------------
# Public entrypoint.
# --------------------------------------------------------------------------


def map_figma_node(
    *,
    node_name: str,
    node_type: str | None = None,
    reference_code: str | None = None,
    hex_colors: list[str] | None = None,
    text_content: str | None = None,
    children_summary: str | None = None,
    structural_hints: list[str] | None = None,
    parent_chain: list[str] | None = None,
    region_role: str | None = None,
    region_shape_bucket: str | None = None,
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
       ``reference_code`` + the four new enrichment fields).
    2. **Hybrid example search** over the semantic query
       (``node_name`` + ``text_content`` + ``reference_code``).
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
        text_content (str | None): concatenated descendant TEXT
            characters. **Additive in this slice (design doc §5)**;
            populated by the page walker for non-INSTANCE
            regions (where the layer name alone is often
            meaningless like ``"Frame 2540"``).
        children_summary (str | None): one-line description of
            immediate descendants (e.g. ``"FRAME Header(1 TEXT)"``).
            Biases search toward composites containing those
            collaborators. **Additive in this slice**.
        structural_hints (list[str] | None): freeform hints like
            ``"320x309 ~square"`` or ``"3-row vertical stack"``.
            **Additive in this slice**.
        parent_chain (list[str] | None): ancestor names,
            root-first. The last two are appended to the lexical
            query. **Additive in this slice**.
        region_role (str | None): the
            :attr:`MappedRegion.role` emitted by the walker (one
            of the pattern roles like ``"kpi-tile"`` /
            ``"table-column"`` / ``"icon"``). When supplied, every
            candidate whose normalised name matches a synonym in
            :data:`ROLE_TO_COMPONENT_SYNONYMS` receives a
            ``+0.15`` boost on the fused score. ``None`` for
            non-pattern regions (``composed-region`` /
            ``layout-container`` / generic Figma types); the
            boost is skipped and the original RRF ranking
            applies — fully backward-compatible with v1 callers.
        region_shape_bucket (str | None): the
            :attr:`MappedRegion.shape_bucket` produced by
            :func:`prism_mcp.figma.utils.shape_bucket` (one of
            ``"tile"`` / ``"card"`` / ``"banner"`` / ``"icon"`` /
            ``"sidebar"`` / ``"modal"`` / ``"page"`` /
            ``"block"``). When supplied AND non-empty, candidates
            whose normalised name matches a synonym in
            :data:`SHAPE_BUCKET_TO_COMPONENT_SYNONYMS` receive
            a ``+0.05`` boost (stacks with the role bonus when
            both agree). ``None`` or ``""`` skips the boost.
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

    Backward-compat: passing ``None`` for all four enrichment
    fields produces *byte-identical* queries to the v1
    implementation — regression-tested in
    ``tests/test_figma_mapping.py``.
    """
    lexical_query = _build_lexical_query(
        node_name=node_name,
        node_type=node_type,
        reference_code=reference_code,
        text_content=text_content,
        children_summary=children_summary,
        structural_hints=structural_hints,
        parent_chain=parent_chain,
    )
    semantic_query = _build_semantic_query(
        node_name=node_name,
        reference_code=reference_code,
        text_content=text_content,
    )

    candidates, hybrid_rows = _build_candidates(
        index=index,
        hybrid_searcher=hybrid_searcher,
        lexical_query=lexical_query,
        semantic_query=semantic_query,
        top_k=top_k,
        region_role=region_role,
        region_shape_bucket=region_shape_bucket,
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
    # Reuse the hybrid hits we already paid for in
    # ``_build_candidates`` instead of running a second
    # ``hybrid_searcher.search`` for the examples list. ``hybrid_rows``
    # is already sorted by descending fused/rerank score, so the
    # first three codes are the same the legacy second call returned.
    examples = [hit.code for hit in hybrid_rows[:3]]
    decompositions = _enumerate_decompositions(
        component_name=top.name if top else None,
        related=related,
    )

    primary, rationale, confidence = _resolve_primary_recommendation(
        region_role
    )

    # Honour the deterministic pattern recommendation in the
    # headline ``suggested_component_name`` field when it landed
    # at full confidence — the walker's pattern detectors are
    # ground-truth for the 6 ``PATTERN_TO_PRIMARY`` roles, and
    # a row's ``Table/Column`` instance should ship
    # ``TableColumn`` in the headline rather than whatever the
    # RRF fusion happened to surface (often ``Table`` because the
    # BM25 doc for ``Table`` contains the token ``"column"``).
    # See the b213fac1 / 753:27069 trace: every
    # ``"Table/Column ✅"`` row had primary_recommendation=
    # ``TableColumn`` at confidence 1.0 yet ``suggested_component
    # _name`` reported ``Table`` because the field was wired to
    # ``candidates[0].name`` only.
    if (
        primary is not None
        and confidence >= _PRIMARY_RECOMMENDATION_CONFIDENCE
    ):
        suggested = primary
    else:
        suggested = top.name if top else None

    logger.info(
        "mapped figma node name=%s top=%s suggested=%s candidates=%d "
        "related=%d tokens=%d examples=%d primary=%s",
        node_name,
        top.name if top else None,
        suggested,
        len(candidates),
        len(related),
        len(token_mappings),
        len(examples),
        primary,
    )

    return FigmaNodeMapping(
        node_name=node_name,
        suggested_component_name=suggested,
        candidates=candidates,
        related=related,
        a11y_blocks=a11y_blocks,
        token_mappings=token_mappings,
        examples=examples,
        candidate_decompositions=decompositions,
        primary_recommendation=primary,
        primary_recommendation_rationale=rationale,
        primary_recommendation_confidence=confidence,
    )


def _resolve_primary_recommendation(
    region_role: str | None,
) -> tuple[str | None, str, float]:
    """Look up the deterministic primary recommendation for a role.

    Returns ``(name, rationale, confidence)``:

    * ``name`` is the Prism component name from
      :data:`PATTERN_TO_PRIMARY` or ``None`` when ``region_role``
      is ``None`` or not in the table.
    * ``rationale`` is a short human-readable string; empty when
      there's no recommendation.
    * ``confidence`` is :data:`_PRIMARY_RECOMMENDATION_CONFIDENCE`
      (1.0) for pattern-derived picks, ``0.0`` otherwise.
    """
    if region_role is None:
        return None, "", 0.0
    primary = PATTERN_TO_PRIMARY.get(region_role)
    if primary is None:
        return None, "", 0.0
    return (
        primary,
        f"pattern role {region_role!r} → {primary}",
        _PRIMARY_RECOMMENDATION_CONFIDENCE,
    )


# --------------------------------------------------------------------------
# Sub-gatherers (one per output field). Kept narrow so a future
# maintainer can swap any single stage independently.
# --------------------------------------------------------------------------


def _hybrid_breadcrumb(hit: ExampleHit) -> str:
    """Build a one-line ``semantic-example: <title>`` breadcrumb
    that the candidate's ``why_matched`` carries when the only
    signal for the candidate came from the hybrid searcher.

    Returns an empty string when the hit has no usable title,
    so the caller can skip appending instead of cluttering
    ``why_matched`` with bare prefixes.

    Examples:
        >>> _hybrid_breadcrumb(
        ...     ExampleHit(
        ...         component_name="NavBar",
        ...         title="Sticky header with logo",
        ...         code="<NavBar/>",
        ...         imports=["NavBar"],
        ...         score=1.0,
        ...     )
        ... )
        'semantic-example: Sticky header with logo'
        >>> _hybrid_breadcrumb(
        ...     ExampleHit(
        ...         component_name="NavBar",
        ...         title="",
        ...         code="<NavBar/>",
        ...         imports=["NavBar"],
        ...         score=1.0,
        ...     )
        ... )
        ''
    """
    title = (hit.title or "").strip()
    if not title:
        return ""
    return f"semantic-example: {title}"


def _build_candidates(
    *,
    index: Index,
    hybrid_searcher: HybridSearcherLike,
    lexical_query: str,
    semantic_query: str,
    top_k: int,
    region_role: str | None = None,
    region_shape_bucket: str | None = None,
) -> tuple[list[CandidateMatch], list[ExampleHit]]:
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

    When ``region_role`` is provided AND has an entry in
    :data:`ROLE_TO_COMPONENT_SYNONYMS`, every candidate whose
    normalised name appears in that synonym set receives a
    :data:`_ROLE_SYNONYM_BONUS` (``+0.15``) score boost before
    the final top-k slice. ``None`` (the default) reproduces v1's
    pure-RRF ranking exactly — verified by
    ``test_figma_mapping.py::test_region_role_none_matches_v1``.

    Returns:
        tuple[list[CandidateMatch], list[ExampleHit]]: the fused
        ranked candidates AND the raw hybrid example hits that fed
        the fusion. The example hits are returned alongside the
        candidates so the caller can slice them into
        :attr:`FigmaNodeMapping.examples` without paying for a
        second :meth:`HybridSearcher.search` call against the same
        query.
    """
    bm25_rows = index.search(query=lexical_query, top_k=top_k * 2)

    # Pull at least 3 hits so the caller's ``examples`` slice never
    # under-fills when ``top_k`` is small (e.g. ``top_k=1`` → would
    # otherwise return only 2 hits). The default ``top_k=5`` already
    # asks for 10 which comfortably covers the 3 we slice for
    # ``FigmaNodeMapping.examples``.
    hybrid_pool = max(top_k * 2, 3)
    hybrid_rows: list[ExampleHit] = []
    if semantic_query.strip():
        hybrid_rows = hybrid_searcher.search(
            query=semantic_query, top_k=hybrid_pool
        )
    # Each hybrid hit is per-*example*; collapse to per-component
    # by keeping the highest-ranked hit per component_name. We
    # retain the full :class:`ExampleHit` (not just the rank) so
    # the candidate's ``why_matched`` field can carry a
    # human-readable ``semantic-example: <title>`` breadcrumb —
    # without it, hybrid-only candidates ship ``why_matched=[]``
    # and the LLM has no way to triage a semantic match. See the
    # b213fac1 / 753:27069 trace: ``Navigation/Subheader`` →
    # ``NavigationIcon`` was a hybrid-only hit with empty
    # ``why_matched``; the agent discarded the suggestion because
    # it couldn't verify the signal.
    hybrid_first_hit: dict[str, tuple[int, ExampleHit]] = {}
    for rank, hit in enumerate(hybrid_rows):
        comp_name = hit.component_name
        if comp_name not in hybrid_first_hit:
            hybrid_first_hit[comp_name] = (rank, hit)

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
    for comp_name, (rank, hit) in hybrid_first_hit.items():
        fused[comp_name] = fused.get(comp_name, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources.setdefault(comp_name, set()).add("hybrid")
        # Append a ``semantic-example: <title>`` rationale so the
        # LLM knows WHICH example anchored the hybrid hit. This
        # is the only signal hybrid-only candidates carry — BM25
        # ``why_matched`` is per-token-overlap, but hybrid is
        # per-embedding so the closest we can come to an
        # explanation is the example whose embedding won.
        existing = why.setdefault(comp_name, [])
        breadcrumb = _hybrid_breadcrumb(hit)
        if breadcrumb and breadcrumb not in existing:
            existing.append(breadcrumb)
        summaries.setdefault(comp_name, "")
        types.setdefault(comp_name, "component")

    def _source_label(src: set[str]) -> str:
        return "both" if src == {"bm25", "hybrid"} else next(iter(src))

    role_synonyms = (
        ROLE_TO_COMPONENT_SYNONYMS.get(region_role)
        if region_role is not None
        else None
    )
    shape_synonyms = (
        SHAPE_BUCKET_TO_COMPONENT_SYNONYMS.get(region_shape_bucket)
        if region_shape_bucket
        else None
    )
    if role_synonyms or shape_synonyms:
        for name in list(fused.keys()):
            norm = _normalise_component_name(name)
            if role_synonyms and norm in role_synonyms:
                fused[name] += _ROLE_SYNONYM_BONUS
            if shape_synonyms and norm in shape_synonyms:
                fused[name] += _SHAPE_BUCKET_BONUS

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    candidates = [
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
    return candidates, hybrid_rows


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
    """Return per-component a11y guidance for the top candidate.

    Uses :meth:`A11yRules.find_by_component` (O(1) cached dict
    lookup) instead of the original linear ``for`` over
    ``rules.per_component``. The mapper is called once per Figma
    agenda row, so on a ~50-region page this saves ~50 linear scans
    over the ~150-entry component list per page.
    """
    if component_name is None:
        return []
    component = rules.find_by_component(component_name)
    if component is None:
        return []
    return list(component.blocks)


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


def _enumerate_decompositions(
    *,
    component_name: str | None,
    related: list[str],
) -> list[str]:
    """Produce up-to-2 ``"anchor + collaborator"`` candidates.

    Each candidate is a short ``"<anchor> + <collaborator>"`` string
    the agent can use as a starting decomposition for the region.
    """
    if component_name is None:
        return []
    candidates: list[str] = []
    for collaborator in related[:2]:
        candidates.append(f"{component_name} + {collaborator}")
    return candidates
