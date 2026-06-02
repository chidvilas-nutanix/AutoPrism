"""Tests for the BM25 searcher.

A "frozen corpus" of explicit entities lets us assert ranking quality
without depending on parser output drifting.
"""

from __future__ import annotations

import pytest

from prism_mcp.entities import Entity, Example
from prism_mcp.search import Searcher


def _component(
    name: str,
    summary: str = "",
    category: str | None = None,
    example_titles: tuple[str, ...] = (),
) -> Entity:
    """Build a minimal component entity for search tests."""
    return Entity(
        name=name,
        type="component",
        version="1.0.0",
        summary=summary,
        category=category,
        examples=[Example(title=t, code="") for t in example_titles],
    )


def _hook(name: str, summary: str = "") -> Entity:
    """Build a minimal hook entity for search tests."""
    return Entity(name=name, type="hook", version="1.0.0", summary=summary)


def test_returns_empty_for_empty_query() -> None:
    """Empty query => empty results, never garbage."""
    searcher = Searcher([_component("Button", summary="primary button")])

    assert searcher.search("") == []
    assert searcher.search("   ") == []


def test_returns_empty_for_empty_corpus() -> None:
    """Empty corpus => empty results, no exception."""
    searcher = Searcher([])

    assert searcher.search("button") == []


def test_ranks_exact_name_match_first() -> None:
    """A query naming the entity puts that entity first."""
    searcher = Searcher(
        [
            _component("Modal", summary="dialog"),
            _component("Button", summary="primary action"),
            _component("Alert", summary="banner notification"),
        ]
    )

    results = searcher.search("button")

    assert results[0]["name"] == "Button"
    assert results[0]["score"] > 0


def test_summary_tokens_contribute_to_ranking() -> None:
    """The summary text is part of the synthetic doc, per PRD section 5."""
    searcher = Searcher(
        [
            _component("Foo", summary="confirm cancel dialog"),
            _component("Bar", summary="loading spinner"),
        ]
    )

    results = searcher.search("dialog")

    assert results
    assert results[0]["name"] == "Foo"


def test_camel_case_query_matches_identifier() -> None:
    """``focus trap`` matches ``useFocusTrap`` via camelCase splits."""
    searcher = Searcher(
        [
            _hook("useFocusTrap", summary="trap focus inside a region"),
            _hook("useRafThrottle", summary="throttle via raf"),
        ]
    )

    results = searcher.search("focus trap")

    assert results[0]["name"] == "useFocusTrap"
    assert "focus" in results[0]["why_matched"]
    assert "trap" in results[0]["why_matched"]


def test_top_k_limits_results() -> None:
    """``top_k`` truncates the list."""
    corpus = [
        _component(f"Comp{i}", summary="button action") for i in range(10)
    ]
    searcher = Searcher(corpus)

    results = searcher.search("button", top_k=3)

    assert len(results) == 3


def test_type_filter_excludes_other_kinds() -> None:
    """Only entities of the requested type are returned."""
    searcher = Searcher(
        [
            _component("Button", summary="click"),
            _hook("useButtonGroup", summary="manage button group state"),
        ]
    )

    results = searcher.search("button", type="hook")

    assert len(results) == 1
    assert results[0]["type"] == "hook"
    assert results[0]["name"] == "useButtonGroup"


def test_top_k_zero_raises() -> None:
    """Asking for zero results is a contract violation."""
    searcher = Searcher([_component("X")])

    with pytest.raises(ValueError, match="top_k"):
        searcher.search("x", top_k=0)


def test_why_matched_only_lists_query_tokens_that_actually_hit() -> None:
    """``why_matched`` is the intersection, not the full token bag."""
    searcher = Searcher(
        [_component("Modal", summary="confirm dialog with footer")]
    )

    results = searcher.search("dialog xyz")

    matched = set(results[0]["why_matched"])
    assert "dialog" in matched
    assert "xyz" not in matched


def test_results_are_deterministic_for_ties() -> None:
    """Two equally scoring entries come back in stable order."""
    corpus = [
        _component("Alpha", summary="x"),
        _component("Beta", summary="x"),
    ]
    searcher = Searcher(corpus)

    first = searcher.search("x")
    second = searcher.search("x")

    assert [r["name"] for r in first] == [r["name"] for r in second]


def test_example_titles_are_part_of_the_doc() -> None:
    """Example section headings contribute to ranking per PRD."""
    searcher = Searcher(
        [
            _component(
                "Modal",
                summary="dialog",
                example_titles=("Confirm before delete",),
            ),
            _component(
                "Alert",
                summary="banner",
                example_titles=("Inline warning",),
            ),
        ]
    )

    results = searcher.search("delete")

    assert results
    assert results[0]["name"] == "Modal"


def test_figma_synonyms_route_navigation_queries_to_navbar_layout() -> None:
    """``NavBarLayout`` carries Figma-vocabulary synonyms
    (``navigation header navbar nav topbar appbar subheader``)
    so a BM25 query containing those Figma layer-name tokens
    surfaces the right Prism component even when the entity's
    own name/summary doesn't carry them.

    Pre-fix the b213fac1 / 753:27069 trace produced
    ``Navigation/Header`` → ``HeaderFooterLayout`` (single
    token match ``"header"``) at score 0.029, because no
    Prism entity actually contains the literal token
    ``"navigation"`` or ``"navbar"``. The synonym injection
    closes that gap without changing the entity model.
    """
    searcher = Searcher(
        [
            _component("NavBarLayout", summary=""),
            _component("Tooltip", summary="hover hint"),
        ]
    )

    results = searcher.search("navigation navbar nav")

    assert results
    assert results[0]["name"] == "NavBarLayout"
    # The matched tokens should reflect the synonym entries —
    # which proves they were indexed into the synthetic doc.
    matched = set(results[0]["why_matched"])
    assert {"navigation", "navbar", "nav"} & matched


def test_figma_synonyms_route_status_tag_to_badge() -> None:
    """``Badge`` carries the ``status tag pill chip`` synonyms
    so the X-Ray ``Status/Tag`` layer name finds ``Badge`` even
    though Badge's own entity name doesn't contain ``"status"``
    or ``"tag"``.
    """
    searcher = Searcher(
        [
            _component("Badge", summary=""),
            _component("Loader", summary="spinner"),
        ]
    )

    results = searcher.search("status tag")

    assert results
    assert results[0]["name"] == "Badge"


def test_figma_synonyms_only_apply_to_their_named_entity() -> None:
    """An entity NOT in :data:`_FIGMA_SYNONYM_TOKENS` does not get
    the synonym tokens — the table is gated on
    ``(type, name)`` so adding a row for ``NavBarLayout`` does
    NOT accidentally widen the search for ``Tooltip`` or
    ``Modal`` or any other component.
    """
    searcher = Searcher(
        [
            _component("Tooltip", summary="hover hint"),
            _component("Loader", summary="spinner"),
        ]
    )

    # ``navigation`` is a NavBarLayout synonym — neither Tooltip
    # nor Loader is in the synonym table, so neither should match.
    results = searcher.search("navigation")

    assert results == [], (
        f"unexpected match for an entity that isn't in the synonym "
        f"table: {results!r}"
    )
