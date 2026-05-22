"""Tests for the ``Library`` orchestrator (Slice 2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from prism_mcp.cache import Cache
from prism_mcp.config import ServerConfig
from prism_mcp.library import Library, LibraryError
from prism_mcp.registry import RegistryClient
from tests.conftest import make_latest_manifest, make_prism_tarball

PACKAGE = "@nutanix-ui/prism-reactjs"
VERSION = "2.54.0"
TARBALL_URL = f"https://registry.test/{PACKAGE}/-/prism-reactjs-{VERSION}.tgz"

Handler = Callable[[httpx.Request], httpx.Response]


def _config(cache_root: Path) -> ServerConfig:
    """Build a ServerConfig pointing at a tmp cache."""
    return ServerConfig(
        registry_base_url="https://registry.test/api/npm/canaveral-npm/",
        package_name=PACKAGE,
        cache_dir=cache_root,
        auth_header="Basic dGVzdA==",
    )


def _library(cache_root: Path, handler: Handler) -> Library:
    """Build a Library wired against a MockTransport."""
    config = _config(cache_root)
    cache = Cache(cache_root)
    client = RegistryClient(
        base_url=config.registry_base_url,
        auth_header=config.auth_header,
        transport=httpx.MockTransport(handler),
    )
    return Library(config=config, registry=client, cache=cache)


def _manifest_response(
    tarball_bytes: bytes,
    etag: str = '"v1"',
) -> tuple[dict[str, Any], str]:
    """Return a manifest body referencing ``tarball_bytes``."""
    return make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball_bytes,
    ), etag


def test_acquire_latest_downloads_and_extracts(
    cache_root: Path,
) -> None:
    """Cold acquire: fetch manifest + tarball, verify, extract."""
    tarball = make_prism_tarball(version=VERSION)
    manifest, etag = _manifest_response(tarball)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": etag},
            content=json.dumps(manifest),
        )

    library = _library(cache_root, handler)
    meta = library.acquire_latest()

    assert meta.package_name == PACKAGE
    assert meta.version == VERSION
    assert meta.source_url == TARBALL_URL
    assert meta.from_cache is False
    assert Path(meta.cache_path).is_dir()
    assert (Path(meta.cache_path) / "package.json").is_file()


def test_acquire_latest_returns_304_meta_on_second_call(
    cache_root: Path,
) -> None:
    """Second call gets 304 and reuses in-process meta."""
    tarball = make_prism_tarball(version=VERSION)
    manifest, etag = _manifest_response(tarball)
    call_count = {"value": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        call_count["value"] += 1
        if call_count["value"] == 1:
            return httpx.Response(
                200,
                headers={"ETag": etag},
                content=json.dumps(manifest),
            )
        assert request.headers.get("If-None-Match") == etag
        return httpx.Response(304)

    library = _library(cache_root, handler)
    first = library.acquire_latest()
    second = library.acquire_latest()

    assert second is first  # same object reused on 304


def test_acquire_latest_skips_download_if_version_already_cached(
    cache_root: Path,
) -> None:
    """Same-version response uses on-disk cache; no tarball GET issued."""
    tarball = make_prism_tarball(version=VERSION)
    Cache(cache_root).install_tarball(VERSION, tarball)
    manifest, etag = _manifest_response(tarball)
    tarball_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            tarball_calls.append(str(request.url))
            return httpx.Response(500)
        return httpx.Response(
            200,
            headers={"ETag": etag},
            content=json.dumps(manifest),
        )

    library = _library(cache_root, handler)
    meta = library.acquire_latest()

    assert tarball_calls == []
    assert meta.version == VERSION
    assert meta.from_cache is False  # we did reach the registry


def test_acquire_latest_falls_back_to_cache_on_registry_error(
    cache_root: Path,
) -> None:
    """Network fail with cache present => ``from_cache=True`` meta."""
    tarball = make_prism_tarball(version=VERSION)
    Cache(cache_root).install_tarball(VERSION, tarball)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    library = _library(cache_root, handler)
    meta = library.acquire_latest()

    assert meta.from_cache is True
    assert meta.version == VERSION


def test_acquire_latest_raises_when_no_cache_and_registry_down(
    cache_root: Path,
) -> None:
    """Cold + offline => LibraryError, never a silent empty meta."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    library = _library(cache_root, handler)
    with pytest.raises(LibraryError, match="no cache is populated"):
        library.acquire_latest()


def test_acquire_latest_raises_on_integrity_mismatch(
    cache_root: Path,
) -> None:
    """Mutated tarball is rejected; the cache is not populated."""
    tarball = make_prism_tarball(version=VERSION)
    manifest, etag = _manifest_response(tarball)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=b"tampered")
        return httpx.Response(
            200,
            headers={"ETag": etag},
            content=json.dumps(manifest),
        )

    library = _library(cache_root, handler)
    with pytest.raises(LibraryError, match="no cache is populated"):
        library.acquire_latest()

    assert not Cache(cache_root).is_version_cached(VERSION)


def test_refresh_swaps_index_on_new_version(
    cache_root: Path,
) -> None:
    """Slice 7 demo: a new dist-tags.latest swaps the in-memory index.

    The handler serves version A on the first poll, then version B on
    the second; ``refresh()`` must report ``swapped=True`` and the new
    :class:`~prism_mcp.indexer.Index` must reflect entities from
    version B's tarball (we add an extra component there to assert
    against).
    """
    version_a = "2.54.0"
    version_b = "2.55.0"
    tarball_a = make_prism_tarball(version=version_a, components=("Button",))
    tarball_b = make_prism_tarball(
        version=version_b, components=("Button", "Modal", "Tooltip")
    )
    tarball_url_b = (
        f"https://registry.test/{PACKAGE}/-/prism-reactjs-{version_b}.tgz"
    )

    state = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TARBALL_URL:
            return httpx.Response(200, content=tarball_a)
        if url == tarball_url_b:
            return httpx.Response(200, content=tarball_b)
        state["poll"] += 1
        if state["poll"] == 1:
            manifest = make_latest_manifest(
                package_name=PACKAGE,
                version=version_a,
                tarball_url=TARBALL_URL,
                tarball_bytes=tarball_a,
            )
            return httpx.Response(
                200, headers={"ETag": '"v-a"'}, content=json.dumps(manifest)
            )
        manifest = make_latest_manifest(
            package_name=PACKAGE,
            version=version_b,
            tarball_url=tarball_url_b,
            tarball_bytes=tarball_b,
        )
        return httpx.Response(
            200, headers={"ETag": '"v-b"'}, content=json.dumps(manifest)
        )

    library = _library(cache_root, handler)

    first = library.refresh()
    assert first.version_before is None
    assert first.version_after == version_a
    assert first.swapped is True
    assert first.not_modified is False
    assert first.offline is False
    assert library.index().get("Button", "component") is not None
    assert library.index().get("Tooltip", "component") is None

    second = library.refresh()

    assert second.version_before == version_a
    assert second.version_after == version_b
    assert second.swapped is True
    assert second.not_modified is False
    assert second.offline is False
    assert library.index().version == version_b
    assert library.index().get("Tooltip", "component") is not None


def test_refresh_is_noop_on_304_not_modified(
    cache_root: Path,
) -> None:
    """Slice 7 demo: a 304 leaves the index alone."""
    tarball = make_prism_tarball(version=VERSION)
    manifest = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    state = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        state["poll"] += 1
        if state["poll"] == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                content=json.dumps(manifest),
            )
        if request.headers.get("If-None-Match") != '"v1"':
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                content=json.dumps(manifest),
            )
        return httpx.Response(304, headers={"ETag": '"v1"'})

    library = _library(cache_root, handler)
    first = library.refresh()
    first_index_id = id(library.index())

    second = library.refresh()

    assert second.not_modified is True
    assert second.swapped is False
    assert second.version_before == VERSION
    assert second.version_after == VERSION
    assert id(library.index()) == first_index_id, (
        "index must not be rebuilt on 304"
    )
    assert first.version_after == VERSION


def test_refresh_falls_back_to_cache_when_registry_unreachable(
    cache_root: Path,
) -> None:
    """Slice 8 demo: ``httpx.ConnectError`` => degraded mode w/ cache."""
    tarball = make_prism_tarball(version=VERSION)
    manifest = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    state = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        state["poll"] += 1
        if state["poll"] == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                content=json.dumps(manifest),
            )
        raise httpx.ConnectError("offline")

    library = _library(cache_root, handler)
    library.refresh()
    pre_index_id = id(library.index())

    offline_outcome = library.refresh()

    assert offline_outcome.offline is True
    assert offline_outcome.version_after == VERSION
    assert offline_outcome.swapped is False
    assert id(library.index()) == pre_index_id


def test_refresh_cold_no_cache_raises_vpn_hint(
    cache_root: Path,
) -> None:
    """Cold start + offline => LibraryError mentioning VPN."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    library = _library(cache_root, handler)
    with pytest.raises(LibraryError, match="VPN"):
        library.refresh()


def test_acquire_latest_errors_on_manifest_missing_version(
    cache_root: Path,
) -> None:
    """Bad manifest (no ``version``) surfaces as LibraryError."""
    manifest = {"name": PACKAGE, "dist": {"tarball": TARBALL_URL}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(manifest))

    library = _library(cache_root, handler)
    with pytest.raises(LibraryError, match="missing 'version'"):
        library.acquire_latest()


def test_acquire_latest_errors_on_manifest_missing_tarball(
    cache_root: Path,
) -> None:
    """A manifest missing ``dist.tarball`` is a LibraryError."""
    manifest = {"name": PACKAGE, "version": VERSION, "dist": {}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(manifest))

    library = _library(cache_root, handler)
    with pytest.raises(LibraryError, match=r"missing dist\.tarball"):
        library.acquire_latest()
