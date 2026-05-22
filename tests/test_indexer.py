"""Tests for the :mod:`prism_mcp.indexer` module."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.entities import Entity, Member
from prism_mcp.indexer import Index, build_index
from tests.conftest import make_prism_tarball


@pytest.fixture()
def components_only_package(tmp_path: Path) -> Path:
    """Extract a components-only tarball; useful for type-filter tests."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=("Button", "Modal", "Alert"),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
    )
    return cache.install_tarball("2.54.0", tarball)


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Extract the default full synthetic tarball and return its root."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0", components=("Button", "Modal", "Alert")
    )
    return cache.install_tarball("2.54.0", tarball)


def test_build_index_populates_lookup_for_components(
    components_only_package: Path,
) -> None:
    """Every component shows up under ``(component, name)``."""
    index = build_index(components_only_package, version="2.54.0")

    assert len(index) == 3
    assert index.get("Button", "component") is not None
    assert index.get("Modal", "component") is not None
    assert index.get("Alert", "component") is not None


def test_index_get_returns_none_for_missing_key(
    extracted_package: Path,
) -> None:
    """A name that doesn't exist returns ``None``, not KeyError."""
    index = build_index(extracted_package, version="2.54.0")

    assert index.get("NotAComponent", "component") is None
    assert index.get("Button", "util") is None


def test_index_list_filters_by_type(extracted_package: Path) -> None:
    """``list(type=...)`` only returns matching entities."""
    index = build_index(extracted_package, version="2.54.0")

    components = index.list(type="component")
    hooks = index.list(type="hook")

    assert {e.name for e in components} == {"Button", "Modal", "Alert"}
    assert {e.name for e in hooks} == {"useFocusTrap"}


def test_index_list_excludes_deprecated_by_default() -> None:
    """Default ``list()`` hides deprecated entities."""
    entities = [
        _component("KeepMe", deprecated=False),
        _component("DropMe", deprecated=True),
    ]
    index = Index(entities=entities, version="1.0.0")

    visible = index.list()
    full = index.list(include_deprecated=True)

    assert {e.name for e in visible} == {"KeepMe"}
    assert {e.name for e in full} == {"KeepMe", "DropMe"}


def test_index_version_is_propagated(extracted_package: Path) -> None:
    """``Index.version`` matches the build-time version."""
    index = build_index(extracted_package, version="2.54.0")

    assert index.version == "2.54.0"


def test_index_duplicate_keys_last_wins(caplog) -> None:
    """Two entities with the same key produce a warning and last wins."""
    entities = [
        _component("Button", summary="first"),
        _component("Button", summary="second"),
    ]

    with caplog.at_level("WARNING"):
        index = Index(entities=entities, version="1.0.0")

    assert index.get("Button", "component").summary == "second"
    assert any("duplicate entity key" in r.message for r in caplog.records)


def _component(name: str, **kwargs) -> Entity:
    """Factory: return a minimal component entity for index tests."""
    return Entity(
        name=name,
        type="component",
        version="1.0.0",
        signature=[Member(name="x", kind="prop", type="string")],
        **kwargs,
    )
