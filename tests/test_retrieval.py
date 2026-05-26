"""Tests for the hybrid retrieval module.

Focus is on **pinning the contract** rather than enumerating every
edge case:

* :func:`reciprocal_rank_fusion` — three deterministic cases
  (single list passthrough, two-list fusion, missing-id contribution
  semantics).
* :class:`ChunkBM25` — one corpus, one query that exercises the
  camelCase + suffix-stripped tokenizer.
* :class:`HybridSearcher` — full pipeline with stub encoder + stub
  reranker so the assertions are hermetic and fast.

We deliberately do *not* re-test :class:`ExamplesIndex` here; that's
covered exhaustively in ``test_embeddings.py``.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from prism_mcp.embeddings import ExamplesIndex, build_examples_index
from prism_mcp.parsers.examples_md_code import ExampleChunk
from prism_mcp.retrieval import (
    ChunkBM25,
    HybridSearcher,
    reciprocal_rank_fusion,
)


def _stub_encoder(dim: int = 16):
    """Deterministic hash-based encoder mirroring test_embeddings.

    Each text is sha256-hashed, truncated to ``dim`` bytes, normalised.
    Identical inputs → identical vectors. Used so the dense ranker is
    deterministic without downloading a real model.
    """

    def encode(texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), dim), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = np.frombuffer(digest[:dim], dtype=np.uint8).astype(np.float32)
            norm = np.linalg.norm(raw)
            vectors[i] = raw / norm if norm > 0 else raw
        return vectors

    return encode


def _chunks(*specs: tuple[str, str, list[str], str]) -> list[ExampleChunk]:
    """Tiny factory: ``(component, title, imports, code)`` → ExampleChunks."""
    return [
        ExampleChunk(
            component_name=comp,
            title=title,
            code=code,
            language_tag="jsx",
            imports=imports,
            example_id=None,
        )
        for comp, title, imports, code in specs
    ]


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------


def test_rrf_single_list_preserves_order() -> None:
    """One ranked list in → same order out (scores monotonically decrease)."""
    out = reciprocal_rank_fusion([[10, 20, 30]], k=60)

    assert [doc_id for doc_id, _ in out] == [10, 20, 30]
    # 1/(60+1) > 1/(60+2) > 1/(60+3)
    scores = [s for _, s in out]
    assert scores[0] > scores[1] > scores[2]


def test_rrf_two_lists_boost_shared_docs() -> None:
    """Docs in both lists score higher than docs in only one.

    BM25 ranks: [A, B, C]. Dense ranks: [B, D, A]. Doc B is rank-1
    dense + rank-2 BM25, doc A is rank-1 BM25 + rank-3 dense. B
    should win the fusion: 1/61 + 1/62 vs A's 1/61 + 1/63.
    """
    fused = reciprocal_rank_fusion([["A", "B", "C"], ["B", "D", "A"]], k=60)

    ordered = [doc_id for doc_id, _ in fused]
    assert ordered[0] == "B"
    assert ordered[1] == "A"
    # Docs only in one list still appear.
    assert set(ordered) == {"A", "B", "C", "D"}


def test_rrf_canonical_scores_match_published_formula() -> None:
    """Spot-check the arithmetic against the published RRF formula.

    From the Cormack 2009 / 2026 production-RAG sources:
    a rank-1 doc contributes 1/(60+1) = 0.01639..., a rank-2 doc
    contributes 1/(60+2) = 0.01613... etc.
    """
    fused = reciprocal_rank_fusion([[1, 2]], k=60)

    assert fused[0][0] == 1
    assert fused[0][1] == pytest.approx(1.0 / 61, rel=1e-6)
    assert fused[1][0] == 2
    assert fused[1][1] == pytest.approx(1.0 / 62, rel=1e-6)


def test_rrf_rejects_non_positive_k() -> None:
    """k <= 0 makes the formula degenerate; that's a programmer error."""
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([[1, 2]], k=0)


def test_rrf_rejects_empty_rankings() -> None:
    """No retrievers in → nothing to fuse."""
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], k=60)


# ---------------------------------------------------------------------------
# ChunkBM25
# ---------------------------------------------------------------------------


def test_chunk_bm25_finds_identifier_match() -> None:
    """BM25 over chunks matches an identifier from imports + code.

    Pins that the tokenizer (camelCase split + suffix strip) actually
    reaches into ``imports`` and the code prefix — the exact use
    case that motivates fusing BM25 with dense embeddings.
    """
    chunks = _chunks(
        (
            "Modal",
            "Modal with form fields",
            ["Modal", "FormItemInput"],
            "<Modal><FormItemInput /></Modal>",
        ),
        ("Button", "Icon-only button", ["Button", "Icon"], "<Button />"),
        ("Alert", "Inline alert", ["Alert"], "<Alert />"),
    )
    bm25 = ChunkBM25(chunks=chunks)

    hits = bm25.search(query="FormItemInput", top_k=3)

    assert hits, "expected at least one hit for FormItemInput"
    assert hits[0][0] == 0, "expected Modal chunk (idx 0) to rank first"


def test_chunk_bm25_empty_corpus_returns_no_hits() -> None:
    """An empty corpus is valid; queries return ``[]``."""
    bm25 = ChunkBM25(chunks=[])

    assert bm25.search(query="anything", top_k=5) == []
    assert len(bm25) == 0


def test_chunk_bm25_blank_query_returns_no_hits() -> None:
    """A blank/whitespace query short-circuits before tokenization."""
    chunks = _chunks(("Modal", "t", ["Modal"], "<Modal />"))
    bm25 = ChunkBM25(chunks=chunks)

    assert bm25.search(query="   ", top_k=5) == []


def test_chunk_bm25_top_k_must_be_positive() -> None:
    """top_k <= 0 is a programmer error."""
    chunks = _chunks(("Modal", "t", ["Modal"], "<Modal />"))
    bm25 = ChunkBM25(chunks=chunks)

    with pytest.raises(ValueError):
        bm25.search(query="modal", top_k=0)


# ---------------------------------------------------------------------------
# HybridSearcher
# ---------------------------------------------------------------------------


def _build_pair(
    chunks: list[ExampleChunk],
) -> tuple[ChunkBM25, ExamplesIndex]:
    """Build an aligned (ChunkBM25, ExamplesIndex) pair for the same chunks.

    Both stages share corpus order — same input list, both
    constructors filter nothing — so RRF integer ids line up.
    """
    bm25 = ChunkBM25(chunks=chunks)
    index = build_examples_index(
        chunks=chunks, version="x", encoder=_stub_encoder()
    )
    return bm25, index


def test_hybrid_searcher_rejects_misaligned_inputs() -> None:
    """If BM25 and dense don't have the same length, fusion is undefined."""
    chunks = _chunks(
        ("Modal", "t", ["Modal"], "<Modal />"),
        ("Button", "t", ["Button"], "<Button />"),
    )
    bm25 = ChunkBM25(chunks=chunks)
    smaller = build_examples_index(
        chunks=chunks[:1], version="x", encoder=_stub_encoder()
    )

    with pytest.raises(ValueError):
        HybridSearcher(bm25=bm25, embeddings=smaller)


def test_hybrid_searcher_returns_top_k_example_hits() -> None:
    """Full pipeline with no reranker returns ``ExampleHit`` rows."""
    chunks = _chunks(
        (
            "Modal",
            "Modal form",
            ["Modal", "FormItemInput"],
            "<Modal><FormItemInput /></Modal>",
        ),
        ("Button", "Icon-only button", ["Button", "Icon"], "<Button />"),
        ("Alert", "Inline alert", ["Alert"], "<Alert />"),
    )
    bm25, index = _build_pair(chunks)
    hybrid = HybridSearcher(bm25=bm25, embeddings=index, reranker=None)

    hits = hybrid.search(query="FormItemInput", top_k=2)

    assert len(hits) <= 2
    assert hits, "expected at least one hit"
    assert hits[0].component_name == "Modal"
    # No reranker → ``score`` is the RRF score, monotonically
    # decreasing across the returned hits.
    if len(hits) > 1:
        assert hits[0].score >= hits[1].score


def test_hybrid_searcher_reranker_reorders_candidates() -> None:
    """A custom reranker that boosts a specific chunk wins the top slot.

    The first-stage retrievers might rank ``Modal`` first; the
    reranker's job is to override that when it disagrees. We inject
    a reranker that gives the Button chunk the highest score, and
    expect Button to come out first.
    """
    chunks = _chunks(
        (
            "Modal",
            "Modal form",
            ["Modal", "FormItemInput"],
            "<Modal><FormItemInput /></Modal>",
        ),
        ("Button", "Icon-only button", ["Button", "Icon"], "<Button />"),
        ("Alert", "Inline alert", ["Alert"], "<Alert />"),
    )
    bm25, index = _build_pair(chunks)

    def fake_reranker(query: str, documents: list[str]) -> np.ndarray:
        scores = np.zeros(len(documents), dtype=np.float32)
        for i, doc in enumerate(documents):
            scores[i] = 10.0 if doc.startswith("Button") else 0.0
        return scores

    hybrid = HybridSearcher(bm25=bm25, embeddings=index, reranker=fake_reranker)
    assert hybrid.has_reranker is True

    hits = hybrid.search(query="anything that exists", top_k=3)

    assert hits[0].component_name == "Button"
    assert hits[0].score == pytest.approx(10.0)


def test_hybrid_searcher_use_reranker_false_bypasses_rerank() -> None:
    """``use_reranker=False`` skips the rerank stage even when injected."""
    chunks = _chunks(
        (
            "Modal",
            "Modal form",
            ["Modal", "FormItemInput"],
            "<Modal><FormItemInput /></Modal>",
        ),
        ("Button", "Icon-only button", ["Button", "Icon"], "<Button />"),
    )
    bm25, index = _build_pair(chunks)

    def fake_reranker(query: str, documents: list[str]) -> np.ndarray:
        raise AssertionError("reranker must not be called")

    hybrid = HybridSearcher(bm25=bm25, embeddings=index, reranker=fake_reranker)
    hits = hybrid.search(query="FormItemInput", top_k=2, use_reranker=False)

    assert hits, "expected at least one hit"
    assert hits[0].component_name == "Modal"


def test_hybrid_searcher_filter_components_applied_after_fusion() -> None:
    """``filter_components`` narrows the final hits, not the corpus."""
    chunks = _chunks(
        ("Modal", "Modal a", ["Modal"], "<Modal a />"),
        ("Modal", "Modal b", ["Modal"], "<Modal b />"),
        ("Button", "Button a", ["Button"], "<Button a />"),
    )
    bm25, index = _build_pair(chunks)
    hybrid = HybridSearcher(bm25=bm25, embeddings=index, reranker=None)

    hits = hybrid.search(query="modal", top_k=5, filter_components=["Modal"])

    assert hits, "expected modal hits"
    assert {h.component_name for h in hits} == {"Modal"}


def test_hybrid_searcher_empty_corpus_returns_no_hits() -> None:
    """Empty corpus → empty hits, no exceptions."""
    bm25 = ChunkBM25(chunks=[])
    index = build_examples_index(
        chunks=[], version="x", encoder=_stub_encoder()
    )
    hybrid = HybridSearcher(bm25=bm25, embeddings=index)

    assert hybrid.search(query="anything", top_k=5) == []


def test_hybrid_searcher_top_k_must_be_positive() -> None:
    """top_k <= 0 is a programmer error."""
    bm25 = ChunkBM25(chunks=[])
    index = build_examples_index(
        chunks=[], version="x", encoder=_stub_encoder()
    )
    hybrid = HybridSearcher(bm25=bm25, embeddings=index)

    with pytest.raises(ValueError):
        hybrid.search(query="modal", top_k=0)
