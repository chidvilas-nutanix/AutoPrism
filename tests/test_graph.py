"""Tests for slice-10 composition graph + community detection.

The graph is built from :class:`ExampleChunk.imports` co-occurrences.
Two components co-imported in one chunk get an edge with weight=1; a
second chunk co-importing the same two bumps the weight to 2. This is
the classic GraphRAG-flavored bipartite-projection pattern, kept
deliberately simple — no NLP, no LLM at index time.

Community detection: networkx's Louvain with ``seed=42`` for
deterministic test runs. Louvain over Leiden is the pragmatic pick
(see ``pyproject.toml`` comment) — the modularity scores differ by
<1% on graphs of our size (~150 nodes) and Louvain ships natively
without the ``python-igraph`` C build.
"""

from __future__ import annotations

import pytest

from prism_mcp.graph import GraphError, build_composition_graph
from prism_mcp.parsers.examples_md_code import ExampleChunk


def _chunk(component: str, imports: list[str]) -> ExampleChunk:
    """Minimal :class:`ExampleChunk` for graph-building tests."""
    return ExampleChunk(
        component_name=component,
        title="t",
        code="<x/>",
        language_tag="jsx",
        imports=imports,
    )


# --------------------------------------------------------------------------
# build_composition_graph: edge weights, node existence, isolated chunks.
# --------------------------------------------------------------------------


def test_build_graph_single_chunk_two_imports_creates_one_edge() -> None:
    """One chunk co-importing A+B → one edge A—B with weight=1."""
    chunks = [_chunk("Modal", ["Modal", "Button"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert set(graph.nodes()) == {"Modal", "Button"}
    assert graph.has_edge("Modal", "Button")
    assert graph.edge_weight("Modal", "Button") == 1


def test_build_graph_two_chunks_same_pair_bumps_weight_to_two() -> None:
    """Repeated co-occurrence stacks: weight measures how often
    A and B are *actually used together* across the example corpus.
    This is the signal the LLM cares about — "which components
    commonly compose?".
    """
    chunks = [
        _chunk("Modal", ["Modal", "Button"]),
        _chunk("FullPageModal", ["Modal", "Button"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert graph.edge_weight("Modal", "Button") == 2


def test_build_graph_three_imports_creates_complete_triangle() -> None:
    """A chunk with {A,B,C} produces edges A—B, A—C, B—C (each w=1)."""
    chunks = [_chunk("Form", ["FormItemInput", "Button", "StackingLayout"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert graph.edge_weight("FormItemInput", "Button") == 1
    assert graph.edge_weight("FormItemInput", "StackingLayout") == 1
    assert graph.edge_weight("Button", "StackingLayout") == 1


def test_build_graph_single_import_chunk_adds_isolated_node() -> None:
    """A chunk with only one import → that node exists with 0 edges.

    Isolated nodes matter: ``related_components(X)`` should return
    an empty list (no neighbours), not raise. The node must still
    be queryable so the tool can say "no related components" cleanly.
    """
    chunks = [_chunk("Loader", ["Loader"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert "Loader" in set(graph.nodes())
    assert graph.related("Loader", top_k=5) == []


def test_build_graph_ignores_chunk_with_empty_imports() -> None:
    """A chunk with no imports contributes nothing — no edges, no nodes."""
    chunks = [
        _chunk("Modal", []),
        _chunk("Modal", ["Modal", "Button"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert set(graph.nodes()) == {"Modal", "Button"}


def test_build_graph_deduplicates_imports_within_chunk() -> None:
    """Duplicate names in a single chunk's import list → still w=1.

    A jsx body that has ``import { Button } from ...; ... import {
    Button as B } from ...`` (rare but possible) shouldn't inflate
    edge weights. The graph measures *which components compose*,
    not how many times each is referenced.
    """
    chunks = [_chunk("Modal", ["Modal", "Button", "Button", "Modal"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert graph.edge_weight("Modal", "Button") == 1


def test_build_graph_empty_chunks_returns_empty_graph() -> None:
    """No chunks → no nodes, no edges, no error."""
    graph = build_composition_graph(chunks=[], version="0.0.0")

    assert list(graph.nodes()) == []


# --------------------------------------------------------------------------
# related(): local neighbours ranked by edge weight.
# --------------------------------------------------------------------------


def test_related_ranks_neighbours_by_descending_edge_weight() -> None:
    """``related(X)`` returns neighbours sorted by composition frequency.

    This is the "local" half of dual-level retrieval (LightRAG
    terminology) — answer the question "what composes with X?".
    """
    chunks = [
        _chunk("Modal", ["Modal", "Button"]),  # Modal-Button w=2
        _chunk("Modal", ["Modal", "Button"]),
        _chunk("Modal", ["Modal", "StackingLayout"]),  # Modal-SL w=1
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")
    related = graph.related("Modal", top_k=5)

    assert [r.name for r in related] == ["Button", "StackingLayout"]
    assert related[0].weight == 2
    assert related[1].weight == 1


def test_related_respects_top_k() -> None:
    """``top_k`` caps the result list at the requested size."""
    chunks = [_chunk("Modal", ["Modal", "Button", "Form", "Loader"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert len(graph.related("Modal", top_k=2)) == 2


def test_related_breaks_ties_alphabetically_for_determinism() -> None:
    """Equal-weight neighbours sort alphabetically — deterministic output
    is critical for the tool contract; the LLM's prompt-cache stability
    depends on identical inputs producing identical outputs.
    """
    chunks = [_chunk("Modal", ["Modal", "Zeta", "Alpha", "Beta"])]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")
    related = graph.related("Modal", top_k=5)

    assert [r.name for r in related] == ["Alpha", "Beta", "Zeta"]


def test_related_unknown_component_raises_graph_error() -> None:
    """Unknown component name is a tool-contract error — surface it,
    don't silently return ``[]`` (the LLM would falsely conclude
    "no related" instead of "I have the wrong name")."""
    graph = build_composition_graph(
        chunks=[_chunk("Modal", ["Modal", "Button"])], version="0.0.0"
    )

    with pytest.raises(GraphError, match="not in composition graph"):
        graph.related("NotAComponent", top_k=3)


def test_related_top_k_must_be_positive() -> None:
    """``top_k <= 0`` is a programming bug, not a query — fail fast."""
    graph = build_composition_graph(
        chunks=[_chunk("Modal", ["Modal", "Button"])], version="0.0.0"
    )

    with pytest.raises(GraphError, match="top_k must be"):
        graph.related("Modal", top_k=0)


# --------------------------------------------------------------------------
# cluster(): global community membership (Louvain).
# --------------------------------------------------------------------------


def test_cluster_disconnected_components_get_different_cluster_ids() -> None:
    """Two disjoint subgraphs end up in different Louvain communities.

    This is the "global" half of dual-level retrieval — answer
    "what *kind* of components does X belong to?".
    """
    chunks = [
        # cluster 1: form widgets
        _chunk("Form", ["FormItemInput", "Button"]),
        _chunk("Form", ["FormItemInput", "Button"]),
        # cluster 2: layout widgets (no overlap with cluster 1)
        _chunk("Page", ["StackingLayout", "Loader"]),
        _chunk("Page", ["StackingLayout", "Loader"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")
    form_cluster = graph.cluster("FormItemInput")
    layout_cluster = graph.cluster("StackingLayout")

    assert form_cluster.cluster_id != layout_cluster.cluster_id
    assert set(form_cluster.members) == {"FormItemInput", "Button"}
    assert set(layout_cluster.members) == {"StackingLayout", "Loader"}


def test_cluster_label_is_top_central_member() -> None:
    """The cluster's ``label`` is the weighted-degree-highest node.

    Per the slice-10 plan: "just enumerate the top-3 most-central
    nodes per cluster as the cluster's 'label.'" We use the single
    most-central node as the label string + return the top-3 as
    ``central_members`` for the LLM to summarise.
    """
    chunks = [
        # Modal has degree 3 (Button, Form, Loader); the others have
        # degree 1 each. Modal should be the cluster's central node.
        _chunk("Modal", ["Modal", "Button"]),
        _chunk("Modal", ["Modal", "Form"]),
        _chunk("Modal", ["Modal", "Loader"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")
    cluster_info = graph.cluster("Modal")

    assert cluster_info.label == "Modal"
    assert "Modal" in cluster_info.central_members


def test_cluster_unknown_component_raises_graph_error() -> None:
    """Unknown name → :class:`GraphError`, same contract as :meth:`related`."""
    graph = build_composition_graph(
        chunks=[_chunk("Modal", ["Modal", "Button"])], version="0.0.0"
    )

    with pytest.raises(GraphError, match="not in composition graph"):
        graph.cluster("Ghost")


def test_cluster_is_deterministic_across_rebuilds() -> None:
    """Two builds from the same chunks produce identical cluster IDs.

    Louvain is randomized but networkx accepts ``seed=`` — we set
    it to 42 so prompt caches stay warm and snapshot tests stay
    stable.
    """
    chunks = [
        _chunk("Form", ["FormItemInput", "Button"]),
        _chunk("Form", ["FormItemInput", "Button"]),
        _chunk("Page", ["StackingLayout", "Loader"]),
    ]

    graph_a = build_composition_graph(chunks=chunks, version="0.0.0")
    graph_b = build_composition_graph(chunks=chunks, version="0.0.0")

    # Same node → same cluster ID across independent builds.
    for node in graph_a.nodes():
        assert (
            graph_a.cluster(node).cluster_id == graph_b.cluster(node).cluster_id
        )


def test_cluster_isolated_node_gets_its_own_singleton_cluster() -> None:
    """An isolated node (no edges) still belongs to a (singleton) cluster.

    Louvain treats every isolated node as its own community. The
    tool should report this cleanly: ``members == [node]``,
    ``label == node``.
    """
    chunks = [
        _chunk("Loader", ["Loader"]),
        _chunk("Form", ["FormItemInput", "Button"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")
    loader_cluster = graph.cluster("Loader")

    assert loader_cluster.members == ["Loader"]
    assert loader_cluster.label == "Loader"


# --------------------------------------------------------------------------
# CompositionGraph: invariants on the wrapper itself.
# --------------------------------------------------------------------------


def test_composition_graph_carries_version_for_cache_correlation() -> None:
    """The wrapper stamps the library version it was built from.

    Used by :class:`Library` to assert cache freshness — the same
    pattern as :class:`ColorTokenIndex` and :class:`ExamplesIndex`.
    """
    graph = build_composition_graph(chunks=[], version="1.2.3-canary")

    assert graph.version == "1.2.3-canary"


def test_composition_graph_nodes_iteration_is_alphabetical() -> None:
    """``nodes()`` returns alphabetically-sorted names for deterministic
    iteration in tests + downstream consumers."""
    chunks = [
        _chunk("X", ["Zeta", "Alpha", "Modal"]),
    ]

    graph = build_composition_graph(chunks=chunks, version="0.0.0")

    assert list(graph.nodes()) == ["Alpha", "Modal", "Zeta"]
