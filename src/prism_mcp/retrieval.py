"""Hybrid retrieval for the slice-9 ``search_examples`` tool.

The dense :class:`prism_mcp.embeddings.ExamplesIndex` is strong on
*semantic* queries ("modal that prompts for credentials") but blind to
exact identifier hits (``useFocusTrap``). BM25 is the opposite. Late
2025 / early 2026 production retrieval has converged on **Reciprocal
Rank Fusion** (Cormack et al. 2009) over both rankers as the canonical
combiner: it operates on *ranks*, not raw scores, so it sidesteps the
score-normalisation problem (BM25 returns IDF-weighted floats, cosine
returns [-1, 1]). See the production-RAG write-ups referenced in the
slice-9 SOTA discussion:

* https://dev.to/velsof/production-reranker-layer-for-rag-in-python-cross-encoder-cohere-fallback-and-reciprocal-rank-1a29
* https://www.kevinluzbetak.com/AI_ML/LLM_Engineering/Hybrid-Search-and-Reranking.html

After fusion we optionally apply a **cross-encoder reranker**
(:data:`~prism_mcp.embeddings.Reranker`) to the top-N candidates. The
reranker attends to ``(query, document)`` jointly — orders of magnitude
more discriminative than the bi-encoder for a small top-k, at the cost
of ~80 ms per 50 pairs on CPU.

The whole pipeline lives behind :class:`HybridSearcher` which is
constructed once per library version and reused across queries.

Pipeline shape::

    query
      ├── BM25 over example chunks    ── top-N ┐
      │                                        │
      │                                        ├── RRF fuse  ── top-N candidates
      │                                        │   (k=60)
      └── Dense (ExamplesIndex)        ── top-N ┘
                                                       │
                                                       ▼
                              (optional) cross-encoder rerank
                                                       │
                                                       ▼
                                              top-k :class:`ExampleHit`
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Hashable, Iterable, Sequence
from typing import TypeVar

from rank_bm25 import BM25Okapi

from prism_mcp.embeddings import ExampleHit, ExamplesIndex, Reranker
from prism_mcp.parsers.examples_md_code import ExampleChunk
from prism_mcp.search import _tokenize

# RRF is generic over any hashable id type. In production we fuse
# integer chunk indices; in tests we use strings for readability.
DocId = TypeVar("DocId", bound=Hashable)

logger = logging.getLogger(__name__)

# Canonical RRF smoothing constant from the original Cormack 2009
# paper. Production sweeps from k=10 to k=200 show <1% NDCG@10
# variation; don't tune.
DEFAULT_RRF_K = 60

# How many candidates to pull from each first-stage retriever before
# fusion. 50 is the universally-cited default; the corpus has ~1200
# chunks so we're recalling ~4% which is generous.
DEFAULT_CANDIDATE_POOL = 50


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[DocId]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[DocId, float]]:
    """Combine multiple ranked id-lists via Reciprocal Rank Fusion.

    For each id, the RRF score is ``Σ 1 / (k + rank_in_list)`` summed
    across every input list. Ids missing from a list contribute 0 from
    that list. Ranks are 1-based.

    The output is sorted high-to-low; ties are broken by *first-seen*
    order across the input lists for determinism (this works for any
    hashable id type, not just comparable ones).

    Args:
        rankings (Sequence[Sequence[DocId]]): one or more ranked lists
            of hashable document ids. Each inner list is sorted with
            the most-relevant id first.
        k (int): smoothing constant. Default 60.

    Returns:
        list[tuple[DocId, float]]: ``(id, fused_score)`` sorted by
        descending fused score, then by first-seen order.

    Raises:
        ValueError: when ``k <= 0`` (the formula degenerates) or when
            ``rankings`` is empty.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive (got {k})")
    if not rankings:
        raise ValueError("rankings must contain at least one ranked list")

    scores: dict[DocId, float] = defaultdict(float)
    first_seen: dict[DocId, int] = {}
    seq = 0
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
            if doc_id not in first_seen:
                first_seen[doc_id] = seq
                seq += 1

    return sorted(scores.items(), key=lambda p: (-p[1], first_seen[p[0]]))


class ChunkBM25:
    """BM25 over :class:`ExampleChunk` instances.

    Sibling to the entity-level :class:`prism_mcp.search.Searcher` —
    same tokenizer (camelCase split + suffix stripper) but indexes a
    per-chunk synthetic doc:

    ``<component> <title> <import1> <import2> ... <code_prefix>``

    The code prefix is bounded so BM25 IDF isn't dominated by JSX
    boilerplate. ``rank-bm25`` is the right tier at our scale (~1200
    chunks); switching to e.g. Tantivy would be over-engineering.

    Args:
        chunks (Iterable[ExampleChunk]): chunks to index, in corpus
            order. Indices returned by :meth:`search` refer to this
            ordering and must match :meth:`ExamplesIndex.chunks` for
            RRF fusion to be meaningful.
        code_prefix_chars (int): how much of ``chunk.code`` to fold
            into the doc. Default 300 captures import statements and
            the first JSX element without bloating the index.
    """

    def __init__(
        self,
        chunks: Iterable[ExampleChunk],
        code_prefix_chars: int = 300,
    ) -> None:
        self._chunks: list[ExampleChunk] = list(chunks)
        self._code_prefix_chars = code_prefix_chars
        self._doc_tokens: list[list[str]] = [
            _chunk_tokens(c, code_prefix_chars) for c in self._chunks
        ]
        # rank-bm25 requires a non-empty corpus; mirror the
        # entity-Searcher pattern.
        self._bm25: BM25Okapi | None = (
            BM25Okapi(self._doc_tokens) if self._doc_tokens else None
        )

    def __len__(self) -> int:
        return len(self._chunks)

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """Return ``(chunk_index, bm25_score)`` for the top-k matches.

        Filters out zero-overlap rows (BM25 IDF can pin at zero on
        tiny corpora; we want actual lexical overlap as the relevance
        signal).

        Args:
            query (str): free-text query.
            top_k (int): max results. ``>= 1``.

        Returns:
            list[tuple[int, float]]: indices into the corpus + scores,
            sorted high-to-low. Length is bounded by ``top_k`` and by
            the number of chunks with non-zero overlap.

        Raises:
            ValueError: when ``top_k < 1``.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if self._bm25 is None or not query.strip():
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        query_set = set(query_tokens)

        # Pair (idx, score) and sort high-to-low, stable on idx for
        # determinism.
        ranked = sorted(enumerate(scores), key=lambda p: (-p[1], p[0]))

        results: list[tuple[int, float]] = []
        for idx, score in ranked:
            doc_tokens = self._doc_tokens[idx]
            if not query_set & set(doc_tokens):
                continue
            results.append((int(idx), float(score)))
            if len(results) >= top_k:
                break
        return results


def _chunk_tokens(chunk: ExampleChunk, code_prefix_chars: int) -> list[str]:
    """Tokenize a chunk's BM25 synthetic doc.

    Args:
        chunk (ExampleChunk): chunk to tokenize.
        code_prefix_chars (int): max code prefix length.

    Returns:
        list[str]: tokens (lowercased, camelCase-split, suffix-stripped)
        ready for BM25.
    """
    parts = [
        chunk.component_name,
        chunk.title,
        " ".join(chunk.imports),
        chunk.code[:code_prefix_chars],
    ]
    return _tokenize(" ".join(parts))


class HybridSearcher:
    """RRF-fuse BM25 + dense, optionally cross-encoder rerank.

    Constructed once per library version (see
    :meth:`prism_mcp.library.Library.example_chunk_searcher` +
    :meth:`prism_mcp.library.Library.examples_index`). All three
    sub-components (BM25, dense index, reranker) share the same
    chunk-ordering — :class:`ChunkBM25` and :class:`ExamplesIndex`
    are both built from the same filtered chunk list so integer
    indices line up.

    Args:
        bm25 (ChunkBM25): lexical retriever over chunks.
        embeddings (ExamplesIndex): dense retriever over chunks.
        reranker (Reranker | None): optional cross-encoder reranker.
            ``None`` disables the rerank stage; callers can also pass
            ``use_reranker=False`` to :meth:`search` to bypass it
            per-query without rebuilding the searcher.
    """

    def __init__(
        self,
        bm25: ChunkBM25,
        embeddings: ExamplesIndex,
        reranker: Reranker | None = None,
    ) -> None:
        bm25_n = len(bm25)
        emb_n = len(embeddings)
        if bm25_n != emb_n:
            raise ValueError(
                "HybridSearcher requires aligned ChunkBM25 and ExamplesIndex "
                f"(got bm25 n={bm25_n} vs embeddings n={emb_n})"
            )
        self._bm25 = bm25
        self._embeddings = embeddings
        self._reranker = reranker
        # Cache the chunk list once — both sub-indices share the
        # ordering by construction (see Library wiring).
        self._chunks = embeddings.chunks()

    @property
    def has_reranker(self) -> bool:
        """Return ``True`` iff a reranker was injected."""
        return self._reranker is not None

    def search(
        self,
        query: str,
        top_k: int = 5,
        candidate_pool: int = DEFAULT_CANDIDATE_POOL,
        filter_components: Iterable[str] | None = None,
        use_reranker: bool = True,
        k: int = DEFAULT_RRF_K,
    ) -> list[ExampleHit]:
        """Run the full hybrid pipeline and return :class:`ExampleHit`.

        Args:
            query (str): free-text query from the LLM.
            top_k (int): final number of hits to return. ``>= 1``.
            candidate_pool (int): size of each first-stage retriever's
                top-N before fusion. Default 50.
            filter_components (Iterable[str] | None): when supplied,
                only return hits whose ``component_name`` is in this
                set. Applied **after** ranking + optional rerank so
                scores still reflect the global corpus.
            use_reranker (bool): when ``True`` and a reranker was
                injected, apply it to the fused top candidates. Cheap
                escape hatch for latency-sensitive batch calls.
            k (int): RRF smoothing constant. Default 60.

        Returns:
            list[ExampleHit]: hits sorted by descending final score
            (rerank score when applicable, RRF score otherwise).
            ``score`` on each hit is the score from the *final* stage
            that produced the ranking.

        Raises:
            ValueError: when ``top_k < 1``.
        """
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._chunks:
            return []

        bm25_hits = self._bm25.search(query, top_k=candidate_pool)
        dense_hits = self._embeddings.rank_indices(query, top_k=candidate_pool)

        bm25_ranking = [idx for idx, _ in bm25_hits]
        dense_ranking = [idx for idx, _ in dense_hits]

        # When both retrievers return nothing, bail early to avoid
        # an empty RRF call (which would raise).
        if not bm25_ranking and not dense_ranking:
            return []

        rankings: list[Sequence[int]] = []
        if bm25_ranking:
            rankings.append(bm25_ranking)
        if dense_ranking:
            rankings.append(dense_ranking)
        fused = reciprocal_rank_fusion(rankings, k=k)

        # Truncate to the rerank input width — rerankers are O(N) in
        # candidates, so feeding the full corpus is wasteful.
        fused = fused[:candidate_pool]

        # Optional rerank pass.
        if use_reranker and self._reranker is not None and fused:
            doc_indices = [idx for idx, _ in fused]
            doc_texts = [_rerank_doc_text(self._chunks[i]) for i in doc_indices]
            rerank_scores = self._reranker(query, doc_texts)
            if len(rerank_scores) != len(doc_indices):
                raise ValueError(
                    "Reranker returned mismatched score count "
                    f"(got {len(rerank_scores)} for {len(doc_indices)} docs)"
                )
            ranked = sorted(
                zip(doc_indices, rerank_scores, strict=True),
                key=lambda p: (-float(p[1]), p[0]),
            )
            scored = [(int(i), float(s)) for i, s in ranked]
        else:
            scored = [(idx, score) for idx, score in fused]

        allow: set[str] | None = (
            set(filter_components) if filter_components is not None else None
        )

        hits: list[ExampleHit] = []
        for idx, score in scored:
            chunk = self._chunks[idx]
            if allow is not None and chunk.component_name not in allow:
                continue
            hits.append(
                ExampleHit(
                    component_name=chunk.component_name,
                    example_id=chunk.example_id,
                    title=chunk.title,
                    code=chunk.code,
                    imports=list(chunk.imports),
                    score=score,
                )
            )
            if len(hits) >= top_k:
                break
        return hits


def _rerank_doc_text(chunk: ExampleChunk) -> str:
    """Format ``chunk`` for the cross-encoder reranker.

    The reranker sees the chunk as one short doc; we include the
    component name + title up-front because cross-encoders are
    sensitive to the start of each input (they tokenize jointly with
    the query and the leading tokens carry more attention mass).

    Args:
        chunk (ExampleChunk): chunk to format.

    Returns:
        str: a 1-3 sentence document text.
    """
    imports_part = (
        f"Imports: {', '.join(chunk.imports)}" if chunk.imports else "Imports:"
    )
    # 600 chars is a generous prefix that still fits comfortably in
    # the cross-encoder's 512-token context after tokenization.
    code_prefix = chunk.code[:600]
    return (
        f"{chunk.component_name} \u2014 {chunk.title}\n"
        f"{imports_part}\n"
        f"{code_prefix}"
    )
