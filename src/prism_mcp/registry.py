"""Minimal npm-registry client against Artifactory.

Two endpoints are implemented:

* ``GET <base>/<package>/latest`` — the *manifest of the latest
  published version* (just ``name`` / ``version`` / ``dist`` / etc.,
  not the full version history). For packages with hundreds of
  published versions (Prism on Canaveral Artifactory has 250+), the
  full registry document at ``GET <base>/<package>`` weighs >40MB and
  takes minutes to stream; the per-version manifest is a few KB. We
  use ETag short-circuiting on this manifest so daily polls 304 when
  the latest version hasn't changed (PRD section 6 / Slice 7).
* ``GET <tarball_url>`` — the tarball binary stream. URL comes from
  the manifest's ``dist.tarball``.

The class is intentionally a thin wrapper around ``httpx.Client`` rather
than the async client, because every caller in v1 is synchronous and
``mcp`` runs us under ``anyio.run`` from a sync entrypoint anyway. We
inject the transport so tests can use ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Connect / read / write timeouts (seconds). The per-version manifest
# at ``/<pkg>/latest`` is a few KB and finishes in well under a second,
# so the read timeout primarily covers the tarball download (Prism's
# tarball is large; we keep the budget generous). Connect / write stay
# short because those are headers and control flow, not data.
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 120.0
DEFAULT_WRITE_TIMEOUT_S = 10.0
DEFAULT_POOL_TIMEOUT_S = 10.0
DEFAULT_USER_AGENT = "prism-mcp/0.1 (+https://github.com/nutanix)"


class RegistryError(RuntimeError):
    """Raised for non-success HTTP responses we can't recover from.

    Also raised by the client when ``httpx`` reports a transport-level
    failure (DNS, connection refused, timeout, ...). Wrapping these as
    a single error type lets :class:`prism_mcp.library.Library` apply
    one offline-fallback strategy for everything that isn't a 200 or
    304 — see PRD section 6 (offline degraded mode) and Slice 8.
    """


@dataclass(frozen=True)
class ManifestResult:
    """Outcome of a ``/<pkg>/latest`` fetch.

    Args:
        manifest (dict | None): parsed per-version manifest JSON, or
            ``None`` when the server returned ``304 Not Modified`` and
            the caller can reuse cached state. The manifest is the
            same shape as one entry in ``versions["<v>"]`` of the full
            registry doc; we read ``name``, ``version`` and ``dist``.
        etag (str | None): ``ETag`` header from the response, if any.
        not_modified (bool): ``True`` if we got a ``304``.
    """

    manifest: dict[str, Any] | None
    etag: str | None
    not_modified: bool


class RegistryClient:
    """Synchronous npm-registry client with ETag awareness.

    Args:
        base_url (str): registry root, slash-terminated.
        auth_header (str | None): pre-built ``Authorization`` header
            value, or ``None`` to make unauthenticated requests (works
            against fixture servers in tests; will fail with 401 against
            real Artifactory).
        timeout (float): per-request timeout in seconds.
        transport (httpx.BaseTransport | None): override transport,
            used by tests to inject ``httpx.MockTransport``.
        verify (bool | Path | str): forwarded verbatim to
            ``httpx.Client(verify=...)``. ``True`` (the default) uses
            certifi's CA bundle; a path points at a PEM file with one
            or more roots (the right move when the server presents an
            internal corporate CA); ``False`` disables verification
            entirely (escape hatch — see
            :attr:`prism_mcp.config.ServerConfig.insecure_tls`).
    """

    def __init__(
        self,
        base_url: str,
        auth_header: str | None,
        timeout: float | httpx.Timeout | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: bool | str = True,
    ) -> None:
        self._base_url = base_url
        self._auth_header = auth_header
        headers = {
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if auth_header:
            headers["Authorization"] = auth_header
        if verify is False:
            logger.warning(
                "TLS verification disabled for %s; this is an "
                "internal-only escape hatch — do not use against "
                "untrusted endpoints",
                base_url,
            )
        if timeout is None:
            timeout = httpx.Timeout(
                connect=DEFAULT_CONNECT_TIMEOUT_S,
                read=DEFAULT_READ_TIMEOUT_S,
                write=DEFAULT_WRITE_TIMEOUT_S,
                pool=DEFAULT_POOL_TIMEOUT_S,
            )
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            verify=verify,
        )

    def close(self) -> None:
        """Close the underlying HTTP client connections."""
        self._client.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_latest_manifest(
        self,
        package_name: str,
        etag: str | None = None,
    ) -> ManifestResult:
        """Fetch the latest-version manifest for ``package_name``.

        Hits ``<base>/<pkg>/latest`` — the npm registry shortcut that
        resolves ``dist-tags.latest`` server-side and returns just that
        one version's manifest (a few KB). We deliberately avoid the
        full ``<base>/<pkg>`` document because for popular packages it
        can be tens of MB.

        Args:
            package_name (str): scoped or unscoped name; sent URL-encoded.
            etag (str | None): prior ``ETag`` value to short-circuit via
                ``If-None-Match``.

        Returns:
            ManifestResult: manifest + new ETag, or a 304 sentinel.

        Raises:
            RegistryError: for any non-200/304 response, or for
                ``httpx.RequestError`` transport failures (DNS,
                connection refused, TLS, timeout, ...).
        """
        request_headers: dict[str, str] = {}
        if etag:
            request_headers["If-None-Match"] = etag

        url_path = f"{_encode_package_path(package_name)}/latest"
        logger.info(
            "fetching registry latest manifest package=%s etag=%s",
            package_name,
            etag,
        )
        try:
            response = self._client.get(url_path, headers=request_headers)
        except httpx.RequestError as exc:
            raise RegistryError(
                f"registry transport error fetching {package_name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 304:
            logger.info(
                "registry manifest not modified package=%s", package_name
            )
            return ManifestResult(
                manifest=None,
                etag=etag,
                not_modified=True,
            )

        if response.status_code != 200:
            raise RegistryError(
                f"registry manifest fetch failed: "
                f"{response.status_code} {response.reason_phrase} "
                f"for {package_name}"
            )

        return ManifestResult(
            manifest=response.json(),
            etag=response.headers.get("ETag"),
            not_modified=False,
        )

    def download_tarball(self, tarball_url: str) -> bytes:
        """Download the tarball at ``tarball_url``.

        Args:
            tarball_url (str): fully-qualified URL from
                ``dist.tarball``. Absolute on real Artifactory; we don't
                resolve relative URLs.

        Returns:
            bytes: raw tarball payload.

        Raises:
            RegistryError: for any non-200 response.
        """
        logger.info("downloading tarball url=%s", tarball_url)
        try:
            response = self._client.get(tarball_url)
        except httpx.RequestError as exc:
            raise RegistryError(
                f"tarball transport error fetching {tarball_url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise RegistryError(
                f"tarball download failed: "
                f"{response.status_code} {response.reason_phrase} "
                f"for {tarball_url}"
            )
        return response.content


def _encode_package_path(package_name: str) -> str:
    """URL-encode a scoped package name.

    npm registries accept ``@scope%2fname`` as the path. We don't pull
    in ``urllib.parse.quote`` because the only character we ever need
    to escape is ``/``, and bare-encoding keeps logs grep-able.

    Args:
        package_name (str): e.g. ``"@nutanix-ui/prism-reactjs"``.

    Returns:
        str: path-safe form, e.g. ``"@nutanix-ui%2fprism-reactjs"``.
    """
    return package_name.replace("/", "%2f")
