"""Tests for the manager walker (Slice 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.parsers.managers import walk_managers
from tests.conftest import make_prism_tarball


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Synthetic tarball with two manager d.ts files."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        managers=("I18nManager", "ThemeManager"),
    )
    return cache.install_tarball("2.54.0", tarball)


def test_walks_each_manager_file(extracted_package: Path) -> None:
    """Every ``*Manager.d.ts`` becomes one manager entity."""
    entities = walk_managers(extracted_package, version="2.54.0")

    names = sorted(e.name for e in entities)
    assert names == ["I18nManager", "ThemeManager"]
    for entity in entities:
        assert entity.type == "manager"


def test_manager_signature_carries_methods(extracted_package: Path) -> None:
    """Methods land in ``Entity.signature`` with ``kind=method``."""
    entities = walk_managers(extracted_package, version="2.54.0")
    i18n = next(e for e in entities if e.name == "I18nManager")

    method_names = {m.name for m in i18n.signature}
    assert {"initialize", "t", "setLocale"} <= method_names
    assert "constructor" not in method_names
    for member in i18n.signature:
        assert member.kind == "method"
        assert member.type.startswith("(")


def test_manager_summary_comes_from_class_jsdoc(
    extracted_package: Path,
) -> None:
    """The class-level JSDoc lands in ``Entity.summary``."""
    entities = walk_managers(extracted_package, version="2.54.0")
    i18n = next(e for e in entities if e.name == "I18nManager")

    assert "singleton" in i18n.summary.lower()


def test_canonical_import_for_managers(extracted_package: Path) -> None:
    """Managers use default-import shape (``import X from ...;``)."""
    entities = walk_managers(extracted_package, version="2.54.0")

    for entity in entities:
        assert entity.import_path == (
            f"import {entity.name} from '@nutanix-ui/prism-reactjs';"
        )


def test_non_manager_classes_in_file_are_ignored(tmp_path: Path) -> None:
    """Only classes whose name ends in ``Manager`` are surfaced."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "lib/managers/MixedManager.d.ts": (
                "declare class HelperType {\n"
                "    helper(): void;\n"
                "}\n"
                "declare class CacheManager {\n"
                "    /** Manager summary. */\n"
                "    get(key: string): string;\n"
                "}\n"
                "declare const instance: CacheManager;\n"
                "export default instance;\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    names = [e.name for e in walk_managers(package_root, version="2.54.0")]

    assert names == ["CacheManager"]
