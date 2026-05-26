"""Slice 10: composition graph + Louvain community detection.

This module turns the :class:`ExampleChunk` corpus into a weighted
undirected graph where nodes are Prism component identifiers and edge
weights count how often two components are co-imported in the same
example. It then runs networkx's pure-Python Louvain implementation
on that graph so we can answer two complementary questions:

* **Local** — "what composes with ``X``?" → top neighbours by edge
  weight. Answered by :meth:`CompositionGraph.related`.
* **Global** — "what *kind* of component is ``X``?" → its Louvain
  community (cluster of frequently-co-composed components) plus that
  cluster's centroid nodes as a human-readable label. Answered by
  :meth:`CompositionGraph.cluster`.

This is the dual-level retrieval pattern named in the slice-10 SOTA
plan (and originally borrowed from LightRAG / Microsoft GraphRAG).
At our scale (~150 components, ~1.2K example chunks) it runs in low
single-digit milliseconds and ships zero new infrastructure — no
Neo4j, no LLM-at-index-time, no vector DB.

Why Louvain and not Leiden?

  Leiden (Traag et al. 2019) is theoretically stricter than Louvain
  on community modularity, and is the strict-SOTA pick on large
  citation / protein graphs. networkx ships Louvain natively but not
  Leiden; the Leiden implementations live in the ``leidenalg`` /
  ``python-igraph`` packages, which bring a C build. At ~150 nodes
  the resolution difference is negligible (under 1% modularity) and
  the trade-off does not favour another transitive dependency. See
  the comment in ``pyproject.toml``.
"""

from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field

from prism_mcp.parsers.examples_md_code import ExampleChunk

logger = logging.getLogger(__name__)

_LOUVAIN_SEED = 42
"""Deterministic seed for ``networkx.community.louvain_communities``.

Louvain is a randomised algorithm: each pass visits nodes in random
order, which can produce different (but similarly-modular) partitions
across runs. Fixing the seed makes the output stable, which keeps
prompt caches warm and snapshot tests deterministic.
"""

_CENTRAL_MEMBERS_LIMIT = 3
"""How many top-weighted-degree nodes to surface as a cluster's
``central_members``. Three is the LightRAG-paper-flavored default
for human-readable cluster labels — enough context to disambiguate
clusters, few enough to fit in a single LLM-prompt line.
"""


class GraphError(ValueError):
    """Raised on bad inputs to :class:`CompositionGraph` methods.

    Inherits :class:`ValueError` so existing ``except ValueError``
    handlers in the server tool layer surface it cleanly as an MCP
    tool error.
    """


class RelatedComponent(BaseModel):
    """One neighbour returned by :meth:`CompositionGraph.related`.

    Args:
        name (str): the neighbour's component identifier.
        weight (int): the co-occurrence count — how many distinct
            example chunks import *both* the query node and this
            neighbour. Higher = more frequently composed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    weight: int


class ClusterInfo(BaseModel):
    """Global community context for a queried component.

    Args:
        cluster_id (int): the Louvain partition index. Stable
            across invocations thanks to the fixed seed; meaningful
            only relative to the graph it was computed for. Two
            calls to :meth:`CompositionGraph.cluster` on the same
            graph will return the same ID for the same node.
        members (list[str]): alphabetically-sorted full member list.
            Includes the queried component itself.
        central_members (list[str]): up to three most-central
            members (by weighted degree within the subgraph induced
            by the cluster), sorted descending by that centrality.
            Used by the LLM as a quick label for the cluster's
            "topic" without us having to run an LLM at index time.
        label (str): the single most-central member's name — a
            convenient one-string handle when the LLM wants to say
            "X belongs to the Modal-Form composition cluster".
    """

    model_config = ConfigDict(extra="forbid")

    cluster_id: int
    members: list[str]
    central_members: list[str] = Field(default_factory=list)
    label: str


class CompositionGraph:
    """Wrapper around a :class:`networkx.Graph` of co-import edges.

    Pre-computes Louvain community membership and per-node weighted
    degree at construction time so :meth:`related` and :meth:`cluster`
    are O(neighbours) and O(1) lookups respectively.

    Args:
        graph (networkx.Graph): the assembled co-import graph. Edges
            must carry an integer ``"weight"`` attribute.
        version (str): the library version this graph was built
            from. Surfaced verbatim in tool responses so the agent
            can correlate ``related_components`` results with a
            specific library snapshot.
    """

    def __init__(self, *, graph: nx.Graph, version: str) -> None:
        self._graph = graph
        self.version = version
        self._communities = self._compute_communities()
        self._node_to_cluster_id = self._index_clusters()

    def nodes(self) -> list[str]:
        """Return all component names, alphabetically sorted."""
        return sorted(self._graph.nodes())

    def has_node(self, name: str) -> bool:
        """Return ``True`` iff ``name`` exists in the graph."""
        return name in self._graph

    def has_edge(self, a: str, b: str) -> bool:
        """Return ``True`` iff an edge ``a--b`` exists in the graph."""
        return self._graph.has_edge(a, b)

    def edge_weight(self, a: str, b: str) -> int:
        """Return the co-import count for edge ``(a, b)``, 0 if absent.

        Args:
            a (str): one endpoint's component name.
            b (str): the other endpoint's component name.

        Returns:
            int: the edge's ``weight``, or 0 when ``a`` and ``b``
            are not connected (or either is unknown).
        """
        if not self._graph.has_edge(a, b):
            return 0
        return int(self._graph[a][b]["weight"])

    def related(self, name: str, top_k: int) -> list[RelatedComponent]:
        """Return the top-``k`` neighbours of ``name`` by edge weight.

        Ties are broken alphabetically by neighbour name so the
        output is deterministic across runs.

        Args:
            name (str): the queried component identifier.
            top_k (int): maximum number of neighbours to return.
                Must be positive.

        Returns:
            list[RelatedComponent]: neighbours in descending
            ``(weight, name)`` order, capped at ``top_k``. Empty
            list when ``name`` is isolated.

        Raises:
            GraphError: when ``name`` is not in the graph, or
                ``top_k`` is non-positive.
        """
        if top_k <= 0:
            raise GraphError(f"top_k must be positive, got {top_k}")
        if name not in self._graph:
            raise GraphError(f"{name!r} not in composition graph")

        neighbours = (
            (other, int(self._graph[name][other]["weight"]))
            for other in self._graph.neighbors(name)
        )
        # Sort by (-weight, name) so ties break alphabetically.
        ranked = sorted(neighbours, key=lambda pair: (-pair[1], pair[0]))
        return [
            RelatedComponent(name=other, weight=weight)
            for other, weight in ranked[:top_k]
        ]

    def cluster(self, name: str) -> ClusterInfo:
        """Return the Louvain community ``name`` belongs to.

        Args:
            name (str): the queried component identifier.

        Returns:
            ClusterInfo: cluster ID, all members, top central
            members, and a one-string label.

        Raises:
            GraphError: when ``name`` is not in the graph.
        """
        if name not in self._graph:
            raise GraphError(f"{name!r} not in composition graph")

        cluster_id = self._node_to_cluster_id[name]
        members_set = self._communities[cluster_id]
        members = sorted(members_set)
        central = self._central_members(members_set)
        label = central[0] if central else name
        return ClusterInfo(
            cluster_id=cluster_id,
            members=members,
            central_members=central,
            label=label,
        )

    def _compute_communities(self) -> list[set[str]]:
        """Run Louvain over the weighted graph; return communities.

        Returns:
            list[set[str]]: one set per community, indexed by the
            ``cluster_id`` we assign in :meth:`_index_clusters`.
            Empty list when the graph has no nodes.
        """
        if self._graph.number_of_nodes() == 0:
            return []
        # ``weight="weight"`` tells Louvain to maximise weighted
        # modularity (the right thing here — heavy edges should
        # bind communities tighter).
        return list(
            nx.community.louvain_communities(
                self._graph,
                weight="weight",
                seed=_LOUVAIN_SEED,
            )
        )

    def _index_clusters(self) -> dict[str, int]:
        """Build the reverse node→cluster_id index for O(1) lookup."""
        index: dict[str, int] = {}
        for cluster_id, members in enumerate(self._communities):
            for node in members:
                index[node] = cluster_id
        return index

    def _central_members(self, members: set[str]) -> list[str]:
        """Return up to :data:`_CENTRAL_MEMBERS_LIMIT` most-central nodes.

        "Central" means highest weighted degree inside the subgraph
        induced by the cluster — i.e. the node with the most
        composition weight to other cluster members. Ties break
        alphabetically.

        Args:
            members (set[str]): the cluster's node set.

        Returns:
            list[str]: top central nodes, descending by intra-cluster
            weighted degree.
        """
        if not members:
            return []
        # Restrict to in-cluster edges so we measure centrality
        # *within* the community (not the whole graph).
        subgraph = self._graph.subgraph(members)
        scored: list[tuple[int, str]] = []
        for node in members:
            # Weighted degree = sum of edge weights touching the node.
            degree = int(subgraph.degree(node, weight="weight"))
            scored.append((degree, node))
        # Sort by (-degree, name) — descending degree, alphabetical ties.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [node for _, node in scored[:_CENTRAL_MEMBERS_LIMIT]]


def build_composition_graph(
    *,
    chunks: Sequence[ExampleChunk] | Iterable[ExampleChunk],
    version: str,
) -> CompositionGraph:
    """Assemble the co-import :class:`CompositionGraph` from chunks.

    The bipartite-projection algorithm:

    1. For each chunk's ``imports`` list, deduplicate to a set
       (so ``import { Button } ... import { Button as B } ...``
       does not double-count).
    2. For every unordered pair (A, B) in that set, bump the edge
       weight by 1.
    3. Singleton imports (a chunk with exactly one component) still
       contribute that one node to the graph so ``related(X)``
       returns ``[]`` cleanly instead of raising.

    Args:
        chunks (Sequence[ExampleChunk] | Iterable[ExampleChunk]):
            the source of co-occurrence signal. Typically the
            output of :func:`prism_mcp.embeddings.walk_example_chunks`.
        version (str): the library version stamp.

    Returns:
        CompositionGraph: the assembled wrapper, with Louvain
        communities already computed.
    """
    graph = nx.Graph()
    weights: dict[tuple[str, str], int] = defaultdict(int)

    for chunk in chunks:
        imports = sorted(set(chunk.imports))
        if not imports:
            continue
        # Always register every imported identifier as a node, even
        # singletons — keeps related() / cluster() honest about
        # which components have ever appeared in the example corpus.
        graph.add_nodes_from(imports)
        for a, b in itertools.combinations(imports, 2):
            # ``combinations`` over sorted input always yields a<b
            # so the tuple key is canonical; no risk of double-counting.
            weights[(a, b)] += 1

    for (a, b), weight in weights.items():
        graph.add_edge(a, b, weight=weight)

    logger.info(
        "built composition graph version=%s nodes=%d edges=%d",
        version,
        graph.number_of_nodes(),
        graph.number_of_edges(),
    )
    return CompositionGraph(graph=graph, version=version)
