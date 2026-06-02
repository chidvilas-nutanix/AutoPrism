"""BM25 search over indexed Prism entities.

PRD section 5 defines the synthetic doc per entity as
``name + type + category + summary + section-headings-of-examples-md``.
PRD section 6 picks ``rank-bm25`` (pure Python, no native deps) at the
~110-entity scale — embeddings buy nothing useful here and add infra.

Tokenization is custom rather than the rank-bm25 default ``str.split``
because identifier-heavy corpora need:

* camelCase splitting so ``useFocusTrap`` matches a query for ``focus``
  or ``trap``;
* lowercase folding so ``Modal`` matches ``modal``;
* preservation of the original token alongside its splits so an
  identifier match still beats a pure component match (BM25 IDF gives
  rare tokens more weight, which is what we want for unique
  identifiers).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from rank_bm25 import BM25Okapi

from prism_mcp.entities import Entity, EntityType, entity_key

logger = logging.getLogger(__name__)

# Splits identifiers like ``useFocusTrap`` into ``use``, ``Focus``,
# ``Trap``. We do *not* try to be clever about acronyms (``HTTPClient``
# stays as ``HTTPClient`` plus ``Client``); good enough at this scale.
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+")


class SearchResult(dict):
    """Convenience subclass so test assertions read naturally."""


class Searcher:
    """BM25-backed searcher over a collection of entities.

    Args:
        entities (Iterable[Entity]): entities to index.
    """

    def __init__(self, entities: Iterable[Entity]) -> None:
        self._entities: list[Entity] = list(entities)
        self._doc_tokens: list[list[str]] = [
            _entity_tokens(e) for e in self._entities
        ]
        # ``rank-bm25`` requires at least one document; empty corpora
        # are valid for us (cold index, type filter that matches
        # nothing) so we shortcut.
        self._bm25: BM25Okapi | None = (
            BM25Okapi(self._doc_tokens) if self._doc_tokens else None
        )

    def __len__(self) -> int:
        return len(self._entities)

    def search(
        self,
        query: str,
        top_k: int = 5,
        type: EntityType | None = None,
    ) -> list[dict]:
        """Return up to ``top_k`` ranked matches for ``query``.

        Args:
            query (str): free-text query from the LLM.
            top_k (int): maximum results to return; must be ``>= 1``.
            type (EntityType | None): when set, only return matches of
                that entity type.

        Returns:
            list[dict]: each row has ``name``, ``type``, ``score``,
            ``summary``, ``import_path``, and ``why_matched`` (the
            subset of query tokens that hit the entity's doc).
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if self._bm25 is None or not query.strip():
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        # Pair (score, idx) and sort high-to-low, stable on idx for
        # determinism.
        ranked = sorted(enumerate(scores), key=lambda p: (-p[1], p[0]))
        query_set = set(query_tokens)

        results: list[dict] = []
        for idx, score in ranked:
            entity = self._entities[idx]
            if type is not None and entity.type != type:
                continue
            # BM25 IDF can pin at zero on tiny corpora (e.g. when a
            # term appears in exactly half the docs). Token overlap is
            # the actual signal of relevance, so we gate on that and
            # use BM25 only for ordering within the matched subset.
            matched = sorted(query_set & set(self._doc_tokens[idx]))
            if not matched:
                continue
            results.append(
                {
                    "name": entity.name,
                    "type": entity.type,
                    "score": float(score),
                    "summary": entity.summary,
                    "import_path": entity.import_path,
                    "why_matched": matched,
                    "key": list(entity_key(entity)),
                }
            )
            if len(results) >= top_k:
                break

        return results


def _entity_tokens(entity: Entity) -> list[str]:
    """Tokenize the synthetic doc for ``entity``.

    Per PRD section 5 the doc is
    ``name + type + category + summary + section-headings``.
    We additionally append a short list of *Figma-vocabulary
    synonyms* (see :data:`_FIGMA_SYNONYM_TOKENS`) so layer names
    the design team uses — like ``Navigation/Header`` or
    ``Status/Tag`` — find their natural Prism component even
    when the entity's own name and summary don't carry those
    tokens.

    Synonyms are append-only and gated on ``(type, name)``, so
    they never pollute the corpus IDF of an unrelated entity.

    Args:
        entity (Entity): entity to tokenize.

    Returns:
        list[str]: lowercased tokens including camelCase splits.
    """
    parts = [
        entity.name,
        entity.type,
        entity.category or "",
        entity.summary,
        " ".join(ex.title for ex in entity.examples),
        _figma_synonyms_for_entity(entity),
    ]
    text = " ".join(parts)
    return _tokenize(text)


# Figma-vocabulary synonyms appended to each entity's synthetic
# doc so BM25 can connect Figma-named layers to the right Prism
# component.
#
# Each row maps ``(entity_type, entity_name)`` to a whitespace-
# joined token list. The tokens are added verbatim to the
# tokenizer input — the same camelCase / suffix-stripping
# pipeline that runs on the rest of the doc applies, so a synonym
# like ``"navbar"`` will also match a query containing
# ``"nav"`` once both have been tokenized.
#
# Curation criteria (deliberately conservative):
#
# * Only Figma layer-name vocabulary that Nutanix designers
#   actually ship — sourced from the b213fac1 / 753:27069 trace
#   and the X-Ray Master Files. No speculative synonyms.
# * Only ``component`` entries — token synonyms would pollute
#   token search; hook/util synonyms aren't a real failure mode
#   we've observed.
# * Tokens that already appear in the entity's own name or
#   summary are still listed for clarity but contribute nothing
#   extra at tokenization time.
#
# Growing the table is safe: the search corpus stays small (~110
# entities) and BM25 IDF naturally down-weights tokens that
# appear across many entities.
_FIGMA_SYNONYM_TOKENS: dict[tuple[str, str], str] = {
    ("component", "HeaderFooterLayout"): (
        "navigation header navbar nav topbar appbar"
    ),
    ("component", "NavBarLayout"): (
        "navigation header navbar nav topbar appbar subheader"
    ),
    ("component", "Breadcrumb"): "navigation breadcrumb crumb trail",
    ("component", "Tabs"): (
        "tab tabs tabstrip tabbar segmented navigation subheader"
    ),
    ("component", "Badge"): "status tag pill chip label indicator",
    ("component", "Alert"): (
        "status alert banner callout warning info notification"
    ),
    ("component", "Modal"): "modal dialog popover overlay sheet",
    ("component", "FullPageModal"): (
        "fullpage modal dialog overlay sheet"
    ),
    ("component", "Pagination"): "pager paginator pageselector",
    ("component", "Title"): "heading title pagetitle pageheader h1 h2 h3",
    ("component", "Link"): "action link anchor hyperlink href",
    ("component", "Button"): "action button cta primaryaction",
    ("component", "Steps"): "navigation steps stepper wizard progress",
    ("component", "TableHeader"): "table header headerrow tableheaderrow",
    ("component", "TableRow"): "table row tablerow",
}
"""Figma-vocabulary synonym tokens by entity ``(type, name)``.

The values are tokenizer-input strings (whitespace-joined,
lowercase); they pass through :func:`_tokenize` exactly like
the rest of the synthetic doc.
"""


def _figma_synonyms_for_entity(entity: Entity) -> str:
    """Return the synonym tokens for ``entity`` or an empty
    string.

    Lookup is keyed on ``(entity.type, entity.name)`` against
    :data:`_FIGMA_SYNONYM_TOKENS`. Unknown entities contribute
    nothing — backward-compatible by construction.
    """
    return _FIGMA_SYNONYM_TOKENS.get((entity.type, entity.name), "")


def _tokenize(text: str) -> list[str]:
    """Lowercase + word-split + camelCase split for BM25 input.

    We additionally emit a *suffix-stripped* variant for each token
    (drops ``-ing``, ``-ed``, ``-s``). This is a poor man's stemmer:
    at the ~110-entity scale a real PorterStemmer pulls in a dep for
    near-zero benefit, but unifying ``trapping`` / ``trap`` matters in
    practice (the PRD's own Slice 5 demo asks "hook for **trapping**
    focus" against an entity named ``useFocusTrap``).

    Args:
        text (str): free text to tokenize.

    Returns:
        list[str]: tokens. Duplicates are preserved — rank-bm25 treats
        term frequency naturally.
    """
    tokens: list[str] = []
    for raw_word in re.findall(r"[A-Za-z0-9_'-]+", text):
        lowered = raw_word.lower()
        tokens.append(lowered)
        stripped = _strip_english_suffix(lowered)
        if stripped != lowered:
            tokens.append(stripped)
        # Emit camelCase splits as additional tokens so queries on the
        # split form still find the identifier.
        camel_parts = _CAMEL_RE.findall(raw_word)
        if len(camel_parts) > 1:
            for part in camel_parts:
                part_lower = part.lower()
                tokens.append(part_lower)
                part_stripped = _strip_english_suffix(part_lower)
                if part_stripped != part_lower:
                    tokens.append(part_stripped)
    return tokens


_VOWELS = frozenset("aeiou")


def _strip_english_suffix(token: str) -> str:
    """Strip ``-ing``, ``-ed``, ``-s`` so ``trapping`` matches ``trap``.

    The rules are intentionally tight to avoid false stems:

    * ``-ing`` and ``-ed`` are stripped then the stem is *un-doubled*
      when its last two characters are matching consonants (``trapp``
      → ``trap``, ``runn`` → ``run``).
    * ``-s`` is only stripped when the preceding character is a
      consonant. That keeps ``focus`` / ``kudos`` / ``bonus`` intact
      (vowel + s) while still letting ``props`` → ``prop`` and
      ``tokens`` → ``token``.
    * Double-s (``class``) is never touched.

    Args:
        token (str): lowercased token.

    Returns:
        str: possibly-shortened token; identical to ``token`` when no
        rule applies.
    """
    if len(token) > 4 and token.endswith("ing"):
        return _undouble(token[:-3])
    if len(token) > 3 and token.endswith("ed"):
        return _undouble(token[:-2])
    if (
        len(token) > 3
        and token.endswith("s")
        and not token.endswith("ss")
        and token[-2] not in _VOWELS
    ):
        return token[:-1]
    return token


def _undouble(stem: str) -> str:
    """Collapse a doubled trailing consonant (``trapp`` → ``trap``).

    Args:
        stem (str): potentially-doubled stem.

    Returns:
        str: stem with one trailing consonant removed when the last
        two characters are matching consonants.
    """
    if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
        return stem[:-1]
    return stem
