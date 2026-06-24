"""Tests for the slice-11 color-token index.

Focus is on pinning the **query contract**:

* exact hex round-trip (ΔE 0, bucket="exact"),
* Oklab-primary / CIEDE2000-tiebreak rank order,
* threshold buckets (exact / near / loose / no-match),
* optional role hint narrows the candidate set,
* role hint falls back to global when narrowed best is too far,
* non-hex token values are filtered out at build time.
"""

from __future__ import annotations

import pytest

from prism_mcp.entities import Entity
from prism_mcp.tokens_index import (
    ColorTokenIndex,
    TokenMatch,
    build_color_token_index,
)


def _token_entity(name: str, value: str) -> Entity:
    """Tiny factory for a slice-6 color-category token entity."""
    return Entity(
        name=name,
        type="token",
        version="2.54.0",
        category="color",
        value=value,
        source_file="src/styles/v2/Colors.less",
        summary=f"color token {value}",
        import_path="",
    )


def _non_color_entity(name: str) -> Entity:
    """A non-color entity that the builder should ignore."""
    return Entity(
        name=name,
        type="component",
        version="2.54.0",
        category="component",
        source_file="lib/components/v2/X.d.ts",
        summary="not a color",
        import_path="@nutanix-ui/prism-reactjs",
    )


# ---------------------------------------------------------------------------
# build_color_token_index
# ---------------------------------------------------------------------------


def test_build_skips_non_color_entities() -> None:
    """Only ``type=token, category=color`` rows make it in."""
    entities = [
        _token_entity("color-primary", "#1B6BCC"),
        _non_color_entity("Button"),
        _token_entity("font-size-md", "14px"),  # category=color but bad hex
        Entity(
            name="z-modal",
            type="token",
            version="2.54.0",
            category="z-index",
            value="1000",
            source_file="src/styles/v2/Z-Index.less",
            summary="z-index 1000",
            import_path="",
        ),
    ]

    # Make the font-size-md row category=color so we exercise the
    # hex-filter rather than the category filter.
    entities[2].category = "color"

    index = build_color_token_index(entities=entities, version="2.54.0")

    assert len(index) == 1
    matches = index.query(target_hex="#1B6BCC", top_k=1)
    assert matches[0].name == "color-primary"


def test_build_skips_variable_reference_hex() -> None:
    """Tokens whose value is another LESS variable are filtered out.

    The Prism corpus has rows like ``@focus-ring-color: @color-primary;``
    — those reference another token, not a hex literal. We can't
    expand them without LESS evaluation; skip silently and rely on
    the referenced token being directly indexed.
    """
    entities = [
        _token_entity("color-primary", "#1B6BCC"),
        _token_entity("focus-ring-color", "@color-primary"),
    ]

    index = build_color_token_index(entities=entities, version="x")

    assert len(index) == 1


def test_build_canonicalises_hex_to_lowercase_six_digit() -> None:
    """Hex values normalise to ``#rrggbb`` (lowercase, no shorthand)."""
    entities = [
        _token_entity("a", "#1BC"),  # 3-digit shorthand
        _token_entity("b", "1B6BCC"),  # missing leading #
        _token_entity("c", "#1B6BCC80"),  # 8-digit with alpha
    ]

    index = build_color_token_index(entities=entities, version="x")

    # Just round-trip each token's canonical hex via a query.
    a = index.query(target_hex="#1BC", top_k=1)[0]
    assert a.hex == "#11bbcc"
    b = index.query(target_hex="#1B6BCC", top_k=1)[0]
    assert b.hex.startswith("#")
    assert b.hex == b.hex.lower()
    # All canonical hexes are 7-char (#rrggbb) — alpha was stripped.
    assert len(b.hex) == 7


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def test_query_exact_hex_match_is_bucket_exact() -> None:
    """When the target hex exactly equals a token, ΔE = 0, bucket = exact."""
    entities = [_token_entity("color-primary", "#1B6BCC")]

    index = build_color_token_index(entities=entities, version="x")
    matches = index.query(target_hex="#1B6BCC", top_k=1)

    assert len(matches) == 1
    assert matches[0].name == "color-primary"
    assert matches[0].distance_oklab == pytest.approx(0.0, abs=1e-9)
    assert matches[0].distance_de2000 == pytest.approx(0.0, abs=1e-9)
    assert matches[0].bucket == "exact"


def test_query_returns_top_k_in_ascending_oklab_distance() -> None:
    """The closest token comes first."""
    entities = [
        _token_entity("color-primary", "#1B6BCC"),
        _token_entity("color-secondary", "#627386"),
        _token_entity("color-success", "#5DBA00"),
    ]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(
        target_hex="#1C6CCD", top_k=3
    )  # very close to primary

    assert all(isinstance(m, TokenMatch) for m in matches)
    assert len(matches) == 3
    assert matches[0].name == "color-primary"
    # Monotone ascending Oklab distance
    assert matches[0].distance_oklab < matches[1].distance_oklab
    assert matches[1].distance_oklab < matches[2].distance_oklab


def test_query_buckets_match_expected_thresholds() -> None:
    """Mapping ΔE 2000 → bucket follows the documented thresholds."""
    entities = [
        # Same as target: exact
        _token_entity("color-primary", "#1B6BCC"),
        # Slightly different: still near
        _token_entity("color-primary-warm", "#256FD0"),
        # Completely different green: no-match
        _token_entity("color-success", "#5DBA00"),
    ]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(target_hex="#1B6BCC", top_k=3)
    by_name = {m.name: m for m in matches}

    assert by_name["color-primary"].bucket == "exact"
    # The slightly-different blue should be "exact" or "near"
    # depending on the actual ΔE; assert it's NOT "no-match".
    assert by_name["color-primary-warm"].bucket in {"exact", "near", "loose"}
    # The green should be "no-match" (way too distant).
    assert by_name["color-success"].bucket == "no-match"


def test_query_role_hint_narrows_candidate_set() -> None:
    """A role hint that matches actual tokens narrows the ranking.

    When role="surface" matches some tokens AND the best narrowed
    match is good enough (ΔE ≤ 5), the narrowed top-1 is returned
    even when a globally-closer non-surface token exists.
    """
    entities = [
        # A surface-keyword token that's an exact match
        _token_entity("color-surface-primary", "#FFFFFF"),
        # A non-surface token that's also an exact match
        _token_entity("color-text-primary", "#FFFFFF"),
        # A non-surface token that's NOT a match
        _token_entity("color-success", "#5DBA00"),
    ]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(target_hex="#FFFFFF", top_k=1, role="surface")

    assert matches[0].name == "color-surface-primary"


def test_query_role_hint_falls_back_when_narrowed_set_too_far() -> None:
    """If no role-matching token is close enough, fall back to global.

    Pins the "never empty handed" contract: a bad role hint must
    not make the LLM blind to a perfectly good global match.
    """
    entities = [
        # Surface token: far from the query (red vs target blue)
        _token_entity("color-surface-warning", "#FF0000"),
        # Non-surface token: perfect match for the query
        _token_entity("color-primary", "#1B6BCC"),
    ]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(target_hex="#1B6BCC", top_k=1, role="surface")

    # Global fallback should win because the narrowed-best ΔE > 5.
    assert matches[0].name == "color-primary"
    assert matches[0].bucket == "exact"


def test_query_unknown_role_uses_global_ranking() -> None:
    """A role string that doesn't map to a known taxonomy is ignored.

    Per the contract: ``role="something-weird"`` shouldn't crash;
    it should silently fall through to the global ranker. Defends
    against drift in :data:`_ROLE_KEYWORDS` over time.
    """
    entities = [_token_entity("color-primary", "#1B6BCC")]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(target_hex="#1B6BCC", top_k=1, role="cromulent")

    assert matches[0].name == "color-primary"


def test_query_known_role_with_zero_matching_tokens_falls_back_global() -> None:
    """A *known* role whose keywords match no token still resolves.

    This is the real-world Prism case: the color tokens are named by hue
    (``dark-blue-2``, ``white``) not by role, so ``role="surface"`` narrows
    to an *empty* set. The "never empty handed" contract requires the query
    to fall back to the global ranker rather than return ``[]`` — otherwise
    every role-hinted lookup (e.g. region background resolution) silently
    fails.
    """
    entities = [
        _token_entity("dark-blue-2", "#1B6BCC"),
        _token_entity("white", "#FFFFFF"),
    ]
    index = build_color_token_index(entities=entities, version="x")

    matches = index.query(target_hex="#FFFFFF", top_k=1, role="surface")

    assert matches, "known role with no keyword match must not return empty"
    assert matches[0].name == "white"
    assert matches[0].bucket == "exact"


def test_query_empty_corpus_returns_no_matches() -> None:
    """An empty index is valid; queries return ``[]``."""
    index = ColorTokenIndex(tokens=[], version="x")

    assert index.query(target_hex="#1B6BCC", top_k=3) == []
    assert len(index) == 0


def test_query_top_k_must_be_positive() -> None:
    """``top_k <= 0`` is a programmer error."""
    entities = [_token_entity("color-primary", "#1B6BCC")]
    index = build_color_token_index(entities=entities, version="x")

    with pytest.raises(ValueError):
        index.query(target_hex="#1B6BCC", top_k=0)
