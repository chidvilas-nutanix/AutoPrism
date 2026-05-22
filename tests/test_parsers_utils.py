"""Tests for the util walker (Slice 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.parsers.utils import walk_utils
from tests.conftest import make_prism_tarball


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Synthetic tarball with one util file (two exports)."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(version="2.54.0", utils=("A11yUtils",))
    return cache.install_tarball("2.54.0", tarball)


def test_walks_each_util_callable(extracted_package: Path) -> None:
    """Each exported function becomes one util entity."""
    entities = walk_utils(extracted_package, version="2.54.0")

    names = sorted(e.name for e in entities)
    assert names == ["buildComponentId", "isAriaString"]
    for entity in entities:
        assert entity.type == "util"


def test_util_signature_carries_params(extracted_package: Path) -> None:
    """Param list lands in ``Entity.signature``."""
    entities = walk_utils(extracted_package, version="2.54.0")
    build = next(e for e in entities if e.name == "buildComponentId")

    assert [m.name for m in build.signature] == ["segments"]
    assert build.signature[0].kind == "param"
    assert build.signature[0].type == "string[]"


def test_util_walker_descends_into_v2_subdir(tmp_path: Path) -> None:
    """``lib/utils/v2/*.d.ts`` is picked up alongside the flat tree."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "lib/utils/flat.d.ts": (
                "export declare const flatFn: (x: number) => number;\n"
            ),
            "lib/utils/v2/Nested.d.ts": (
                "export declare const nestedFn: (s: string) => string;\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    names = [e.name for e in walk_utils(package_root, version="2.54.0")]

    assert names == ["flatFn", "nestedFn"]


def test_missing_utils_dir_returns_empty(tmp_path: Path) -> None:
    """A package without ``lib/utils`` yields ``[]``."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="0.0.1",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
    )
    package_root = cache.install_tarball("0.0.1", tarball)

    assert walk_utils(package_root, version="0.0.1") == []
