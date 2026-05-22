"""Tests for the hook walker (Slice 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.parsers.hooks import walk_hooks
from tests.conftest import make_prism_tarball


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Synthetic tarball with two named hooks extracted to disk."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        hooks=("useFocusTrap", "useResizeObserver"),
    )
    return cache.install_tarball("2.54.0", tarball)


def test_walks_each_hook_file(extracted_package: Path) -> None:
    """Every ``use*`` d.ts becomes one hook entity."""
    entities = walk_hooks(extracted_package, version="2.54.0")

    names = sorted(e.name for e in entities)
    assert names == ["useFocusTrap", "useResizeObserver"]
    for entity in entities:
        assert entity.type == "hook"
        assert entity.version == "2.54.0"


def test_hook_signature_carries_params(extracted_package: Path) -> None:
    """Params from the d.ts surface as ``Member`` rows with ``kind=param``."""
    entities = walk_hooks(extracted_package, version="2.54.0")
    focus_trap = next(e for e in entities if e.name == "useFocusTrap")

    param_names = {m.name for m in focus_trap.signature}
    assert {"innerRef", "options"} <= param_names
    for member in focus_trap.signature:
        assert member.kind == "param"


def test_hook_summary_comes_from_jsdoc(extracted_package: Path) -> None:
    """The JSDoc description is mirrored into ``Entity.summary``."""
    entities = walk_hooks(extracted_package, version="2.54.0")
    focus_trap = next(e for e in entities if e.name == "useFocusTrap")

    assert "Trap focus" in focus_trap.summary


def test_canonical_import_for_hooks(extracted_package: Path) -> None:
    """Hooks use the named-import shape."""
    entities = walk_hooks(extracted_package, version="2.54.0")

    for entity in entities:
        assert entity.import_path == (
            f"import {{ {entity.name} }} from '@nutanix-ui/prism-reactjs';"
        )


def test_missing_hooks_dir_returns_empty(tmp_path: Path) -> None:
    """A package without ``lib/hooks`` yields ``[]`` rather than raising."""
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

    assert walk_hooks(package_root, version="0.0.1") == []


def test_non_hook_exports_in_a_hook_file_are_ignored(
    tmp_path: Path,
) -> None:
    """Only ``use*``-named exports become hook entities."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        components=(),
        extra_files={
            "lib/hooks/useMixed.d.ts": (
                "export declare const useReal: (n: number) => number;\n"
                "export declare const helper: (n: number) => number;\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    names = [e.name for e in walk_hooks(package_root, version="2.54.0")]

    assert names == ["useReal"]
