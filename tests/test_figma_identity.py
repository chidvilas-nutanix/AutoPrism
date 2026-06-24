"""P1 fetch-fix: exact design-system identity capture in the walker.

These tests cover the payoff of preserving the Figma ``components`` /
``componentSets`` maps (``improvements/02-phase1-fetch-fix.md``): the
walker resolves each ``INSTANCE`` / ``COMPONENT`` region's node-local
``componentId`` into its global ``componentKey`` + logical name +
styleguide URL, surfaced on :attr:`MappedRegion.figma_component`.

The walker is exercised with ``map_figma_node_fn=None`` so the tests are
independent of the live Prism library index — identity resolution does
not depend on the mapper.
"""

from __future__ import annotations

from typing import Any

from prism_mcp.figma import FigmaComponentIdentity, walk_tree
from prism_mcp.figma.models import FigmaTreeMapping
from prism_mcp.figma.walker import _parse_doc_url


def _walk(
    document: dict[str, Any],
    *,
    components: dict[str, Any] | None = None,
    component_sets: dict[str, Any] | None = None,
) -> FigmaTreeMapping:
    return walk_tree(
        tree_json=document,
        components=components,
        component_sets=component_sets,
        map_figma_node_fn=None,
    )


def _identities(mapping: FigmaTreeMapping) -> list[FigmaComponentIdentity]:
    return [r.figma_component for r in mapping.agenda if r.figma_component]


def _page_with_child(child: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "1:1",
        "name": "Page",
        "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 300},
        "children": [child],
    }


# --------------------------------------------------------------------------
# _parse_doc_url — pure helper.
# --------------------------------------------------------------------------


def test_parse_doc_url_extracts_styleguide_url() -> None:
    desc = (
        "http://prism-styleguide/v2/index.html#/Components/Actions?id=button"
        "\nhttps://ds.nutanix.design/components/buttons"
    )
    assert _parse_doc_url(desc) == (
        "http://prism-styleguide/v2/index.html#/Components/Actions?id=button"
    )


def test_parse_doc_url_returns_none_without_url() -> None:
    assert _parse_doc_url("just a description, no link") is None
    assert _parse_doc_url("") is None


def test_parse_doc_url_strips_trailing_punctuation() -> None:
    assert _parse_doc_url("see (https://x.test/comp).") == "https://x.test/comp"


# --------------------------------------------------------------------------
# Instance identity — the 100%-present real-world case.
# --------------------------------------------------------------------------


def test_instance_identity_resolved_from_components_map() -> None:
    """An INSTANCE's componentId resolves to its global key + name."""
    instance = {
        "id": "1:2",
        "name": "Button",
        "type": "INSTANCE",
        "componentId": "10:1",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }
    components = {
        "10:1": {
            "key": "globalkeyA",
            "name": "Action/ \u2705 Button",
            "description": (
                "http://prism-styleguide/v2/index.html"
                "#/Components/Actions?id=button"
            ),
            "remote": True,
            "documentationLinks": [],
        }
    }
    ids = _identities(_walk(_page_with_child(instance), components=components))
    assert len(ids) == 1
    ident = ids[0]
    assert ident.component_id == "10:1"
    assert ident.component_key == "globalkeyA"
    assert ident.component_name == "Action/ \u2705 Button"
    assert ident.remote is True
    assert ident.component_set_id is None
    assert ident.component_set_key is None
    assert ident.doc_url == (
        "http://prism-styleguide/v2/index.html#/Components/Actions?id=button"
    )


def test_instance_identity_prefers_component_set_name_and_key() -> None:
    """When the instance belongs to a set, the logical name + key + desc
    come from the componentSets entry, while component_key stays the
    specific variant's key."""
    instance = {
        "id": "2:2",
        "name": "Primary",
        "type": "INSTANCE",
        "componentId": "20:1",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }
    components = {
        "20:1": {
            "key": "variantkey",
            "name": "Type=Primary",
            "componentSetId": "20:0",
            "remote": True,
            "description": "variant-level desc (no url)",
        }
    }
    component_sets = {
        "20:0": {
            "key": "setkey",
            "name": "Action/ \u2705 Button",
            "description": "https://ds.nutanix.design/components/buttons",
        }
    }
    ids = _identities(
        _walk(
            _page_with_child(instance),
            components=components,
            component_sets=component_sets,
        )
    )
    assert len(ids) == 1
    ident = ids[0]
    assert ident.component_key == "variantkey"
    assert ident.component_set_id == "20:0"
    assert ident.component_set_key == "setkey"
    assert ident.component_name == "Action/ \u2705 Button"
    assert ident.doc_url == "https://ds.nutanix.design/components/buttons"


# --------------------------------------------------------------------------
# Backward-compat + fallback paths.
# --------------------------------------------------------------------------


def test_no_components_map_leaves_identity_none() -> None:
    """Legacy / document-only path: no maps → no identity. This is the
    byte-for-byte backward-compatible behaviour every existing caller
    and golden fixture relies on."""
    instance = {
        "id": "1:2",
        "name": "Button",
        "type": "INSTANCE",
        "componentId": "10:1",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }
    mapping = _walk(_page_with_child(instance))  # no components passed
    assert all(r.figma_component is None for r in mapping.agenda)


def test_detached_instance_not_in_map_is_none() -> None:
    """An instance whose componentId is absent from the map (detached /
    local) resolves to None — genuine Tier-3 fallback territory."""
    instance = {
        "id": "1:2",
        "name": "Button",
        "type": "INSTANCE",
        "componentId": "99:9",  # not in the map
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40},
    }
    components = {"10:1": {"key": "globalkeyA", "name": "Action/ Button"}}
    ids = _identities(_walk(_page_with_child(instance), components=components))
    assert ids == []


def test_component_node_resolved_via_own_id() -> None:
    """A COMPONENT (definition) node resolves via its own ``id``."""
    component = {
        "id": "30:1",
        "name": "Spinner",
        "type": "COMPONENT",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 48, "height": 48},
        "fills": [
            {"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}, "opacity": 1}
        ],
    }
    components = {
        "30:1": {
            "key": "compkey",
            "name": "Loader/Spinner \u2705",
            "remote": False,
        }
    }
    ids = _identities(
        _walk(_page_with_child(component), components=components)
    )
    assert len(ids) == 1
    assert ids[0].component_key == "compkey"
    assert ids[0].component_name == "Loader/Spinner \u2705"
    assert ids[0].remote is False
