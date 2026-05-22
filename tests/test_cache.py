"""Tests for the on-disk cache layout."""

from __future__ import annotations

import os
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from prism_mcp.cache import (
    LATEST_SYMLINK_NAME,
    PACKAGE_DIRNAME,
    Cache,
    CacheError,
)
from tests.conftest import build_tarball


def test_install_tarball_extracts_and_marks_cached(tmp_path: Path) -> None:
    """A freshly installed version becomes ``is_version_cached``."""
    cache = Cache(tmp_path / "root")
    tarball = build_tarball({"package.json": '{"name":"x","version":"1"}'})

    pkg_dir = cache.install_tarball("1.0.0", tarball)

    assert pkg_dir == cache.package_dir("1.0.0")
    assert pkg_dir.is_dir()
    assert (pkg_dir / "package.json").is_file()
    assert cache.is_version_cached("1.0.0") is True


def test_install_tarball_writes_raw_tgz_alongside(tmp_path: Path) -> None:
    """The raw ``.tgz`` is preserved for re-verification."""
    cache = Cache(tmp_path / "root")
    tarball = build_tarball({"package.json": "{}"})

    cache.install_tarball("1.0.0", tarball)

    assert cache.tarball_path("1.0.0").read_bytes() == tarball


def test_install_tarball_updates_latest_symlink(tmp_path: Path) -> None:
    """``latest`` symlink follows the most recently installed version."""
    cache = Cache(tmp_path / "root")
    tarball = build_tarball({"package.json": "{}"})

    cache.install_tarball("1.0.0", tarball)
    cache.install_tarball("2.0.0", tarball)

    symlink = cache.latest_symlink()
    assert symlink.is_symlink()
    assert os.readlink(symlink) == "2.0.0"


def test_latest_cached_version_uses_symlink(tmp_path: Path) -> None:
    """``latest_cached_version`` reads through the symlink when valid."""
    cache = Cache(tmp_path / "root")
    cache.install_tarball("1.0.0", build_tarball({"package.json": "{}"}))
    cache.install_tarball("2.0.0", build_tarball({"package.json": "{}"}))

    assert cache.latest_cached_version() == "2.0.0"


def test_latest_cached_version_falls_back_when_symlink_broken(
    tmp_path: Path,
) -> None:
    """Broken symlink falls back to mtime-sorted list."""
    cache = Cache(tmp_path / "root")
    cache.install_tarball("1.0.0", build_tarball({"package.json": "{}"}))
    symlink = cache.latest_symlink()
    symlink.unlink()
    symlink.symlink_to("999.0.0")  # nonexistent

    assert cache.latest_cached_version() == "1.0.0"


def test_list_versions_skips_half_written_directories(
    tmp_path: Path,
) -> None:
    """A directory without ``package.json`` doesn't count as cached."""
    cache = Cache(tmp_path / "root")
    cache.ensure_root()

    half = cache.version_dir("0.9.0")
    (half / PACKAGE_DIRNAME).mkdir(parents=True)

    cache.install_tarball("1.0.0", build_tarball({"package.json": "{}"}))

    assert cache.list_versions() == ["1.0.0"]


def test_install_tarball_is_idempotent(tmp_path: Path) -> None:
    """Re-installing the same version is a no-op."""
    cache = Cache(tmp_path / "root")
    tarball = build_tarball({"package.json": "{}"})

    first = cache.install_tarball("1.0.0", tarball)
    second = cache.install_tarball("1.0.0", b"ignored")  # not extracted

    assert first == second
    assert cache.tarball_path("1.0.0").read_bytes() == tarball


def test_install_tarball_rejects_missing_package_root(
    tmp_path: Path,
) -> None:
    """Tarballs without a top-level ``package/`` directory are refused."""
    cache = Cache(tmp_path / "root")

    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="not-package/package.json")
        info.size = 2
        tar.addfile(info, BytesIO(b"{}"))

    with pytest.raises(CacheError, match=r"package.* directory"):
        cache.install_tarball("1.0.0", buffer.getvalue())


def test_install_tarball_blocks_path_traversal(tmp_path: Path) -> None:
    """``..``-escaping tarball members are refused."""
    cache = Cache(tmp_path / "root")

    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="package/../escape.txt")
        info.size = 4
        tar.addfile(info, BytesIO(b"oops"))

    with pytest.raises(CacheError, match="escape"):
        cache.install_tarball("1.0.0", buffer.getvalue())


def test_latest_symlink_not_listed_as_version(tmp_path: Path) -> None:
    """The ``latest`` symlink itself isn't a version."""
    cache = Cache(tmp_path / "root")
    cache.install_tarball("1.0.0", build_tarball({"package.json": "{}"}))

    assert LATEST_SYMLINK_NAME not in cache.list_versions()
