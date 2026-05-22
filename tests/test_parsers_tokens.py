"""Tests for the LESS token extractor (Slice 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.cache import Cache
from prism_mcp.parsers.tokens import walk_tokens
from tests.conftest import make_prism_tarball


@pytest.fixture()
def extracted_package(tmp_path: Path) -> Path:
    """Synthetic tarball with the default styles tree extracted."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(version="2.54.0")
    return cache.install_tarball("2.54.0", tarball)


def test_walks_color_tokens(extracted_package: Path) -> None:
    """``Colors.less`` becomes a stream of ``color``-category tokens."""
    entities = walk_tokens(extracted_package, version="2.54.0")
    colors = {e.name: e for e in entities if e.category == "color"}

    assert {"color-primary", "color-secondary", "color-success"} <= set(colors)
    assert colors["color-primary"].value == "#1B6BCC"
    assert colors["color-primary"].source_file == ("src/styles/v2/Colors.less")


def test_skips_block_comment_contents(extracted_package: Path) -> None:
    """``/* @color-bogus: ... */`` is not extracted as a real token."""
    names = {e.name for e in walk_tokens(extracted_package, version="2.54.0")}

    assert "color-bogus" not in names


def test_skips_line_comment_contents(tmp_path: Path) -> None:
    """``// @fake: ...`` is not extracted as a real token."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "src/styles/v2/Colors.less": (
                "// @fake-token: red;\n@real-token: blue;\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    names = [e.name for e in walk_tokens(package_root, version="2.54.0")]

    assert names == ["real-token"]


def test_category_per_filename(extracted_package: Path) -> None:
    """The token's category is inferred from its source file."""
    entities = walk_tokens(extracted_package, version="2.54.0")

    by_name = {e.name: e for e in entities}

    assert by_name["color-primary"].category == "color"
    assert by_name["font-family-base"].category == "typography"
    assert by_name["z-modal"].category == "z-index"
    assert by_name["animation-duration-fast"].category == "animation"
    assert by_name["focus-ring-color"].category == "focus"


def test_unknown_file_uses_spacing_fallback(tmp_path: Path) -> None:
    """Tokens in an unrecognized file land in the ``spacing`` bucket."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "src/styles/v2/Spacing.less": "@spacing-md: 16px;\n",
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    entity = walk_tokens(package_root, version="2.54.0")[0]

    assert entity.name == "spacing-md"
    assert entity.category == "spacing"
    assert entity.value == "16px"


def test_value_with_function_call_is_preserved(tmp_path: Path) -> None:
    """LESS function calls in the value (``fade(@white, 90%)``) survive."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "src/styles/v2/Colors.less": (
                "@white: #ffffff;\n@white-alpha-90: fade(@white, 90%);\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    by_name = {e.name: e for e in walk_tokens(package_root, version="2.54.0")}

    assert by_name["white-alpha-90"].value == "fade(@white, 90%)"


def test_missing_styles_dir_returns_empty(tmp_path: Path) -> None:
    """A tarball without ``src/styles/v2`` yields ``[]``."""
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

    assert walk_tokens(package_root, version="0.0.1") == []


def test_aliased_value_is_preserved(tmp_path: Path) -> None:
    """``@focus-ring-color: @color-primary;`` keeps the alias intact."""
    cache = Cache(tmp_path / "cache")
    tarball = make_prism_tarball(
        version="2.54.0",
        components=(),
        hooks=(),
        managers=(),
        utils=(),
        include_tokens=False,
        extra_files={
            "src/styles/v2/Focus.less": (
                "@focus-ring-color: @color-primary;\n"
            ),
        },
    )
    package_root = cache.install_tarball("2.54.0", tarball)

    entity = walk_tokens(package_root, version="2.54.0")[0]

    assert entity.value == "@color-primary"
