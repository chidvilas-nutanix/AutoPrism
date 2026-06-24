"""Figma color / typography -> Prism design-token resolution (roadmap P5).

The walker captures raw visual facts — fill hexes, stroke hexes, and TEXT
``style`` (font size + weight). This module maps those literals onto the
Prism **design-token** vocabulary so codegen emits ``@color-primary`` /
``<Title size="h2">`` instead of ``#1B6BCC`` / ``fontSize: 18px`` (the
roadmap's "tokens, not literals" layer — target ≥ 95% of colors/typography
expressed as tokens).

Two resolvers, both pure + deterministic:

* :func:`resolve_color_token` — a trust cascade: the designer's own Figma
  variable (``variable_defs``, exact) → the perceptual
  :class:`~prism_mcp.tokens_index.ColorTokenIndex` (Oklab + CIEDE2000,
  ``exact`` / ``near`` buckets) → unresolved (the hex stands).
* :func:`resolve_typography` — snaps a Figma text ``style`` onto the curated
  Prism type ramp (``Variables.less``).

Why a *curated* type ramp rather than an index over token entities: the
Prism typography tokens (``@title-h1-font-size: 29px`` …) live in
``Variables.less``, which the slice-6 walker files under the ``spacing``
category — so they are not cleanly separable as "typography" entities at
runtime. The ramp here is small, stable, and traces line-for-line to
``Variables.less:13-92``; it is the same pattern P4's ``layout.py`` uses for
the size ladder.

The semantic **style-name** path (Figma published FILL/TEXT styles via the
node ``styles`` map, or ``boundVariables``) is intentionally *not* relied on:
it is absent from hand-built / detached files and the Variables REST API is
``403`` for the project PAT (roadmap §5 risk #2). The perceptual index +
type ramp resolve from the node tree alone, so they work on every file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prism_mcp.figma.models import Typography
from prism_mcp.tokens_index import ColorTokenIndex

# --------------------------------------------------------------------------
# Color resolution.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColorTokenResult:
    """Outcome of resolving one hex to a Prism color token.

    Attributes:
        token (str | None): the Prism token name (no leading ``@``) when the
            hex resolved within the ``exact`` / ``near`` trust band; ``None``
            when nothing close exists (the caller keeps the raw hex).
        bucket (str): perceptual bucket — ``exact`` / ``near`` / ``loose`` /
            ``no-match`` (``exact`` is also used for a ``variable_defs`` hit).
        source (str): provenance — ``"figma_variable"`` (designer's own
            named variable), ``"prism_token_index"`` (perceptual nearest), or
            ``"none"``.
        nearest (str | None): the nearest token name even when it was too far
            to adopt (``bucket`` ``loose`` / ``no-match``) — surfaced as a
            hint so the LLM can decide to inline the hex or add a token.
    """

    token: str | None
    bucket: str
    source: str
    nearest: str | None = None


_NO_COLOR = ColorTokenResult(token=None, bucket="no-match", source="none")


def _variable_lookup(
    hex_value: str, variable_defs: dict[str, str] | None
) -> str | None:
    """Case-insensitive lookup of ``hex_value`` in the designer variable map."""
    if not variable_defs:
        return None
    name = variable_defs.get(hex_value)
    if name:
        return name
    upper = variable_defs.get(hex_value.upper())
    if upper:
        return upper
    return variable_defs.get(hex_value.lower())


def resolve_color_token(
    hex_value: str,
    variable_defs: dict[str, str] | None,
    color_index: ColorTokenIndex | None,
    *,
    role: str | None = None,
) -> ColorTokenResult:
    """Resolve one ``#RRGGBB`` hex to a Prism color token (P5 cascade).

    Args:
        hex_value (str): the source hex (``"#1B6BCC"``; alpha is ignored).
        variable_defs (dict[str, str] | None): the designer's
            ``hex -> token-name`` map from Figma ``get_variable_defs`` — the
            highest-trust signal (the designer literally named this color).
        color_index (ColorTokenIndex | None): the perceptual fallback. When
            ``None`` / empty only the ``variable_defs`` path is available.
        role (str | None): optional semantic-role hint forwarded to the
            index (``"surface"`` / ``"text"`` / …) to bias the candidate set.

    Returns:
        ColorTokenResult: ``token`` is set only when the resolution is in the
        ``exact`` / ``near`` band; otherwise ``token`` is ``None`` and the
        nearest name (if any) rides on ``nearest``.
    """
    designer = _variable_lookup(hex_value, variable_defs)
    if designer:
        return ColorTokenResult(
            token=designer, bucket="exact", source="figma_variable"
        )

    if color_index is None or len(color_index) == 0:
        return _NO_COLOR

    try:
        matches = color_index.query(target_hex=hex_value, top_k=1, role=role)
    except ValueError:
        # Unparseable hex (e.g. a gradient placeholder) — never let a stray
        # color literal abort the walk; keep it as-is.
        return _NO_COLOR
    if not matches:
        return _NO_COLOR
    top = matches[0]
    if top.bucket in ("exact", "near"):
        return ColorTokenResult(
            token=top.name, bucket=top.bucket, source="prism_token_index"
        )
    return ColorTokenResult(
        token=None,
        bucket=top.bucket,
        source="prism_token_index",
        nearest=top.name,
    )


# --------------------------------------------------------------------------
# Typography resolution — the curated Prism type ramp.
# --------------------------------------------------------------------------

# Figma ``style.fontWeight`` (numeric) -> Prism weight token name
# (``Variables.less:13-18``).
_WEIGHT_TO_TOKEN: tuple[tuple[int, str], ...] = (
    (200, "fine"),
    (300, "thin"),
    (400, "regular"),
    (500, "medium"),
    (600, "semi-bold"),
    (700, "bold"),
)


@dataclass(frozen=True)
class _RampEntry:
    """One Prism named text style: ``(size_px, weight, style_token)``."""

    size: int
    weight: int
    style_token: str


# The Prism type ramp, ordered by descending "primacy" so that when two
# styles share a (size, weight) — (14, 400) paragraph vs label; (12, 500)
# title-h4 vs tag — the more common/structural name wins the tie. Sourced
# line-for-line from ``Variables.less:31-92``.
_TYPE_RAMP: tuple[_RampEntry, ...] = (
    _RampEntry(29, 300, "title-h1"),
    _RampEntry(18, 400, "title-h2"),
    _RampEntry(14, 600, "title-h3"),
    _RampEntry(12, 500, "title-h4"),
    _RampEntry(14, 400, "paragraph"),
    _RampEntry(14, 400, "label"),
    _RampEntry(12, 400, "label-small"),
    _RampEntry(14, 500, "link"),
    _RampEntry(12, 500, "tag"),
)

_SIZE_TOLERANCE_PX = 3.0
"""A font size farther than this from every ramp entry is left unresolved
(returns ``None``) rather than snapped to a wrong style — honesty over a
forced, misleading token."""


def _weight_token(weight: int | None) -> str | None:
    """Map a numeric font weight to the nearest Prism weight token name."""
    if weight is None:
        return None
    best = _WEIGHT_TO_TOKEN[0]
    best_dist = abs(_WEIGHT_TO_TOKEN[0][0] - weight)
    for value, name in _WEIGHT_TO_TOKEN[1:]:
        dist = abs(value - weight)
        if dist < best_dist:
            best_dist = dist
            best = (value, name)
    return best[1]


def resolve_typography(style: dict[str, Any] | None) -> Typography | None:
    """Map a Figma text ``style`` onto the Prism type ramp (P5).

    Args:
        style (dict[str, Any] | None): a Figma TEXT node's ``style`` block
            (reads ``fontSize`` + ``fontWeight``).

    Returns:
        Typography | None: the resolved named style + size/weight token
        names, or ``None`` when ``style`` lacks a font size or the size is
        more than :data:`_SIZE_TOLERANCE_PX` from every ramp entry.
    """
    if not isinstance(style, dict):
        return None
    raw_size = style.get("fontSize")
    if not isinstance(raw_size, (int, float)):
        return None
    size = float(raw_size)
    raw_weight = style.get("fontWeight")
    weight = int(raw_weight) if isinstance(raw_weight, (int, float)) else None

    # Nearest size band, then nearest weight inside it, then ramp order.
    best: _RampEntry | None = None
    best_key: tuple[float, float, int] | None = None
    for idx, entry in enumerate(_TYPE_RAMP):
        size_dist = abs(entry.size - size)
        weight_dist = abs(entry.weight - weight) if weight is not None else 0
        key = (size_dist, weight_dist, idx)
        if best_key is None or key < best_key:
            best_key = key
            best = entry

    if best is None or abs(best.size - size) > _SIZE_TOLERANCE_PX:
        return None

    exact = best.size == round(size) and (
        weight is None or best.weight == weight
    )
    return Typography(
        font_size=size,
        font_weight=weight,
        style_token=best.style_token,
        size_token=f"{best.style_token}-font-size",
        weight_token=_weight_token(
            weight if weight is not None else best.weight
        ),
        confidence=1.0 if exact else 0.8,
    )


# --------------------------------------------------------------------------
# Page-level color summary (the walker's ``tokens`` map enrichment).
# --------------------------------------------------------------------------


@dataclass
class ColorCoverage:
    """Tally of how many distinct page hexes resolved to a token (P5 metric).

    Attributes:
        total (int): distinct hexes seen.
        variable (int): resolved via a designer ``variable_defs`` name.
        perceptual (int): resolved via the perceptual index (exact / near).
        unresolved (set[str]): hexes with no close token (kept as literals).
    """

    total: int = 0
    variable: int = 0
    perceptual: int = 0
    unresolved: set[str] = field(default_factory=set)

    @property
    def resolved(self) -> int:
        return self.variable + self.perceptual

    @property
    def coverage_pct(self) -> float:
        return round(100 * self.resolved / self.total, 1) if self.total else 0.0
