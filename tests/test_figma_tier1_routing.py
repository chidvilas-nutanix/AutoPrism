"""P3 Part A: Tier-1 routing — catalog identity -> headline recommendation.

These tests cover :func:`prism_mcp.figma.walker._apply_catalog_routing`
and its helper :func:`_promote_resolution_to_headline`: the pass that
turns the P1 :class:`FigmaComponentIdentity` + the P2
:class:`FigmaCatalog` into a deterministic
:attr:`MappedRegion.prism_resolution` and promotes it into the
``mapping`` headline (unless an audited pattern role already claimed a
finer sub-component).

A hand-built catalog is injected for hermeticity — these tests do not
depend on the committed ``data/figma_catalog.json`` artifact except
where they explicitly exercise the page-fallback cascade (which is
table-driven and covered exhaustively in ``test_figma_catalog.py``).
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma import walk_tree
from prism_mcp.figma.catalog import (
    CatalogEntry,
    FigmaCatalog,
    RegionResolution,
    resolve_prism_component,
)
from prism_mcp.figma.models import (
    FigmaComponentIdentity,
    FigmaTreeMapping,
    MappedRegion,
    leanify_tree_mapping,
)
from prism_mcp.figma.walker import (
    _PATTERN_HEADLINE_CONFIDENCE,
    _promote_resolution_to_headline,
)
from prism_mcp.figma_mapping import (
    _PRIMARY_RECOMMENDATION_CONFIDENCE,
    CandidateMatch,
    FigmaNodeMapping,
)

# --------------------------------------------------------------------------
# Harness.
# --------------------------------------------------------------------------


def _entry(
    key: str,
    prism: str,
    *,
    method: str = "key-override",
    confidence: float = 1.0,
    set_key: str | None = None,
    doc_url: str | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        component_key=key,
        prism_component=prism,
        kind="component",
        method=method,  # type: ignore[arg-type]
        confidence=confidence,
        figma_name="test",
        library_key="LIB",
        library_name="Test Library",
        component_set_key=set_key,
        doc_url=doc_url,
    )


def _catalog(*entries: CatalogEntry) -> FigmaCatalog:
    return FigmaCatalog({e.component_key: e for e in entries}, {})


def _page_with_child(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "1:1",
        "name": "Page",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 300},
        "children": [child],
    }


def _instance(
    *,
    node_id: str = "1:2",
    name: str = "Button",
    component_id: str = "10:1",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": "INSTANCE",
        "componentId": component_id,
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }


def _walk(
    document: dict[str, Any],
    *,
    components: dict[str, Any] | None = None,
    component_sets: dict[str, Any] | None = None,
    catalog: FigmaCatalog | None = None,
    catalog_routing: bool = True,
    map_fn: Any = None,
) -> FigmaTreeMapping:
    return walk_tree(
        tree_json=document,
        components=components,
        component_sets=component_sets,
        map_figma_node_fn=map_fn,
        catalog=catalog,
        catalog_routing=catalog_routing,
    )


def _identity_region(mapping: FigmaTreeMapping) -> MappedRegion:
    """Return the single agenda row carrying a DS identity."""
    rows = [r for r in mapping.agenda if r.figma_component is not None]
    assert len(rows) == 1, f"expected 1 identity row, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------
# Catalog key hit -> source="catalog" + headline promotion.
# --------------------------------------------------------------------------


def test_catalog_key_hit_sets_resolution_and_promotes_headline() -> None:
    components = {"10:1": {"key": "globalkeyA", "name": "Whatever Layer"}}
    catalog = _catalog(_entry("globalkeyA", "Button"))

    mapping = _walk(
        _page_with_child(_instance()),
        components=components,
        catalog=catalog,
    )
    region = _identity_region(mapping)

    res = region.prism_resolution
    assert res is not None
    assert res.source == "catalog"
    assert res.prism_component == "Button"
    assert res.component_key == "globalkeyA"

    # Headline promoted: an exact key beats the fuzzy ranker.
    assert region.mapping.primary_recommendation == "Button"
    assert region.mapping.suggested_component_name == "Button"
    assert region.mapping.primary_recommendation_confidence == 1.0
    assert region.mapping.primary_recommendation_rationale.startswith(
        "Tier-1 catalog:"
    )


def test_summary_reports_catalog_resolved_count() -> None:
    components = {"10:1": {"key": "k", "name": "x"}}
    mapping = _walk(
        _page_with_child(_instance()),
        components=components,
        catalog=_catalog(_entry("k", "Button")),
    )
    assert mapping.summary["catalog_resolved"] == 1


# --------------------------------------------------------------------------
# Page-fallback — key absent from catalog, cascade resolves the name/desc.
# --------------------------------------------------------------------------


def test_page_fallback_when_key_absent_from_catalog() -> None:
    desc = (
        "http://prism-styleguide/v2/index.html#/Components/Actions?id=button"
    )
    components = {
        "10:1": {
            "key": "not-in-catalog",
            "name": "Action/ Button",
            "description": desc,
            "remote": True,
        }
    }
    # Expected target computed from the *real* cascade so the test does
    # not hardcode the curated override table.
    expected = resolve_prism_component("Action/ Button", desc)
    assert expected.prism_component, "fixture must cascade-resolve"

    mapping = _walk(
        _page_with_child(_instance(name="Action/ Button")),
        components=components,
        catalog=_catalog(_entry("some-other-key", "Tables")),
    )
    region = _identity_region(mapping)
    res = region.prism_resolution
    assert res is not None
    assert res.source == "page-fallback"
    assert res.prism_component == expected.prism_component
    assert region.mapping.suggested_component_name == expected.prism_component


# --------------------------------------------------------------------------
# Audited pattern roles keep the headline; catalog only corroborates.
# --------------------------------------------------------------------------


def _pattern_map_fn(**kwargs: Any) -> FigmaNodeMapping:
    """Stub mapper that mimics a deterministic pattern pick."""
    return FigmaNodeMapping(
        node_name=kwargs.get("node_name", ""),
        suggested_component_name="TableColumn",
        primary_recommendation="TableColumn",
        primary_recommendation_confidence=_PRIMARY_RECOMMENDATION_CONFIDENCE,
        primary_recommendation_rationale="pattern role 'table-column'",
    )


def test_audited_pattern_headline_not_overridden_by_catalog() -> None:
    components = {"10:1": {"key": "tblkey", "name": "Table/Cell"}}
    mapping = _walk(
        _page_with_child(_instance(name="Table/Cell")),
        components=components,
        catalog=_catalog(_entry("tblkey", "Tables")),
        map_fn=_pattern_map_fn,
    )
    region = _identity_region(mapping)

    # Catalog identity is still recorded as provenance...
    assert region.prism_resolution is not None
    assert region.prism_resolution.prism_component == "Tables"
    # ...but the finer, audited pattern pick keeps the headline.
    assert region.mapping.primary_recommendation == "TableColumn"
    assert region.mapping.suggested_component_name == "TableColumn"


# --------------------------------------------------------------------------
# _promote_resolution_to_headline — focused unit.
# --------------------------------------------------------------------------


def _region(mapping: FigmaNodeMapping) -> MappedRegion:
    return MappedRegion(
        id="1:2",
        name="x",
        role="component-instance",
        bbox=(0, 0, 10, 10),
        mapping=mapping,
        figma_component=FigmaComponentIdentity(component_id="10:1"),
    )


def test_promote_overrides_when_no_pattern_claim() -> None:
    region = _region(
        FigmaNodeMapping(node_name="x", suggested_component_name="Guess")
    )
    res = RegionResolution(
        prism_component="Button",
        method="family-name",
        confidence=0.7,
        source="page-fallback",
        component_key="abcdef 1234",
    )
    _promote_resolution_to_headline(region, res)
    assert region.mapping.primary_recommendation == "Button"
    assert region.mapping.primary_recommendation_confidence == 0.7
    assert region.mapping.suggested_component_name == "Button"
    assert region.mapping.primary_recommendation_rationale.startswith(
        "Tier-1 page-fallback:"
    )


def test_promote_is_noop_when_pattern_already_claimed() -> None:
    region = _region(
        FigmaNodeMapping(
            node_name="x",
            suggested_component_name="ButtonGroup",
            primary_recommendation="ButtonGroup",
            primary_recommendation_confidence=_PATTERN_HEADLINE_CONFIDENCE,
        )
    )
    res = RegionResolution(
        prism_component="Button", method="key-override", confidence=1.0,
        source="catalog", component_key="k",
    )
    _promote_resolution_to_headline(region, res)
    # Untouched — the audited pattern sub-component wins the headline.
    assert region.mapping.primary_recommendation == "ButtonGroup"
    assert region.mapping.suggested_component_name == "ButtonGroup"


def test_pattern_headline_confidence_matches_mapping_source_of_truth() -> None:
    """The override guard must track the pattern recommendation's
    confidence so a future bump to one stays in lock-step."""
    assert _PATTERN_HEADLINE_CONFIDENCE == _PRIMARY_RECOMMENDATION_CONFIDENCE


# --------------------------------------------------------------------------
# No-op / disabled / fault-tolerance paths.
# --------------------------------------------------------------------------


def test_no_identity_no_resolution_and_no_summary_key() -> None:
    """No ``components`` map -> no identity -> no routing. The summary
    must omit ``catalog_resolved`` so the no-identity golden fixtures
    stay byte-for-byte unchanged."""
    mapping = _walk(_page_with_child(_instance()), catalog=_catalog())
    assert all(r.prism_resolution is None for r in mapping.agenda)
    assert "catalog_resolved" not in mapping.summary


def test_catalog_routing_disabled_keeps_identity_only() -> None:
    components = {"10:1": {"key": "k", "name": "x"}}
    mapping = _walk(
        _page_with_child(_instance()),
        components=components,
        catalog=_catalog(_entry("k", "Button")),
        catalog_routing=False,
    )
    region = _identity_region(mapping)
    assert region.figma_component is not None  # P1 identity still captured
    assert region.prism_resolution is None  # but Tier-1 routing skipped
    assert "catalog_resolved" not in mapping.summary


def test_unmapped_catalog_entry_leaves_resolution_none() -> None:
    components = {"10:1": {"key": "k", "name": "Zzx Nonsense Layer"}}
    mapping = _walk(
        _page_with_child(_instance(name="Zzx Nonsense Layer")),
        components=components,
        catalog=_catalog(_entry("k", "", method="unmapped", confidence=0.0)),
    )
    region = _identity_region(mapping)
    assert region.figma_component is not None
    assert region.prism_resolution is None


def test_missing_catalog_artifact_is_non_fatal(monkeypatch) -> None:
    """A missing/corrupt artifact downgrades to a logged warning — the
    walk still ships the P1 identity, never raises."""

    def _boom() -> FigmaCatalog:
        raise FileNotFoundError("artifact missing")

    monkeypatch.setattr("prism_mcp.figma.walker.get_catalog", _boom)
    components = {"10:1": {"key": "k", "name": "x"}}
    # catalog=None forces the lazy load, which now raises.
    mapping = _walk(_page_with_child(_instance()), components=components)
    region = _identity_region(mapping)
    assert region.figma_component is not None
    assert region.prism_resolution is None


# --------------------------------------------------------------------------
# Warning ordering — a Tier-1 catalog hit suppresses the stale
# ``low_confidence`` alarm the fuzzy ranker would otherwise raise.
# --------------------------------------------------------------------------


def _low_score_map_fn(**kwargs: Any) -> FigmaNodeMapping:
    """Stub mapper whose top candidate is always sub-threshold.

    Mirrors the fuzzy ranker on an annotation-master page where no
    BM25/dense signal clears :data:`~prism_mcp.figma.walker.
    _LOW_CONFIDENCE_THRESHOLD` (``0.05``). Used to prove the catalog
    override, not the fuzzy score, decides whether the warning fires.
    """
    name = str(kwargs.get("node_name", ""))
    return FigmaNodeMapping(
        node_name=name,
        suggested_component_name="GuessWidget",
        candidates=[
            CandidateMatch(
                name="GuessWidget",
                type="component",
                score=0.01,
                why_matched=[],
                summary="",
                source="bm25",
            )
        ],
    )


def test_catalog_hit_suppresses_low_confidence_warning() -> None:
    """Regression for the warning-ordering bug.

    The per-region ``low_confidence`` audit used to run inside
    :func:`_resolve_pending_mappings`, *before*
    :func:`_apply_catalog_routing`. A region the catalog resolved to an
    exact Prism component therefore still surfaced a stale warning naming
    the fuzzy candidate the catalog was about to override — flooding the
    consumer with false alarms on a spec that was, in fact, deterministic
    (see ``improvements/09-warning-ordering-fix.md``). The audit now runs
    after routing, so a Tier-1 hit (which sets ``primary_recommendation``)
    suppresses the flag; only genuinely-unresolved regions stay flagged.

    Two instances exercise both arms in one walk:

    * ``1:2`` (``Action/ Button``, key in catalog) -> ``Button`` -> the
      warning MUST NOT fire even though the fuzzy score was 0.01.
    * ``1:3`` (``Mystery Layer``, key absent + name un-cascadable) -> stays
      on the fuzzy pick -> the warning MUST still fire.
    """
    page = {
        "id": "1:1",
        "name": "Page",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 300},
        "children": [
            _instance(node_id="1:2", name="Action/ Button", component_id="10:1"),
            _instance(node_id="1:3", name="Mystery Layer", component_id="10:2"),
        ],
    }
    components = {
        "10:1": {"key": "globalkeyA", "name": "Action/ Button"},
        "10:2": {"key": "unknownkey", "name": "Mystery Layer"},
    }
    catalog = _catalog(_entry("globalkeyA", "Button"))

    mapping = _walk(
        page,
        components=components,
        catalog=catalog,
        map_fn=_low_score_map_fn,
    )

    by_id = {r.id: r for r in mapping.agenda}
    # The catalog resolved 1:2 deterministically...
    assert by_id["1:2"].prism_resolution is not None
    assert by_id["1:2"].prism_resolution.prism_component == "Button"
    # ...while 1:3 stayed unresolved (no catalog key, un-cascadable name).
    assert by_id["1:3"].prism_resolution is None

    low_conf = [w for w in mapping.warnings if "low_confidence" in w]
    assert not any("region 1:2 (" in w for w in low_conf), (
        "catalog-resolved region 1:2 must not carry a stale low_confidence "
        f"warning; got {low_conf!r}"
    )
    assert any("region 1:3 (" in w for w in low_conf), (
        "genuinely-unresolved region 1:3 must still be flagged "
        f"low_confidence; got {low_conf!r}"
    )


# --------------------------------------------------------------------------
# Lean response surfacing.
# --------------------------------------------------------------------------


def test_lean_response_surfaces_prism_resolution_only_when_resolved() -> None:
    components = {"10:1": {"key": "k", "name": "x"}}
    mapping = _walk(
        _page_with_child(_instance()),
        components=components,
        catalog=_catalog(_entry("k", "Button", confidence=1.0)),
    )
    lean = leanify_tree_mapping(mapping, "lean")

    resolved_rows = [
        r for r in lean["agenda"] if r.get("figma_component") is not None
    ]
    assert resolved_rows, "expected an identity row in the lean agenda"
    row = resolved_rows[0]
    assert row["prism_resolution"] == {
        "prism_component": "Button",
        "source": "catalog",
        "method": "key-override",
        "confidence": 1.0,
    }

    # Rows without a DS identity must NOT carry the key at all, so every
    # pre-P3 fixture / mock stays byte-for-byte identical.
    for other in lean["agenda"]:
        if other.get("figma_component") is None:
            assert "prism_resolution" not in other
