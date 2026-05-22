"""Tests for the component walker."""

from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.parsers.components import walk_components
from tests.conftest import make_prism_tarball


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Return a package_root with the synthetic prism tarball extracted."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0", components=("Button", "Modal")
    )
    return cache.install_tarball("2.54.0", tarball)


def test_walks_each_v2_component(extracted_package: Path) -> None:
    """Every component folder under ``lib/components/v2/`` becomes one entity."""
    entities = walk_components(extracted_package, version="2.54.0")

    names = sorted(e.name for e in entities)
    assert names == ["Button", "Modal"]
    for entity in entities:
        assert entity.type == "component"
        assert entity.version == "2.54.0"


def test_component_signature_extracted_from_dts(
    extracted_package: Path,
) -> None:
    """Props from the d.ts land in ``Entity.signature``."""
    entities = walk_components(extracted_package, version="2.54.0")
    button = next(e for e in entities if e.name == "Button")

    prop_names = {m.name for m in button.signature}

    assert {"className", "disabled", "onClick", "children"} <= prop_names

    on_click = next(m for m in button.signature if m.name == "onClick")
    assert on_click.kind == "prop"
    assert "MouseEvent" in on_click.type
    assert on_click.required is False


def test_canonical_import_uses_package_root(
    extracted_package: Path,
) -> None:
    """``import_path`` is a paste-ready statement, not just the package."""
    entities = walk_components(extracted_package, version="2.54.0")
    button = next(e for e in entities if e.name == "Button")

    assert button.import_path == (
        "import { Button } from '@nutanix-ui/prism-reactjs';"
    )


def test_examples_md_drives_summary_and_examples(
    extracted_package: Path,
) -> None:
    """Summary and example list come from ``X.examples.md``."""
    entities = walk_components(extracted_package, version="2.54.0")
    button = next(e for e in entities if e.name == "Button")

    assert len(button.examples) >= 1
    assert any("<Button" in ex.code for ex in button.examples)


def test_missing_lib_dir_returns_empty(tmp_path: Path) -> None:
    """A package with no ``lib/components/v2`` yields no entities."""
    cache = Cache(tmp_path / "cache")

    bare = make_minimal_tarball()
    package_root = cache.install_tarball("0.0.1", bare)

    assert walk_components(package_root, version="0.0.1") == []


def make_minimal_tarball() -> bytes:
    """Return a tarball with only ``package.json`` (no components)."""
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        payload = b'{"name":"x","version":"0.0.1"}'
        info = tarfile.TarInfo(name="package/package.json")
        info.size = len(payload)
        tar.addfile(info, BytesIO(payload))
    return buffer.getvalue()
