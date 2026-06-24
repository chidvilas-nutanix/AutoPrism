"""P2 catalog: resolution cascade, builder, loader, and runtime resolve.

Covers ``prism_mcp.figma.catalog`` — the deterministic
``componentKey -> Prism component`` identity layer (roadmap P2, see
``improvements/03-phase2-catalog.md``). Pure-function tests need no
network or rplib; the handful that load the committed
``data/figma_catalog.json`` double as regression guards on the shipped
artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prism_mcp.figma.catalog import (
    CATALOG_SCHEMA_VERSION,
    DATA_PATH,
    CatalogEntry,
    FigmaCatalog,
    LibraryDump,
    assert_targets_valid,
    build_catalog,
    build_catalog_entries,
    get_catalog,
    normalize_family,
    resolve_prism_component,
)
from prism_mcp.figma.catalog_overrides import PRISM_V2_COMPONENTS

_BUTTON_URL = "http://prism-styleguide/v2/index.html#/Components/Actions?id=button"
_SCROLLBAR_URL = (
    "http://prism-styleguide/v2/index.html#/Layouts/Structure?id=scrollbar"
)


# --------------------------------------------------------------------------
# normalize_family
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Action/ \u2705 Button", "action"),
        ("\u23f3 Accordion (detach asset)", "accordion"),
        ("Carousel (slot)", "carousel"),
        ("Alert Banners/ \u2705 Standard", "alert banners"),
        ("_structure", "structure"),
        ("Input/Multi-Input", "input"),
        ("", ""),
    ],
)
def test_normalize_family(raw: str, expected: str) -> None:
    assert normalize_family(raw) == expected


# --------------------------------------------------------------------------
# resolve_prism_component cascade
# --------------------------------------------------------------------------


def test_styleguide_id_is_highest_non_override_tier() -> None:
    res = resolve_prism_component("Whatever/Name", _BUTTON_URL)
    assert res.prism_component == "Button"
    assert res.method == "styleguide-id"
    assert res.styleguide_slug == "button"
    assert res.confidence == 1.0
    assert res.doc_url == _BUTTON_URL


def test_styleguide_id_matches_non_component_sections() -> None:
    # Slug lives under #/Layouts/... not #/Components/... — must still hit.
    res = resolve_prism_component("Misc/Scrollbar", _SCROLLBAR_URL)
    assert res.prism_component == "Scrollbar"
    assert res.method == "styleguide-id"
    assert res.styleguide_slug == "scrollbar"


def test_styleguide_icon_slug_routes_to_icons() -> None:
    res = resolve_prism_component(
        "Some/Thing", "http://prism-styleguide/#/Icons/Alert?id=alerticon"
    )
    assert res.prism_component == "Icons"
    assert res.method == "icon-family"


def test_ds_slug_resolution() -> None:
    res = resolve_prism_component(
        "Status/Tag", "see https://ds.nutanix.design/components/tag for usage"
    )
    assert res.prism_component == "Badge"
    assert res.method == "ds-slug"
    assert res.confidence == 0.95


def test_icon_family_by_name() -> None:
    res = resolve_prism_component("Icon/Add", "")
    assert res.prism_component == "Icons"
    assert res.method == "icon-family"


def test_family_name_fallback() -> None:
    res = resolve_prism_component("Action/Primary", "")
    assert res.prism_component == "Button"
    assert res.method == "family-name"
    assert res.confidence == 0.7
    assert res.styleguide_slug is None


def test_family_unsupported_is_distinct_from_unmapped() -> None:
    unsupported = resolve_prism_component("Brand/Logo", "")
    assert unsupported.prism_component == ""
    assert unsupported.method == "family-unsupported"

    unmapped = resolve_prism_component("Totally Unknown Family", "")
    assert unmapped.prism_component == ""
    assert unmapped.method == "unmapped"


def test_styleguide_beats_family_name() -> None:
    # A name whose family would map to Tables, but the description carries
    # the Button styleguide URL — the URL (higher tier) must win.
    res = resolve_prism_component("Table/Weird", _BUTTON_URL)
    assert res.prism_component == "Button"
    assert res.method == "styleguide-id"


def test_key_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from prism_mcp.figma import catalog as catalog_mod

    monkeypatch.setitem(catalog_mod.KEY_OVERRIDES, "PINNED", "Modal")
    res = resolve_prism_component(
        "Action/Button", _BUTTON_URL, component_key="PINNED"
    )
    assert res.prism_component == "Modal"
    assert res.method == "key-override"
    assert res.confidence == 1.0


# --------------------------------------------------------------------------
# build_catalog_entries — the component -> set join
# --------------------------------------------------------------------------


def _sample_library() -> LibraryDump:
    return LibraryDump(
        key="LIB",
        name="Test Lib",
        component_sets=[
            {
                "key": "setkey1",
                "node_id": "10:0",
                "name": "Action/ \u2705 Button",
                "description": _BUTTON_URL,
            }
        ],
        components=[
            {
                "key": "variantkey",
                "node_id": "10:1",
                "name": "Type=Primary",
                "description": "",
                "containing_frame": {
                    "containingStateGroup": {
                        "nodeId": "10:0",
                        "name": "Action/ \u2705 Button",
                    }
                },
            },
            {
                "key": "standalonekey",
                "node_id": "20:1",
                "name": "Tooltip/Default",
                "description": (
                    "http://prism-styleguide/#/Components/Tooltip?id=tooltip"
                ),
            },
        ],
    )


def test_build_entries_variant_inherits_set_identity() -> None:
    entries = build_catalog_entries([_sample_library()])

    set_entry = entries["setkey1"]
    assert set_entry.kind == "component_set"
    assert set_entry.prism_component == "Button"

    variant = entries["variantkey"]
    assert variant.kind == "component"
    # variant's own name/desc are empty -> resolved through the set
    assert variant.prism_component == "Button"
    assert variant.method == "styleguide-id"
    assert variant.figma_name == "Action/ \u2705 Button"
    assert variant.figma_family == "action"
    assert variant.component_set_key == "setkey1"


def test_build_entries_standalone_component() -> None:
    entries = build_catalog_entries([_sample_library()])
    standalone = entries["standalonekey"]
    assert standalone.prism_component == "Tooltip"
    assert standalone.component_set_key is None


def test_build_entries_are_sorted() -> None:
    entries = build_catalog_entries([_sample_library()])
    assert list(entries) == sorted(entries)


# --------------------------------------------------------------------------
# assert_targets_valid
# --------------------------------------------------------------------------


def test_assert_targets_valid_rejects_non_canonical() -> None:
    bad = {
        "k": CatalogEntry(
            component_key="k",
            prism_component="Bogus",
            kind="component",
            method="family-name",
            confidence=0.7,
            figma_name="X",
            library_key="L",
            library_name="L",
        )
    }
    with pytest.raises(ValueError, match="non-canonical"):
        assert_targets_valid(bad)


def test_assert_targets_valid_passes_real_entries() -> None:
    assert_targets_valid(build_catalog_entries([_sample_library()]))


# --------------------------------------------------------------------------
# FigmaCatalog loader + resolve_region
# --------------------------------------------------------------------------


def _sample_catalog() -> FigmaCatalog:
    artifact = build_catalog([_sample_library()], rplib_version="test")
    return FigmaCatalog.from_artifact(artifact)


def test_from_artifact_rejects_schema_mismatch() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        FigmaCatalog.from_artifact({"schema_version": 999, "entries": {}})


def test_lookup_and_resolve() -> None:
    cat = _sample_catalog()
    assert cat.lookup("variantkey").prism_component == "Button"
    assert cat.lookup("missing") is None
    assert cat.resolve("variantkey").is_mapped
    # an unmapped (or unknown) key returns None from resolve()
    assert cat.resolve("missing") is None


def test_resolve_region_prefers_catalog() -> None:
    cat = _sample_catalog()
    res = cat.resolve_region(
        component_key="variantkey", figma_name="ignored", description=""
    )
    assert res.source == "catalog"
    assert res.prism_component == "Button"
    assert res.component_set_key == "setkey1"


def test_resolve_region_page_fallback_for_unknown_key() -> None:
    cat = _sample_catalog()
    # Key absent from the catalog (remote/un-ingested library) but the
    # page-provided identity carries the styleguide URL -> cascade hits.
    res = cat.resolve_region(
        component_key="REMOTE_KEY_NOT_IN_CATALOG",
        figma_name="Action/Link",
        description=_BUTTON_URL,
    )
    assert res.source == "page-fallback"
    assert res.prism_component == "Button"


def test_resolve_region_none_when_unresolvable() -> None:
    cat = _sample_catalog()
    res = cat.resolve_region(
        component_key="UNKNOWN", figma_name="Frame 12345", description=""
    )
    assert res.source == "none"
    assert not res.is_mapped


# --------------------------------------------------------------------------
# Committed artifact regression guards (load data/figma_catalog.json)
# --------------------------------------------------------------------------


def test_committed_catalog_loads_and_is_large() -> None:
    cat = FigmaCatalog.load()
    assert len(cat) > 3000
    assert cat.meta["schema_version"] == CATALOG_SCHEMA_VERSION


def test_committed_catalog_targets_are_all_canonical() -> None:
    """Every shipped target must be a real prism-react v2 component."""
    artifact = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    offenders = {
        key: entry["prism_component"]
        for key, entry in artifact["entries"].items()
        if entry.get("prism_component")
        and entry["prism_component"] not in PRISM_V2_COMPONENTS
    }
    assert offenders == {}


def test_committed_catalog_mapped_share_is_high() -> None:
    artifact = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    assert artifact["stats"]["mapped_pct"] >= 80.0


def test_get_catalog_is_cached_singleton() -> None:
    assert get_catalog() is get_catalog()


def _live_v2_dirs() -> set[str] | None:
    """Return the live rplib ``src/components/v2/*`` dir names, or None."""
    cache_root = Path.home() / ".cache" / "prism-mcp"
    matches = sorted(cache_root.glob("*/package/src/components/v2"))
    for v2 in matches:
        if v2.is_dir():
            return {p.name for p in v2.iterdir() if p.is_dir()}
    return None


def test_allowlist_matches_live_rplib_v2_dirs() -> None:
    """PRISM_V2_COMPONENTS must equal the shipped v2 component families.

    The catalog's target vocabulary is the ``src/components/v2/*``
    directory set of ``@nutanix-ui/prism-reactjs``. When the rplib cache
    is present we enforce the allowlist matches it exactly, so a library
    version bump that adds/removes a family fails loudly (the P2 CI
    guard). Skips when the cache has not been warmed (offline CI).
    """
    dirs = _live_v2_dirs()
    if dirs is None:
        pytest.skip("rplib cache not warmed; cannot cross-check v2 dirs")
    assert dirs == PRISM_V2_COMPONENTS
