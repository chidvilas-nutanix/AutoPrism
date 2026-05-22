"""Top-level orchestrator for tarball acquisition + state.

This is the only module the tool layer needs to talk to. It owns:

* an in-process ETag (so repeated polls on the same process short-
  circuit cleanly);
* the cache instance;
* the resolved :class:`LibraryMeta` returned by ``get_library_meta``.

A small surface keeps Slice 2 focused: a single ``acquire_latest()``
call performs the whole "fetch metadata, decide whether to download,
verify, extract, update state" workflow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prism_mcp.cache import Cache
from prism_mcp.config import ServerConfig
from prism_mcp.indexer import Index, build_index
from prism_mcp.integrity import IntegrityError, verify
from prism_mcp.registry import RegistryClient, RegistryError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a single :meth:`Library.refresh` cycle.

    The background refresh loop and tests both use this to distinguish
    the three states the PRD calls out for Slice 7:

    * ``not_modified=True`` — registry returned ``304``; cached state
      reused, no swap. The PRD demo's "no-op on 304" path.
    * ``swapped=True`` — a new version was published; the in-memory
      :class:`~prism_mcp.indexer.Index` was rebuilt. The PRD demo's
      "fake new version" path.
    * ``offline=True`` — registry was unreachable; if a cache exists
      we fall back to it (loaded only if not already loaded), else the
      refresh raises :class:`LibraryError`.

    Args:
        version_before (str | None): in-process version prior to this
            cycle. ``None`` on cold start.
        version_after (str): in-process version after this cycle.
        swapped (bool): ``True`` iff the active index was rebuilt.
        not_modified (bool): ``True`` iff registry returned 304.
        offline (bool): ``True`` iff registry was unreachable AND the
            cycle resolved from cache.
    """

    version_before: str | None
    version_after: str
    swapped: bool
    not_modified: bool
    offline: bool


@dataclass(frozen=True)
class LibraryMeta:
    """Public ``get_library_meta`` shape.

    Args:
        package_name (str): scoped npm package.
        version (str): resolved ``dist-tags.latest``.
        last_indexed_at (str): RFC 3339 UTC timestamp.
        source_url (str): tarball URL used to populate the cache.
        cache_path (str): on-disk path of the extracted ``package/``
            directory.
        from_cache (bool): ``True`` when this state was loaded from the
            on-disk cache without a successful registry fetch (offline
            degraded mode lite — Slice 8 will firm this up).
    """

    package_name: str
    version: str
    last_indexed_at: str
    source_url: str
    cache_path: str
    from_cache: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view for the MCP tool layer."""
        return {
            "package_name": self.package_name,
            "version": self.version,
            "last_indexed_at": self.last_indexed_at,
            "source_url": self.source_url,
            "cache_path": self.cache_path,
            "from_cache": self.from_cache,
        }


class LibraryError(RuntimeError):
    """Raised when we cannot produce any usable library state."""


class Library:
    """In-process owner of the Prism library acquisition workflow.

    Args:
        config (ServerConfig): resolved server config.
        registry (RegistryClient): registry client to use.
        cache (Cache): cache instance to populate.
    """

    def __init__(
        self,
        config: ServerConfig,
        registry: RegistryClient,
        cache: Cache,
    ) -> None:
        self._config = config
        self._registry = registry
        self._cache = cache
        self._etag: str | None = None
        self._meta: LibraryMeta | None = None
        self._index: Index | None = None

    @property
    def meta(self) -> LibraryMeta | None:
        """Return the last-resolved meta, if any."""
        return self._meta

    def index(self) -> Index:
        """Return the entity index, acquiring the library if needed.

        Returns:
            Index: built lazily on first access; rebuilt whenever
            :meth:`acquire_latest` swaps to a new version.

        Raises:
            LibraryError: when no library is available at all (cold
            start, registry down, empty cache).
        """
        if self._index is None:
            self.acquire_latest()
        if self._index is None:
            raise LibraryError(
                "index unavailable: library acquisition produced no "
                "package directory"
            )
        return self._index

    def acquire_latest(self) -> LibraryMeta:
        """Resolve the latest published version and ensure it's cached.

        Workflow:

        1. Fetch metadata with ``If-None-Match`` if we already have an
           ETag. On ``304`` and existing in-process meta, return it.
        2. From ``dist-tags.latest`` pick the version; look up its
           ``dist`` entry for the tarball URL and integrity hint.
        3. If that version is already on disk, populate meta from cache
           and skip the download.
        4. Otherwise download, verify integrity, extract into cache,
           then populate meta.

        Returns:
            LibraryMeta: the resolved metadata.

        Raises:
            LibraryError: when the registry response is unusable AND no
                cache fallback is available. The error message includes
                a "connect to VPN / Artifactory" hint per Slice 8.
                Successful cache fallback surfaces as a
                ``from_cache=True`` :class:`LibraryMeta`.
        """
        try:
            return self._acquire_online()
        except (RegistryError, IntegrityError) as exc:
            logger.warning(
                "registry acquisition failed: %s; checking cache",
                exc,
            )
            cached = self._meta_from_cache(source_url="(offline)")
            if cached is not None:
                logger.warning(
                    "serving cached library version=%s in degraded "
                    "mode; registry was unreachable: %s",
                    cached.version,
                    exc,
                )
                self._adopt_meta(cached)
                return cached
            raise LibraryError(
                "could not acquire library and no cache is populated. "
                "Connect to the Nutanix VPN (or set "
                "JFROG_EMAIL/JFROG_API_KEY) so we can reach "
                f"Artifactory at least once. Underlying error: {exc}"
            ) from exc

    def refresh(self) -> RefreshOutcome:
        """Run one refresh cycle and report what changed.

        This is the entrypoint the background loop in
        :mod:`prism_mcp.refresh` calls every interval, and the demo
        path the PRD's Slice 7 talks about. It composes
        :meth:`acquire_latest` (which already handles ETag /
        cache-hit / offline-fallback) with a small bit of state
        tracking so callers can tell the three Slice-7 cases apart.

        Returns:
            RefreshOutcome: structured description of this cycle.

        Raises:
            LibraryError: same conditions as :meth:`acquire_latest` —
                only when no cache fallback is available.
        """
        version_before = self._meta.version if self._meta else None
        etag_before = self._etag

        meta = self.acquire_latest()
        version_after = meta.version

        not_modified = (
            etag_before is not None
            and self._etag == etag_before
            and version_before == version_after
            and not meta.from_cache
        )
        return RefreshOutcome(
            version_before=version_before,
            version_after=version_after,
            swapped=(
                version_before is not None and version_before != version_after
            )
            or (version_before is None and not not_modified),
            not_modified=not_modified,
            offline=meta.from_cache,
        )

    def _acquire_online(self) -> LibraryMeta:
        """Run the happy path against the registry."""
        result = self._registry.get_latest_manifest(
            self._config.package_name,
            etag=self._etag,
        )

        if result.not_modified and self._meta is not None:
            logger.info("registry returned 304; reusing in-process meta")
            return self._meta

        if result.manifest is None:
            cached = self._meta_from_cache(source_url="(304 cold start)")
            if cached is not None:
                self._adopt_meta(cached)
                if result.etag:
                    self._etag = result.etag
                return cached
            raise LibraryError(
                "registry returned 304 but no cache is populated; "
                "remove ETag override or warm the cache first"
            )

        manifest = result.manifest
        version, dist = _extract_version_and_dist(manifest)
        tarball_url = str(dist["tarball"])
        integrity = dist.get("integrity")
        shasum = dist.get("shasum")

        if self._cache.is_version_cached(version):
            logger.info(
                "version already on disk; skipping download version=%s",
                version,
            )
            package_dir = self._cache.package_dir(version)
            meta = self._build_meta(
                version=version,
                tarball_url=tarball_url,
                package_dir=package_dir,
                from_cache=False,
            )
        else:
            tarball_bytes = self._registry.download_tarball(tarball_url)
            verify(tarball_bytes, integrity=integrity, shasum=shasum)
            package_dir = self._cache.install_tarball(version, tarball_bytes)
            meta = self._build_meta(
                version=version,
                tarball_url=tarball_url,
                package_dir=package_dir,
                from_cache=False,
            )

        if result.etag:
            self._etag = result.etag
        self._adopt_meta(meta)
        return meta

    def _adopt_meta(self, meta: LibraryMeta) -> None:
        """Store ``meta`` and atomically swap the index on a version move.

        We build the new index into a local variable first, then publish
        both ``self._meta`` and ``self._index`` in two atomic CPython
        assignments. That way an in-flight tool call that's already
        called :meth:`index` keeps using its captured reference (stable
        snapshot), and the next tool call observes the new meta + new
        index together — never a torn pair. This is the "atomic-swap"
        behavior PRD Slice 7 requires.

        Args:
            meta (LibraryMeta): newly resolved meta. The index is
                rebuilt iff the version differs from the current one.
        """
        needs_rebuild = (
            self._index is None or self._index.version != meta.version
        )
        new_index = (
            build_index(Path(meta.cache_path), meta.version)
            if needs_rebuild
            else self._index
        )
        self._meta = meta
        self._index = new_index

    def _meta_from_cache(self, source_url: str) -> LibraryMeta | None:
        """Build a :class:`LibraryMeta` from on-disk state, if any."""
        version = self._cache.latest_cached_version()
        if version is None:
            return None
        package_dir = self._cache.package_dir(version)
        return self._build_meta(
            version=version,
            tarball_url=source_url,
            package_dir=package_dir,
            from_cache=True,
        )

    def _build_meta(
        self,
        version: str,
        tarball_url: str,
        package_dir: Path,
        from_cache: bool,
    ) -> LibraryMeta:
        """Compose a :class:`LibraryMeta` value."""
        return LibraryMeta(
            package_name=self._config.package_name,
            version=version,
            last_indexed_at=_utc_now_iso(),
            source_url=tarball_url,
            cache_path=str(package_dir),
            from_cache=from_cache,
        )


def _extract_version_and_dist(
    manifest: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Extract ``(version, dist)`` from a per-version manifest.

    The npm ``/<pkg>/latest`` endpoint already resolved
    ``dist-tags.latest`` for us, so the response is one version's
    manifest — we just need to read ``version`` and ``dist`` (the
    tarball URL + integrity hint).

    Args:
        manifest (dict): manifest body from the registry.

    Returns:
        tuple[str, dict]: version string and its ``dist`` block.

    Raises:
        LibraryError: when required fields are missing or malformed.
    """
    version = manifest.get("version")
    if not isinstance(version, str):
        raise LibraryError("registry manifest is missing 'version'")
    dist = manifest.get("dist")
    if not isinstance(dist, dict) or "tarball" not in dist:
        raise LibraryError(
            f"manifest for version {version!r} is missing dist.tarball"
        )
    return version, dist


def _utc_now_iso() -> str:
    """Return ``datetime.now(UTC)`` as a stable RFC 3339 string.

    Returns:
        str: ``YYYY-MM-DDTHH:MM:SS+00:00``.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")
