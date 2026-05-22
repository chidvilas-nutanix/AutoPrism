"""Tests for the npm registry client."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from prism_mcp.registry import RegistryClient, RegistryError

Handler = Callable[[httpx.Request], httpx.Response]


def _client_with(handler: Handler) -> RegistryClient:
    """Return a registry client driven by ``handler`` for tests."""
    return RegistryClient(
        base_url="https://registry.example.test/api/npm/canaveral-npm/",
        auth_header="Basic dGVzdA==",
        transport=httpx.MockTransport(handler),
    )


def test_get_latest_manifest_returns_body_and_etag() -> None:
    """A 200 response yields the parsed manifest and the ETag header."""
    body = {
        "name": "@nutanix-ui/prism-reactjs",
        "version": "1.0.0",
        "dist": {"tarball": "https://r.test/x-1.0.0.tgz"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Basic dGVzdA=="
        assert request.url.raw_path.endswith(b"/latest")
        return httpx.Response(
            200,
            headers={"ETag": '"abc"'},
            content=json.dumps(body),
        )

    with _client_with(handler) as client:
        result = client.get_latest_manifest("@nutanix-ui/prism-reactjs")

    assert result.manifest == body
    assert result.etag == '"abc"'
    assert result.not_modified is False


def test_get_latest_manifest_sends_if_none_match_when_etag_given() -> None:
    """Prior ETag is forwarded as ``If-None-Match``."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("If-None-Match", ""))
        return httpx.Response(304)

    with _client_with(handler) as client:
        result = client.get_latest_manifest(
            "@nutanix-ui/prism-reactjs", etag='"abc"'
        )

    assert captured == ['"abc"']
    assert result.not_modified is True
    assert result.manifest is None
    assert result.etag == '"abc"'  # echoed back


def test_get_latest_manifest_raises_on_5xx() -> None:
    """Non-success status codes other than 304 surface as RegistryError."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"bad gateway")

    with (
        _client_with(handler) as client,
        pytest.raises(RegistryError, match="502"),
    ):
        client.get_latest_manifest("@nutanix-ui/prism-reactjs")


def test_get_latest_manifest_raises_on_transport_error() -> None:
    """``httpx.RequestError`` is wrapped as RegistryError for offline path."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with (
        _client_with(handler) as client,
        pytest.raises(RegistryError, match="transport error"),
    ):
        client.get_latest_manifest("@nutanix-ui/prism-reactjs")


def test_download_tarball_returns_bytes() -> None:
    """A 200 binary response is returned verbatim."""
    payload = b"\x1f\x8b\x08\x00\x00\x00\x00\x00pretend-gz"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/prism-reactjs-1.0.0.tgz")
        return httpx.Response(200, content=payload)

    url = (
        "https://registry.example.test/api/npm/canaveral-npm/"
        "@nutanix-ui/prism-reactjs/-/prism-reactjs-1.0.0.tgz"
    )
    with _client_with(handler) as client:
        body = client.download_tarball(url)

    assert body == payload


def test_download_tarball_raises_on_404() -> None:
    """Tarball 404 surfaces as RegistryError, not silent empty bytes."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing")

    with (
        _client_with(handler) as client,
        pytest.raises(RegistryError, match="404"),
    ):
        client.download_tarball("https://x.test/prism-reactjs-1.0.0.tgz")


def test_scoped_package_path_is_url_encoded() -> None:
    """Scoped names hit ``@scope%2fname/latest`` on the wire.

    httpx exposes ``request.url.path`` decoded for convenience, so we
    have to inspect ``raw_path`` (bytes including the ``%2f``) to assert
    on what actually goes out over the network.
    """
    captured_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.raw_path)
        return httpx.Response(
            200,
            content=b'{"name":"x","version":"1.0.0","dist":{"tarball":"u"}}',
        )

    with _client_with(handler) as client:
        client.get_latest_manifest("@nutanix-ui/prism-reactjs")

    assert any(b"%2fprism-reactjs/latest" in p for p in captured_paths), (
        captured_paths
    )
