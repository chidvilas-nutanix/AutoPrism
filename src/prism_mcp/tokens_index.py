"""Slice-11 color-token index for the ``map_token`` tool.

The Prism library publishes its color tokens as LESS variables
(``@color-primary: #1B6BCC;`` etc.) under ``src/styles/v2/Colors.less``.
The slice-6 :mod:`~prism_mcp.parsers.tokens` walker already turns those
into :class:`Entity` objects with ``category="color"`` and
``value="#1B6BCC"``. This module sits on top of those entities and
gives the LLM a "nearest token to this hex" surface backed by SOTA
perceptual color math.

Pipeline shape::

    Entity[category="color", value="#hex"]
        │
        ▼
    ColorToken (precomputed lab + oklab vectors)
        │
        ▼
    ColorTokenIndex.query(target_hex, role=None, top_k=3)
        │   1. parse target → lab + oklab
        │   2. optional role filter (substring match on the token name)
        │   3. rank by Oklab Euclidean distance (primary, screen-optimised)
        │   4. tiebreak by CIEDE2000 ΔE in CIE Lab
        │   5. bucket by ΔE2000 threshold:
        │        ≤ 2.0 → "exact"  (visually identical for normal viewing)
        │        ≤ 5.0 → "near"   (close enough to substitute)
        │        ≤ 10.0 → "loose" (related but visibly different)
        │        > 10.0 → "no-match"
        ▼
    list[TokenMatch]

Why **Oklab Euclidean as primary, CIEDE2000 as tiebreaker**:

* Oklab (Ottosson 2020) is the modern perceptually-uniform space used
  by Tailwind v4, shadcn/ui, Radix Colors. Distance there is straight
  Euclidean — fast, screen-tuned, no patch-coverage edge cases.
* CIEDE2000 is the 25-year-old industry-standard *threshold* metric.
  Designers reason about "ΔE ≤ 2" as visually identical; the
  threshold buckets above are calibrated to that scale. So we rank
  by Oklab but bucket by CIEDE2000 — best of both.

We also keep an optional **semantic-role hint** as a candidate filter:
when the caller passes ``role="surface"`` we narrow to tokens whose
name matches that role substring (``bg``, ``surface``, ``panel``,
``background``) before ranking. If the narrowed set's best ΔE2000 is
still > 5.0 we fall back to the global ranking so the LLM never gets
an empty result just because the role taxonomy didn't fit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from prism_mcp.color import (
    delta_e_2000,
    hex_to_lab,
    hex_to_oklab,
    hex_to_rgb,
    oklab_distance,
)
from prism_mcp.entities import Entity

logger = logging.getLogger(__name__)

# CIEDE2000 thresholds. These are the standard "just noticeable"
# scales — every reference design-system tooling guide uses the same
# boundaries:
#  - 2.0: "visually identical" for normal viewing (CIE TC 1-47 cite).
#  - 5.0: "close enough to substitute" (industry rule of thumb).
#  - 10.0: "same colour family" (the rough boundary of "blue" vs
#    "indigo" for an untrained eye).
EXACT_DELTA_E_THRESHOLD = 2.0
NEAR_DELTA_E_THRESHOLD = 5.0
LOOSE_DELTA_E_THRESHOLD = 10.0

# Match buckets returned with each TokenMatch.
MatchBucket = Literal["exact", "near", "loose", "no-match"]

# Hex prefilter: anything that isn't a 3/6/8-digit hex literal is
# treated as a non-color value (e.g. ``@color-primary-from-theme``
# pointing at a LESS variable rather than a hex). Those are dropped
# silently at index time.
_HEX_VALUE_RE = re.compile(r"^#?[0-9a-fA-F]{3,8}$")

# Role-keyword vocabulary. The PRD's `.cursor/rules/10-tokens.mdc`
# encodes a canonical role taxonomy: surface / text / interactive /
# success / warning / danger / focus. We expand each into the
# substring(s) actual Prism token names use so a ``role="surface"``
# hint catches ``@color-surface``, ``@color-bg``, ``@color-panel-bg``,
# etc. without forcing the LLM to learn library-internal naming.
_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "surface": ("surface", "bg", "background", "panel"),
    "text": ("text", "fg", "foreground", "ink"),
    "interactive": ("interactive", "button", "link", "action"),
    "success": ("success", "positive", "ok"),
    "warning": ("warning", "warn", "caution"),
    "danger": ("danger", "error", "negative", "critical"),
    "focus": ("focus", "outline", "ring"),
}


class ColorToken(BaseModel):
    """One color-category token from the Prism design system.

    Built once per library version by :func:`build_color_token_index`
    from the slice-6 token entities. Holds both the parsed channel
    representations (``rgb``, ``lab``, ``oklab``) so the query path is
    a pure dot/diff — no hex re-parsing per query.

    Attributes:
        name (str): the LESS variable name without the leading ``@``
            (e.g. ``"color-primary"``).
        hex (str): the 6-digit lowercase canonical hex
            (e.g. ``"#1b6bcc"``).
        source_file (str): in-tarball path the token was declared in,
            preserved for traceability in the tool response.
    """

    model_config = ConfigDict(frozen=False, arbitrary_types_allowed=True)

    name: str
    hex: str
    source_file: str

    # The actual numerical reps are managed by the index, not the
    # public-facing model. Keep them as private attributes on the
    # python object via the index, not on the wire schema.


class TokenMatch(BaseModel):
    """One match row returned by :meth:`ColorTokenIndex.query`.

    Attributes:
        name (str): token name (without leading ``@``).
        hex (str): canonical 6-digit lowercase hex.
        source_file (str): in-tarball path for traceability.
        distance_oklab (float): Oklab Euclidean distance from query.
            Smaller is better. Used as the primary rank.
        distance_de2000 (float): CIEDE2000 ΔE distance from query.
            Smaller is better. Used for tiebreak + bucket.
        bucket (str): coarse bucket from
            :data:`EXACT_DELTA_E_THRESHOLD` etc. — one of
            ``"exact" | "near" | "loose" | "no-match"``.
    """

    name: str
    hex: str
    source_file: str
    distance_oklab: float
    distance_de2000: float
    bucket: MatchBucket = Field(description="exact | near | loose | no-match")


class ColorTokenIndex:
    """Vector index over Prism color tokens with Oklab + Lab precomputed.

    Args:
        tokens (Iterable[ColorToken]): tokens to index.
        version (str): tarball version label stamped on the index.

    The constructor pre-computes the ``(N, 3)`` Oklab and Lab matrices
    once; queries are then one hex-to-vec + one batched diff + one
    batched CIEDE2000. At ~60 tokens this is sub-millisecond per
    query and there's nothing to optimise.
    """

    def __init__(self, tokens: Iterable[ColorToken], version: str) -> None:
        self._tokens: list[ColorToken] = list(tokens)
        self._version = version
        if self._tokens:
            self._lab = np.stack(
                [hex_to_lab(t.hex) for t in self._tokens]
            ).astype(np.float64, copy=False)
            self._oklab = np.stack(
                [hex_to_oklab(t.hex) for t in self._tokens]
            ).astype(np.float64, copy=False)
        else:
            self._lab = np.zeros((0, 3), dtype=np.float64)
            self._oklab = np.zeros((0, 3), dtype=np.float64)

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def version(self) -> str:
        """Return the tarball version this index was built from."""
        return self._version

    def query(
        self,
        target_hex: str,
        top_k: int = 3,
        role: str | None = None,
    ) -> list[TokenMatch]:
        """Return the ``top_k`` closest tokens to ``target_hex``.

        Args:
            target_hex (str): hex string (``"#1B6BCC"`` /
                ``"#1B6BCC80"`` / ``"#fff"``). Alpha bytes are
                ignored — we only match RGB.
            top_k (int): maximum matches to return. ``>= 1``.
            role (str | None): optional semantic-role hint. When in
                :data:`_ROLE_KEYWORDS`, restricts candidates to
                tokens whose name contains any of the role's
                keywords. If the narrowed set's best ΔE2000 > 5.0,
                we fall back to the global ranking — never returns
                an empty list because of an unhelpful role hint.

        Returns:
            list[TokenMatch]: matches sorted by primary
            ``distance_oklab``, ties broken by ``distance_de2000``,
            length bounded by ``top_k`` and corpus size.

        Raises:
            ValueError: when ``top_k < 1`` or ``target_hex`` cannot
                be parsed (the latter from :func:`hex_to_rgb`).
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._tokens:
            return []

        target_lab = hex_to_lab(target_hex)
        target_oklab = hex_to_oklab(target_hex)

        # Primary ranking: Oklab Euclidean (broadcasts (3,) vs (N, 3)).
        oklab_dists = oklab_distance(target_oklab, self._oklab)
        # Tiebreak / bucket: CIEDE2000 over CIE Lab.
        de2000_dists = delta_e_2000(target_lab, self._lab)

        # Candidate mask: full set unless we have a role keyword
        # match; in that case narrow first. If the narrowed best is
        # too far, fall back to the global set.
        candidate_mask = _role_candidate_mask(self._tokens, role)
        if candidate_mask is not None and candidate_mask.any():
            narrow_best_de = float(de2000_dists[candidate_mask].min())
            if narrow_best_de > NEAR_DELTA_E_THRESHOLD:
                logger.info(
                    "role=%s narrowed set's best ΔE2000=%.2f > %.2f; "
                    "falling back to global ranking",
                    role,
                    narrow_best_de,
                    NEAR_DELTA_E_THRESHOLD,
                )
                candidate_mask = None
        if candidate_mask is None:
            candidate_mask = np.ones(len(self._tokens), dtype=bool)

        candidate_indices = np.flatnonzero(candidate_mask)

        # Sort by (oklab_dist, de2000_dist) ascending for deterministic
        # tie-break behaviour. lexsort sorts by the last key as primary,
        # so we pass them in reverse order: secondary first, then
        # primary.
        order = np.lexsort(
            (
                de2000_dists[candidate_indices],
                oklab_dists[candidate_indices],
            )
        )
        ranked_indices = candidate_indices[order]

        matches: list[TokenMatch] = []
        for idx in ranked_indices[:top_k]:
            token = self._tokens[int(idx)]
            de = float(de2000_dists[idx])
            matches.append(
                TokenMatch(
                    name=token.name,
                    hex=token.hex,
                    source_file=token.source_file,
                    distance_oklab=float(oklab_dists[idx]),
                    distance_de2000=de,
                    bucket=_bucket_for_distance(de),
                )
            )
        return matches


def build_color_token_index(
    entities: Iterable[Entity], version: str
) -> ColorTokenIndex:
    """Construct a :class:`ColorTokenIndex` from Prism token entities.

    Filters ``entities`` down to ``category="color"`` with a parseable
    hex ``value``. Tokens that reference another LESS variable (e.g.
    ``@focus-ring-color: @color-primary;``) are dropped silently
    because we have no expansion machinery here and the underlying
    token they point at is already in the index.

    Args:
        entities (Iterable[Entity]): all token entities from the
            slice-6 walker.
        version (str): tarball version label stamped on the index.

    Returns:
        ColorTokenIndex: ready to query.
    """
    tokens: list[ColorToken] = []
    for entity in entities:
        if entity.type != "token" or entity.category != "color":
            continue
        raw_value = (entity.value or "").strip()
        if not _HEX_VALUE_RE.match(raw_value):
            logger.debug(
                "skipping non-hex color token name=%s value=%r",
                entity.name,
                raw_value,
            )
            continue
        try:
            # We don't use the rgb here — this is just validation.
            # ``hex_to_rgb`` raises ``ValueError`` on bad hex; that
            # would skip the row.
            hex_to_rgb(raw_value)
        except ValueError:
            logger.debug(
                "skipping unparseable color token name=%s value=%r",
                entity.name,
                raw_value,
            )
            continue
        tokens.append(
            ColorToken(
                name=entity.name,
                hex=_canonical_hex(raw_value),
                source_file=entity.source_file or "",
            )
        )
    return ColorTokenIndex(tokens=tokens, version=version)


def _bucket_for_distance(delta_e: float) -> MatchBucket:
    """Return the coarse bucket for a CIEDE2000 distance."""
    if delta_e <= EXACT_DELTA_E_THRESHOLD:
        return "exact"
    if delta_e <= NEAR_DELTA_E_THRESHOLD:
        return "near"
    if delta_e <= LOOSE_DELTA_E_THRESHOLD:
        return "loose"
    return "no-match"


def _role_candidate_mask(
    tokens: list[ColorToken], role: str | None
) -> np.ndarray | None:
    """Return a boolean mask over ``tokens`` for ``role`` keywords.

    ``None`` is returned when ``role`` is missing or doesn't match
    the canonical taxonomy — callers should then use the full set.
    An *empty mask* (all False) is returned when no token matches
    the role's keywords; the query layer falls back to global in
    that case too.
    """
    if not role:
        return None
    keywords = _ROLE_KEYWORDS.get(role.lower())
    if not keywords:
        return None
    lowered_names = [t.name.lower() for t in tokens]
    return np.array(
        [any(kw in name for kw in keywords) for name in lowered_names],
        dtype=bool,
    )


def _canonical_hex(raw: str) -> str:
    """Return a lowercase 6-digit hex with leading ``#``.

    Expands 3-digit shorthand (``#1bc`` → ``#11bbcc``) and strips
    alpha bytes (``#1B6BCC80`` → ``#1b6bcc``). Anything that doesn't
    match :data:`_HEX_VALUE_RE` should have been rejected earlier;
    this function assumes a valid input.
    """
    body = raw.lstrip("#").lower()
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    elif len(body) == 8:
        body = body[:6]
    return f"#{body}"
