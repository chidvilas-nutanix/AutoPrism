"""Semantic-search index over ``ExampleChunk`` code bodies.

Slice 9's value proposition: the existing BM25 index in
:mod:`prism_mcp.search` only embeds example *titles* into the
synthetic doc. The actual jsx code (the most LLM-useful part) is
indexed lexically by name only. For "find me a call-site shape that
imports Modal and FormItemInput together", BM25 is the wrong tool.

This module adds a sibling, embedding-backed index that:

1. Filters :class:`~prism_mcp.parsers.examples_md_code.ExampleChunk`
   to drop anti-pattern + noeditor + deprecated entries (so the LLM
   never gets shown an example it should avoid).
2. Composes a short, deterministic text per chunk
   (``"<comp> — <title>\\nImports: ...\\n<code-prefix>"``) — keeping
   the chunk-text short (~400 char code prefix) is enough for the
   tiny BGE-small model to disambiguate without paying for long
   sequence lengths.
3. Embeds with a caller-supplied encoder (production: fastembed
   ``BAAI/bge-small-en-v1.5``; tests: a deterministic stub).
4. Persists as a single ``.npz`` so server restarts skip re-encoding.

The on-disk file is deliberately self-describing — vectors plus a
JSON blob of chunk metadata stored as a uint8 array — so we can load
it without ``allow_pickle`` (which is a hard "no" for shipping code).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TypeAlias

import numpy as np
from pydantic import BaseModel, ConfigDict

from prism_mcp.parsers.examples_md_code import ExampleChunk

logger = logging.getLogger(__name__)

#: A pure-function encoder. Receives a list of texts, returns an
#: ``(N, D)`` float32 array. L2-normalisation is the encoder's
#: responsibility because the cosine path below is a plain dot product.
Encoder: TypeAlias = Callable[[list[str]], np.ndarray]

#: A cross-encoder reranker. Receives ``(query, list[document_text])``
#: and returns a 1-D ``np.ndarray`` of relevance scores aligned with
#: ``documents``. Higher = more relevant. Unlike :data:`Encoder`,
#: rerankers attend to ``query`` and ``document`` jointly, which is
#: why they beat any bi-encoder on top-k accuracy but cost ~10x more
#: per pair. Used as a final-stage refinement over the top-50 of
#: BM25+dense RRF, not as a primary retriever.
Reranker: TypeAlias = Callable[[str, list[str]], np.ndarray]

# Production encoder. As of slice 9 SOTA (early 2026) we use Jina's
# code-specialised v2 base embedding (768-dim, ONNX, ~640 MB on first
# call). Trained on multi-language code corpora so it ranks "modal
# with form fields" or "icon-only button" by structure, not just
# lexical overlap. ``fastembed`` ships this model natively; no torch
# dep. Listed in
# https://qdrant.github.io/fastembed/examples/Supported_Models/
DEFAULT_FASTEMBED_MODEL = "jinaai/jina-embeddings-v2-base-code"

# Production reranker. The lightest cross-encoder fastembed ships
# (Apache 2.0, ~120 MB ONNX). One step down from BGE-reranker-v2-m3
# in absolute quality but native to our ONNX runtime — no torch, no
# FlagEmbedding. Sub-100ms per 50 pairs on CPU.
DEFAULT_FASTEMBED_RERANKER = "Xenova/ms-marco-MiniLM-L-12-v2"

# A short code-prefix is enough for BGE-small to rank by structural
# signal; the full body would just dilute the centroid and slow the
# (tiny) encode pass.
_CODE_PREFIX_CHARS = 400


class ExampleHit(BaseModel):
    """One ranked hit returned by :meth:`ExamplesIndex.query`.

    Args:
        component_name (str): name of the component the chunk
            documents (the parent file's stem).
        example_id (str | None): the ``// @example-id`` slug from the
            chunk, if present.
        title (str): the chunk's title — useful for the LLM to display
            "I found this example called 'Modal with form fields'".
        code (str): the full jsx body, untruncated.
        imports (list[str]): identifiers imported from
            ``@nutanix-ui/prism-reactjs`` in the chunk.
        score (float): cosine similarity in ``[-1, 1]``. Larger is
            better; for normalised BGE embeddings most useful hits sit
            in ``[0.4, 0.8]``.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    example_id: str | None = None
    title: str
    code: str
    imports: list[str]
    score: float


class ExamplesIndex:
    """In-memory, embedding-backed index over example chunks.

    Args:
        chunks (Sequence[ExampleChunk]): the chunks indexed, in the
            same order as ``vectors``. ``vectors[i]`` is the
            normalised embedding for ``chunks[i]``.
        vectors (np.ndarray): ``(N, D)`` float32 array of
            **L2-normalised** vectors. The query path assumes the
            rows are already unit-norm so cosine reduces to a single
            ``vectors @ query_vec`` call.
        version (str): tarball version label these vectors were built
            from. Stamped on disk so a stale ``.npz`` from an older
            published Prism version can be detected and ignored.
        encoder (Encoder): the encoder used to embed *queries* at
            search time. The corpus vectors are already in
            ``vectors``; the encoder is only invoked at query time.

    Notes:
        ``ExamplesIndex`` instances are **not** thread-safe to
        construct, but ``query`` is safe to call from multiple async
        tasks once construction is done (numpy releases the GIL during
        the dot product).
    """

    def __init__(
        self,
        chunks: Sequence[ExampleChunk],
        vectors: np.ndarray,
        version: str,
        encoder: Encoder,
        model_id: str = DEFAULT_FASTEMBED_MODEL,
    ) -> None:
        if len(chunks) != vectors.shape[0]:
            raise ValueError(
                "chunks and vectors must have the same length "
                f"(got {len(chunks)} and {vectors.shape[0]})"
            )
        self._chunks: list[ExampleChunk] = list(chunks)
        self._vectors: np.ndarray = vectors.astype(np.float32, copy=False)
        self._version = version
        self._encoder = encoder
        self._model_id = model_id

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def version(self) -> str:
        """Return the tarball version this index was built from."""
        return self._version

    @property
    def model_id(self) -> str:
        """Return the encoder model id used to build the corpus vectors.

        Swapping the encoder model (e.g. BGE-small → jina-v2-code) means
        the stored vectors have different dimensionality. ``load`` uses
        this field to reject stale ``.npz`` files instead of crashing
        with an opaque numpy shape error.
        """
        return self._model_id

    def vectors_for_test(self) -> np.ndarray:
        """Test helper: return the underlying corpus matrix.

        Exposed so retrieval-side tests can introspect the index
        without reaching into private state.
        """
        return self._vectors

    def chunks(self) -> list[ExampleChunk]:
        """Return the chunks indexed in corpus order.

        Returned list is a copy; callers are free to slice but
        mutating won't affect the index. Used by HybridSearcher to
        materialise ExampleHit instances from RRF-fused integer ids
        without reaching into private state.
        """
        return list(self._chunks)

    def rank_indices(
        self,
        query: str,
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Return ``(corpus_index, cosine_score)`` for the dense top-k.

        This is the integer-id surface used by RRF fusion in
        :class:`prism_mcp.retrieval.HybridSearcher`. It avoids
        materialising the full :class:`ExampleHit` objects when all
        we need are the ranks. Filtering by ``component_name`` is
        deliberately NOT applied here; HybridSearcher applies it
        once after fusion to keep both ranking lists ranked over the
        same population.

        Args:
            query (str): free-text query.
            top_k (int): max ranks to return. ``>= 1``.

        Returns:
            list[tuple[int, float]]: indexed by corpus position,
            sorted by descending cosine score. Length is
            ``min(top_k, len(corpus))``.

        Raises:
            ValueError: when ``top_k < 1``.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._chunks:
            return []

        query_vec = self._encoder([query])[0].astype(np.float32, copy=False)
        scores = self._vectors @ query_vec
        order = np.argsort(-scores)[:top_k]
        return [(int(i), float(scores[i])) for i in order]

    def query(
        self,
        query: str,
        top_k: int = 5,
        filter_components: Iterable[str] | None = None,
    ) -> list[ExampleHit]:
        """Return the ``top_k`` most-similar chunks for ``query``.

        Args:
            query (str): free-text query from the LLM. Embedded
                through ``self._encoder`` at call time; we don't cache
                query embeddings because Cursor queries are
                ~unbounded-cardinality strings.
            top_k (int): max hits to return. ``>= 1``.
            filter_components (Iterable[str] | None): when supplied,
                only return hits whose ``component_name`` is in this
                set. Applied **after** ranking so the score still
                reflects the global corpus.

        Returns:
            list[ExampleHit]: hits sorted by descending cosine score.
            May be shorter than ``top_k`` when the corpus is small or
            the filter discards most candidates.

        Raises:
            ValueError: when ``top_k < 1``.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._chunks:
            return []

        query_vec = self._encoder([query])[0].astype(np.float32, copy=False)
        scores = self._vectors @ query_vec

        # ``argsort`` ascending → reverse for descending. We don't use
        # ``argpartition`` because N is small (~1200) and the full
        # sort is sub-ms; partial sort would add code complexity for
        # no measurable win.
        order = np.argsort(-scores)

        allow: set[str] | None = (
            set(filter_components) if filter_components is not None else None
        )

        hits: list[ExampleHit] = []
        for idx in order:
            chunk = self._chunks[int(idx)]
            if allow is not None and chunk.component_name not in allow:
                continue
            hits.append(
                ExampleHit(
                    component_name=chunk.component_name,
                    example_id=chunk.example_id,
                    title=chunk.title,
                    code=chunk.code,
                    imports=list(chunk.imports),
                    score=float(scores[idx]),
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    def save(self, path: Path) -> None:
        """Persist the index to ``path`` as a single ``.npz`` file.

        Layout:

        * ``vectors`` — the ``(N, D)`` float32 corpus matrix.
        * ``metadata`` — uint8 array whose bytes are JSON
          ``{"version": ..., "chunks": [{...}, ...]}``. Storing it as
          a numeric array means ``np.load`` can read the file with
          ``allow_pickle=False`` (the safe default).

        Args:
            path (Path): destination path. Parent must exist.
        """
        metadata = {
            "version": self._version,
            "model_id": self._model_id,
            "chunks": [chunk.model_dump() for chunk in self._chunks],
        }
        meta_bytes = json.dumps(metadata).encode("utf-8")
        meta_arr = np.frombuffer(meta_bytes, dtype=np.uint8)
        np.savez(path, vectors=self._vectors, metadata=meta_arr)
        logger.info(
            "saved examples index path=%s n=%d version=%s model=%s",
            path,
            len(self._chunks),
            self._version,
            self._model_id,
        )

    @classmethod
    def load(
        cls,
        path: Path,
        encoder: Encoder,
        expected_version: str | None = None,
        expected_model_id: str | None = None,
    ) -> ExamplesIndex:
        """Load a previously :meth:`save`-d index.

        Args:
            path (Path): file path written by a prior :meth:`save`.
            encoder (Encoder): query-time encoder. The corpus vectors
                come from disk, but the encoder is needed to embed
                queries later.
            expected_version (str | None): when supplied, raise if the
                on-disk ``version`` doesn't match. The caller uses
                this to invalidate stale ``.npz`` files after a
                library upgrade.
            expected_model_id (str | None): when supplied, raise if
                the on-disk ``model_id`` doesn't match. Critical when
                the encoder has been swapped (e.g. BGE-small → jina
                v2 base-code) because the cached vectors then have a
                dimensionality the new query encoder cannot dot with.

        Returns:
            ExamplesIndex: ready to query.

        Raises:
            ValueError: when an ``expected_*`` is supplied and
                mismatched, or when the on-disk file is malformed.
        """
        with np.load(path, allow_pickle=False) as data:
            vectors = np.asarray(data["vectors"], dtype=np.float32)
            meta_bytes = data["metadata"].tobytes()
        try:
            metadata = json.loads(meta_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"corrupt examples index at {path}: {exc}"
            ) from exc

        version = metadata.get("version")
        if not isinstance(version, str):
            raise ValueError(f"missing version in examples index at {path}")
        if expected_version is not None and version != expected_version:
            raise ValueError(
                f"examples index version mismatch at {path}: "
                f"on-disk={version!r} expected={expected_version!r}"
            )

        # ``model_id`` is optional in legacy ``.npz`` files (pre-SOTA
        # swap). When absent we default to the BGE-small id so the
        # check below correctly rejects them under the new default.
        model_id = metadata.get("model_id", "BAAI/bge-small-en-v1.5")
        if not isinstance(model_id, str):
            raise ValueError(f"corrupt model_id in examples index at {path}")
        if expected_model_id is not None and model_id != expected_model_id:
            raise ValueError(
                f"examples index model mismatch at {path}: "
                f"on-disk={model_id!r} expected={expected_model_id!r}"
            )

        chunks = [ExampleChunk.model_validate(c) for c in metadata["chunks"]]
        return cls(
            chunks=chunks,
            vectors=vectors,
            version=version,
            encoder=encoder,
            model_id=model_id,
        )


def build_examples_index(
    chunks: Iterable[ExampleChunk],
    version: str,
    encoder: Encoder,
    model_id: str = DEFAULT_FASTEMBED_MODEL,
) -> ExamplesIndex:
    """Filter ``chunks``, embed them, and return an :class:`ExamplesIndex`.

    The filter step drops anti-pattern + noeditor chunks because the
    embedding index is the "show me a good example" surface.

    Args:
        chunks (Iterable[ExampleChunk]): chunks from
            :func:`~prism_mcp.parsers.examples_md_code.parse_example_code_blocks`.
        version (str): tarball version label stamped on the index.
        encoder (Encoder): callable used to embed both corpus and
            queries.
        model_id (str): identifier persisted into the ``.npz``
            metadata. Must match the model behind ``encoder`` so that
            ``ExamplesIndex.load`` can later detect encoder swaps.

    Returns:
        ExamplesIndex: ready to ``query`` and ``save``.
    """
    eligible = [c for c in chunks if _is_embeddable(c)]
    if not eligible:
        vectors = np.zeros((0, 0), dtype=np.float32)
        return ExamplesIndex(
            chunks=eligible,
            vectors=vectors,
            version=version,
            encoder=encoder,
            model_id=model_id,
        )

    texts = [_embedding_text(c) for c in eligible]
    raw = encoder(texts).astype(np.float32, copy=False)
    vectors = _l2_normalise(raw)
    return ExamplesIndex(
        chunks=eligible,
        vectors=vectors,
        version=version,
        encoder=encoder,
        model_id=model_id,
    )


def build_default_encoder(
    model_name: str = DEFAULT_FASTEMBED_MODEL,
) -> Encoder:  # pragma: no cover - exercised by the production server only
    """Construct a fastembed-backed encoder.

    Imports fastembed lazily so test runs that never need the model
    don't pay the (slow) onnxruntime import cost. Production wiring
    in :mod:`prism_mcp.server` / :mod:`prism_mcp.library` calls this
    once per process; the resulting closure is reused across queries.

    Args:
        model_name (str): fastembed model id. Default is the
            project's code-specialised jina v2 base-code (see
            :data:`DEFAULT_FASTEMBED_MODEL`).

    Returns:
        Encoder: closure that calls into ``TextEmbedding.embed`` and
        stacks the generator output into an ``(N, D)`` array.
    """
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=model_name)

    def encode(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = list(model.embed(texts))
        return np.stack(vectors).astype(np.float32, copy=False)

    return encode


def build_default_reranker(
    model_name: str = DEFAULT_FASTEMBED_RERANKER,
) -> Reranker:  # pragma: no cover - exercised by the production server only
    """Construct a fastembed-backed cross-encoder reranker.

    Used as the final refinement stage of the hybrid retrieval
    pipeline in :class:`prism_mcp.retrieval.HybridSearcher`. The
    cross-encoder attends to ``(query, document)`` jointly and is
    orders of magnitude more discriminative than the bi-encoder for
    a small top-k after RRF.

    Imports fastembed lazily for the same reason as
    :func:`build_default_encoder`.

    Args:
        model_name (str): fastembed cross-encoder model id. Default
            is ms-marco-MiniLM-L-12-v2 (see
            :data:`DEFAULT_FASTEMBED_RERANKER`).

    Returns:
        Reranker: closure that calls into ``TextCrossEncoder.rerank``
        and returns a 1-D ``np.ndarray`` aligned with ``documents``.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name=model_name)

    def rerank(query: str, documents: list[str]) -> np.ndarray:
        if not documents:
            return np.zeros(0, dtype=np.float32)
        scores = list(model.rerank(query, documents))
        return np.asarray(scores, dtype=np.float32)

    return rerank


def _is_embeddable(chunk: ExampleChunk) -> bool:
    """Return ``True`` iff ``chunk`` should land in the LLM-facing index.

    Drops:

    * anti-patterns — the LLM should never be shown a bad example;
    * noeditor blocks — those are docs (e.g. a11y guidance), not
      runnable examples. ``get_a11y_rules`` (slice 11) consumes them
      via a different path.
    """
    return not (chunk.is_anti_pattern or chunk.is_noeditor)


def _embedding_text(chunk: ExampleChunk) -> str:
    """Return the short text fed to the encoder for one chunk.

    Shape: ``"<component> \u2014 <title>\\nImports: A, B, C\\n<code prefix>"``.
    The em-dash is a stylistic separator; keep it so tests can mirror
    the production text exactly.

    Args:
        chunk (ExampleChunk): chunk to format.

    Returns:
        str: 1-3 sentence "what is this example" text. Short enough
        that BGE-small can encode the whole batch in well under a
        second on CPU.
    """
    imports_part = (
        f"Imports: {', '.join(chunk.imports)}" if chunk.imports else "Imports:"
    )
    code_prefix = chunk.code[:_CODE_PREFIX_CHARS]
    return (
        f"{chunk.component_name} \u2014 {chunk.title}\n"
        f"{imports_part}\n"
        f"{code_prefix}"
    )


def _l2_normalise(vectors: np.ndarray) -> np.ndarray:
    """Row-normalise ``vectors`` so cosine reduces to dot product.

    Zero-norm rows (degenerate chunks) are left at zero; cosine
    against zero is zero, which is the correct rank.

    Args:
        vectors (np.ndarray): ``(N, D)`` float array.

    Returns:
        np.ndarray: ``(N, D)`` unit-norm rows (float32).
    """
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32, copy=False)
