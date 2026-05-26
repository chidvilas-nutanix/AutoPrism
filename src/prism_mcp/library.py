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

from prism_mcp.a11y import A11yRules, build_a11y_rules
from prism_mcp.cache import Cache
from prism_mcp.config import ServerConfig
from prism_mcp.embeddings import (
    DEFAULT_FASTEMBED_MODEL,
    Encoder,
    ExamplesIndex,
    Reranker,
    build_default_encoder,
    build_default_reranker,
    build_examples_index,
)
from prism_mcp.graph import CompositionGraph, build_composition_graph
from prism_mcp.indexer import Index, build_index
from prism_mcp.integrity import IntegrityError, verify
from prism_mcp.parsers.examples_md_code import walk_example_chunks
from prism_mcp.registry import RegistryClient, RegistryError
from prism_mcp.retrieval import ChunkBM25, HybridSearcher
from prism_mcp.tokens_index import ColorTokenIndex, build_color_token_index

EXAMPLES_INDEX_FILENAME = "examples.embeddings.npz"

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
        encoder: Encoder | None = None,
        reranker: Reranker | None = None,
        encoder_model_id: str = DEFAULT_FASTEMBED_MODEL,
    ) -> None:
        """Construct a :class:`Library`.

        Args:
            config (ServerConfig): resolved server config.
            registry (RegistryClient): registry client to use.
            cache (Cache): cache instance to populate.
            encoder (Encoder | None): callable that embeds texts for
                the slice-9 examples index. Default ``None`` means
                lazily construct the fastembed-backed production
                encoder on first call to :meth:`examples_index`.
                Tests inject a deterministic stub.
            reranker (Reranker | None): cross-encoder reranker for
                the slice-9 hybrid pipeline. Default ``None`` means
                lazily construct the fastembed-backed
                ms-marco-MiniLM-L-12-v2 on first call to
                :meth:`hybrid_searcher`. Set explicitly to
                ``False`` …no — pass an injected stub in tests so the
                rerank stage is hermetic.
            encoder_model_id (str): identifier persisted into the
                ``.npz`` cache metadata. When the encoder is the
                production default this must match
                :data:`DEFAULT_FASTEMBED_MODEL` so a model swap
                invalidates stale caches. Tests pass their own id
                so cached files don't collide with production ones.
        """
        self._config = config
        self._registry = registry
        self._cache = cache
        self._encoder: Encoder | None = encoder
        self._reranker: Reranker | None = reranker
        self._encoder_model_id = encoder_model_id
        self._etag: str | None = None
        self._meta: LibraryMeta | None = None
        self._index: Index | None = None
        self._examples_index: ExamplesIndex | None = None
        self._example_chunk_searcher: ChunkBM25 | None = None
        self._hybrid_searcher: HybridSearcher | None = None
        self._color_token_index: ColorTokenIndex | None = None
        self._a11y_rules: A11yRules | None = None
        self._composition_graph: CompositionGraph | None = None

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

    def examples_index(self) -> ExamplesIndex:
        """Return the slice-9 :class:`ExamplesIndex`, building it lazily.

        Unlike :meth:`index` (which is built eagerly on cold start
        because every tool needs it), the examples index is built on
        first call to ``search_examples``. That keeps the cold-start
        VPN check (slice 8) on the fast path and defers the cost of
        loading fastembed's ONNX runtime to first actual use.

        The result is cached as an ``.npz`` file alongside the
        extracted tarball; subsequent process restarts on the same
        version skip the embed pass entirely.

        Returns:
            ExamplesIndex: ready to ``query``. Same instance returned
            on every call until :meth:`_adopt_meta` swaps versions.

        Raises:
            LibraryError: when no library is available (cold start
            without VPN / cache; same condition as :meth:`index`).
        """
        if self._examples_index is not None:
            return self._examples_index
        if self._meta is None:
            self.acquire_latest()
        if self._meta is None:  # pragma: no cover - acquire_latest raised
            raise LibraryError(
                "examples_index unavailable: library acquisition "
                "produced no metadata"
            )
        encoder = self._ensure_encoder()
        package_root = Path(self._meta.cache_path)
        cache_path = self._examples_index_path(self._meta.version)
        index = self._load_or_build_examples_index(
            package_root=package_root,
            version=self._meta.version,
            cache_path=cache_path,
            encoder=encoder,
        )
        self._examples_index = index
        return index

    def example_chunk_searcher(self) -> ChunkBM25:
        """Return the BM25 sibling-index over example chunks.

        Built lazily alongside :meth:`examples_index` so both share
        the same filtered chunk list (and therefore the same integer
        ids) needed by :class:`prism_mcp.retrieval.HybridSearcher`'s
        RRF fusion stage.
        """
        if self._example_chunk_searcher is not None:
            return self._example_chunk_searcher
        # examples_index() does the heavy lifting (acquire, build,
        # cache); we then build the BM25 sibling over the same
        # filtered chunks.
        embeddings = self.examples_index()
        searcher = ChunkBM25(chunks=embeddings.chunks())
        self._example_chunk_searcher = searcher
        return searcher

    def reranker(self) -> Reranker:
        """Return the cross-encoder reranker, lazily building default.

        Production wiring constructs the fastembed-backed
        ms-marco-MiniLM-L-12-v2 reranker on first call. Tests inject
        their own stub via the ``reranker`` constructor parameter.

        Returns:
            Reranker: closure mapping ``(query, list[doc])`` to a
            score array.
        """
        if self._reranker is None:
            logger.info("lazily constructing the default fastembed reranker")
            self._reranker = build_default_reranker()
        return self._reranker

    def hybrid_searcher(self) -> HybridSearcher:
        """Return the slice-9-SOTA hybrid retrieval pipeline.

        Composes the dense :class:`ExamplesIndex`, the BM25 sibling
        :class:`ChunkBM25`, and the cross-encoder
        :data:`~prism_mcp.embeddings.Reranker` behind a single
        :class:`HybridSearcher` instance. Same lifecycle as the other
        accessors: built lazily on first call, invalidated on version
        swap inside :meth:`_adopt_meta`.
        """
        if self._hybrid_searcher is not None:
            return self._hybrid_searcher
        embeddings = self.examples_index()
        bm25 = self.example_chunk_searcher()
        reranker = self.reranker()
        searcher = HybridSearcher(
            bm25=bm25, embeddings=embeddings, reranker=reranker
        )
        self._hybrid_searcher = searcher
        return searcher

    def color_token_index(self) -> ColorTokenIndex:
        """Return the slice-11 :class:`ColorTokenIndex`.

        Built lazily over :meth:`index`'s color-category token
        entities so we don't re-scan the LESS files; the slice-6
        token walker already did that. Same atomic-swap lifecycle
        as the other derived indices.
        """
        if self._color_token_index is not None:
            return self._color_token_index
        entities = self.index().all()
        version = self.index().version
        built = build_color_token_index(entities=entities, version=version)
        self._color_token_index = built
        return built

    def a11y_rules(self) -> A11yRules:
        """Return the slice-11 :class:`A11yRules` aggregation.

        Combines the global LLMS.md guidance with per-component
        a11y blocks extracted from ``*.examples.md`` chunks. Built
        lazily; invalidated on version swap.

        Returns:
            A11yRules: the aggregated rules. ``global_rules`` will
            be empty if the tarball doesn't ship LLMS.md;
            ``per_component`` will be empty if no chunk is flagged
            ``is_a11y_block``.
        """
        if self._a11y_rules is not None:
            return self._a11y_rules
        if self._meta is None:
            self.acquire_latest()
        if self._meta is None:  # pragma: no cover - acquire_latest raised
            raise LibraryError(
                "a11y_rules unavailable: library acquisition produced "
                "no metadata"
            )
        package_root = Path(self._meta.cache_path)
        chunks = list(walk_example_chunks(package_root))
        built = build_a11y_rules(package_root=package_root, chunks=chunks)
        self._a11y_rules = built
        return built

    def composition_graph(self) -> CompositionGraph:
        """Return the slice-10 :class:`CompositionGraph`.

        Built lazily over the same ``*.examples.md`` chunk corpus
        the hybrid searcher consumes — co-import edges weighted by
        co-occurrence count, then Louvain-partitioned for the
        cluster tool. Invalidated on version swap.

        Returns:
            CompositionGraph: the assembled wrapper with Louvain
            communities already computed. Empty graph when the
            tarball ships no example fences.
        """
        if self._composition_graph is not None:
            return self._composition_graph
        if self._meta is None:
            self.acquire_latest()
        if self._meta is None:  # pragma: no cover - acquire_latest raised
            raise LibraryError(
                "composition_graph unavailable: library acquisition "
                "produced no metadata"
            )
        package_root = Path(self._meta.cache_path)
        chunks = list(walk_example_chunks(package_root))
        built = build_composition_graph(
            chunks=chunks,
            version=self._meta.version,
        )
        self._composition_graph = built
        return built

    def _ensure_encoder(self) -> Encoder:
        """Return the cached encoder, lazily building the default one."""
        if self._encoder is None:
            logger.info("lazily constructing the default fastembed encoder")
            self._encoder = build_default_encoder()
        return self._encoder

    def _examples_index_path(self, version: str) -> Path:
        """Path of the ``.npz`` cache file for ``version``."""
        return self._cache.version_dir(version) / EXAMPLES_INDEX_FILENAME

    def _load_or_build_examples_index(
        self,
        *,
        package_root: Path,
        version: str,
        cache_path: Path,
        encoder: Encoder,
    ) -> ExamplesIndex:
        """Try to load ``cache_path``; on miss or corruption, rebuild.

        Build failures are not silently swallowed because they
        indicate either a corrupt cache (rebuild fixes it) or a
        genuine bug (which should surface). On a recoverable corrupt
        cache (including a model_id mismatch from an encoder swap)
        we log + rebuild; on a build failure we propagate.

        Args:
            package_root (Path): extracted tarball root.
            version (str): tarball version.
            cache_path (Path): where the ``.npz`` lives / will live.
            encoder (Encoder): query-time encoder.

        Returns:
            ExamplesIndex: either loaded from disk or freshly built.
        """
        if cache_path.is_file():
            try:
                return ExamplesIndex.load(
                    cache_path,
                    encoder=encoder,
                    expected_version=version,
                    expected_model_id=self._encoder_model_id,
                )
            except (ValueError, OSError) as exc:
                logger.warning(
                    "stale or corrupt examples index at %s: %s; rebuilding",
                    cache_path,
                    exc,
                )
        chunks = walk_example_chunks(package_root)
        built = build_examples_index(
            chunks=chunks,
            version=version,
            encoder=encoder,
            model_id=self._encoder_model_id,
        )
        try:
            built.save(cache_path)
        except OSError as exc:
            logger.warning(
                "failed to persist examples index to %s: %s",
                cache_path,
                exc,
            )
        return built

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
        # Invalidate every slice-9 derived index alongside the BM25
        # entity index so a torn read can't observe meta+index from
        # version N and any retrieval surface from version N-1. The
        # next call to ``examples_index`` / ``example_chunk_searcher``
        # / ``hybrid_searcher`` rebuilds (or loads from the new
        # version's ``.npz`` cache file). The reranker is corpus-
        # independent, so we *don't* invalidate it.
        if needs_rebuild:
            self._examples_index = None
            self._example_chunk_searcher = None
            self._hybrid_searcher = None
            # Slice 11 derived state — color tokens and a11y rules
            # are both built from the new tarball, so we must
            # invalidate alongside.
            self._color_token_index = None
            self._a11y_rules = None
            # Slice 10 derived state — composition graph + Louvain
            # communities are both pinned to a single tarball's
            # example corpus, so we invalidate alongside.
            self._composition_graph = None

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
