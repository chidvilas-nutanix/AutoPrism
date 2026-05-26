"""Tests for the embeddings module.

We never hit the real fastembed model in tests because:

* downloading a ~130 MB ONNX checkpoint inside CI is slow + flaky;
* the suite must remain hermetic per :mod:`tests.conftest`.

So :class:`prism_mcp.embeddings.ExamplesIndex` accepts a callable
``encoder``; tests inject a deterministic hash-based stub. The
production wiring builds the real fastembed-backed encoder inside
``server.py`` / ``library.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from prism_mcp.embeddings import (
    ExampleHit,
    ExamplesIndex,
    build_examples_index,
)
from prism_mcp.parsers.examples_md_code import ExampleChunk


def _stub_encoder(dim: int = 16):
    """Return a deterministic encoder mapping text → unit-norm vectors.

    Each call hashes the text into ``dim`` bytes, casts to float32, and
    normalises. Identical inputs return identical vectors so the test
    can assert exact rankings; *similar* texts (shared substrings) get
    moderately correlated vectors because the hash bytes drift
    deterministically with content.

    Args:
        dim (int): vector dimensionality. Default 16 keeps the test
            output readable.

    Returns:
        Callable[[list[str]], np.ndarray]: encoder.
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


def _stub_encoder_that_matches(query: str, dim: int = 16):
    """Return an encoder that gives ``query`` and a chosen target the
    *same* vector while randomising everything else.

    Useful when a test wants to assert "row N comes first" without
    being at the mercy of sha256 collisions.

    Args:
        query (str): the query text.
        dim (int): vector dim.

    Returns:
        Callable[[list[str]], np.ndarray]: encoder.
    """
    rng = np.random.default_rng(seed=0)
    target_vec = rng.standard_normal(dim).astype(np.float32)
    target_vec /= np.linalg.norm(target_vec)

    def encode(texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            if text == query or text.startswith(query + " "):
                vectors.append(target_vec)
            else:
                v = rng.standard_normal(dim).astype(np.float32)
                v /= np.linalg.norm(v)
                vectors.append(v)
        return np.stack(vectors)

    return encode


def _chunks(*specs: tuple[str, str, list[str]]) -> list[ExampleChunk]:
    """Tiny factory: ``(component_name, title, imports)`` → ExampleChunks."""
    return [
        ExampleChunk(
            component_name=comp,
            title=title,
            code=f"<{comp} />",
            language_tag="jsx",
            imports=imports,
            example_id=None,
        )
        for comp, title, imports in specs
    ]


def test_build_returns_query_able_index() -> None:
    """``build_examples_index`` produces a usable :class:`ExamplesIndex`."""
    chunks = _chunks(
        ("Modal", "Modal with form fields", ["Modal", "FormItemInput"]),
        ("Button", "Icon-only button", ["Button", "Icon"]),
        ("Alert", "Inline alert", ["Alert"]),
    )

    index = build_examples_index(
        chunks=chunks,
        version="2.54.0",
        encoder=_stub_encoder(),
    )

    assert len(index) == 3
    assert index.version == "2.54.0"


def test_query_returns_top_k_in_descending_score_order() -> None:
    """Search returns results sorted high-to-low and capped at top_k."""
    chunks = _chunks(
        ("Modal", "Modal with form fields", ["Modal", "FormItemInput"]),
        ("Button", "Icon-only button", ["Button", "Icon"]),
        ("Alert", "Inline alert", ["Alert"]),
        ("Tooltip", "Default tooltip", ["Tooltip"]),
    )
    index = build_examples_index(
        chunks=chunks,
        version="x",
        encoder=_stub_encoder(),
    )

    hits = index.query(query="anything", top_k=2)

    assert len(hits) == 2
    assert hits[0].score >= hits[1].score
    assert all(isinstance(h, ExampleHit) for h in hits)


def test_query_with_matching_encoder_finds_the_intended_chunk() -> None:
    """When the encoder maps query == target → that chunk ranks first.

    Pins the contract: ``ExamplesIndex.query`` actually uses cosine
    similarity, not just returns chunks in insertion order.
    """
    chunks = _chunks(
        ("Modal", "Modal with form fields", ["Modal", "FormItemInput"]),
        ("Button", "Icon-only button", ["Button", "Icon"]),
        ("Alert", "Inline alert", ["Alert"]),
    )
    # The encoder builds the input text from the chunk; we have to
    # mirror that to know what the index will see.
    target_text = (
        "Button \u2014 Icon-only button\nImports: Button, Icon\n<Button />"
    )

    index = build_examples_index(
        chunks=chunks,
        version="x",
        encoder=_stub_encoder_that_matches(query=target_text),
    )

    hits = index.query(query=target_text, top_k=3)

    assert hits[0].component_name == "Button"
    assert hits[0].title == "Icon-only button"
    assert hits[0].score >= hits[1].score


def test_query_top_k_filter_by_component_name() -> None:
    """``filter_components`` keeps only the named components."""
    chunks = _chunks(
        ("Modal", "Modal a", ["Modal"]),
        ("Modal", "Modal b", ["Modal"]),
        ("Button", "Button a", ["Button"]),
        ("Button", "Button b", ["Button"]),
    )
    index = build_examples_index(
        chunks=chunks, version="x", encoder=_stub_encoder()
    )

    hits = index.query(query="anything", top_k=10, filter_components=["Modal"])

    assert {h.component_name for h in hits} == {"Modal"}
    assert len(hits) == 2


def test_empty_corpus_returns_no_hits() -> None:
    """Building an empty index is valid; queries return ``[]``."""
    index = build_examples_index(
        chunks=[], version="x", encoder=_stub_encoder()
    )

    assert len(index) == 0
    assert index.query(query="anything", top_k=5) == []


def test_top_k_must_be_positive() -> None:
    """``top_k <= 0`` is a programmer error."""
    chunks = _chunks(("X", "t", ["X"]))
    index = build_examples_index(
        chunks=chunks, version="x", encoder=_stub_encoder()
    )

    import pytest

    with pytest.raises(ValueError):
        index.query(query="q", top_k=0)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    """An index round-trips through ``.npz`` so the production server
    doesn't re-encode on every cold start.
    """
    chunks = _chunks(
        ("Modal", "Modal with form fields", ["Modal", "FormItemInput"]),
        ("Button", "Icon-only button", ["Button", "Icon"]),
    )
    index = build_examples_index(
        chunks=chunks, version="2.54.0", encoder=_stub_encoder()
    )

    target = tmp_path / "examples.embeddings.npz"
    index.save(target)

    assert target.is_file()

    # Reload without an encoder — the on-disk vectors are enough to
    # serve queries; the encoder is only needed at query time to embed
    # the prompt.
    reloaded = ExamplesIndex.load(target, encoder=_stub_encoder())

    assert len(reloaded) == 2
    assert reloaded.version == "2.54.0"

    hits = reloaded.query(query="anything", top_k=2)
    assert len(hits) == 2
    assert {h.component_name for h in hits} == {"Modal", "Button"}


def test_load_rejects_version_mismatch(tmp_path: Path) -> None:
    """When the on-disk version doesn't match the expected version,
    ``load`` raises so the caller can fall back to a rebuild.
    """
    import pytest

    chunks = _chunks(("Modal", "t", ["Modal"]))
    index = build_examples_index(
        chunks=chunks, version="2.54.0", encoder=_stub_encoder()
    )
    target = tmp_path / "examples.embeddings.npz"
    index.save(target)

    with pytest.raises(ValueError):
        ExamplesIndex.load(
            target,
            encoder=_stub_encoder(),
            expected_version="2.55.0",
        )


def test_load_accepts_matching_expected_version(tmp_path: Path) -> None:
    """The happy-path explicit-version check passes when versions match."""
    chunks = _chunks(("Modal", "t", ["Modal"]))
    index = build_examples_index(
        chunks=chunks, version="2.54.0", encoder=_stub_encoder()
    )
    target = tmp_path / "examples.embeddings.npz"
    index.save(target)

    reloaded = ExamplesIndex.load(
        target, encoder=_stub_encoder(), expected_version="2.54.0"
    )
    assert reloaded.version == "2.54.0"


def test_build_skips_a11y_and_anti_pattern_chunks_by_default() -> None:
    """The default builder drops a11y + anti-pattern + noeditor chunks.

    The embedding index is the LLM's "show me a good example" surface;
    feeding it noeditor docs or anti-patterns is actively harmful.
    """
    chunks = [
        ExampleChunk(
            component_name="Button",
            title="Good",
            code="<Button />",
            language_tag="jsx",
            imports=["Button"],
        ),
        ExampleChunk(
            component_name="Button",
            title="Bad",
            code="<Button kind='bad' />",
            language_tag="jsx",
            imports=["Button"],
            is_anti_pattern=True,
        ),
        ExampleChunk(
            component_name="Button",
            title="A11y",
            code="<Button />",
            language_tag="jsx noeditor",
            imports=["Button"],
            is_noeditor=True,
            is_a11y_block=True,
        ),
        ExampleChunk(
            component_name="Button",
            title="Docs",
            code="<Button />",
            language_tag="jsx noeditor",
            imports=["Button"],
            is_noeditor=True,
        ),
    ]
    index = build_examples_index(
        chunks=chunks, version="x", encoder=_stub_encoder()
    )

    assert len(index) == 1
    hits = index.query(query="anything", top_k=5)
    assert [h.title for h in hits] == ["Good"]
