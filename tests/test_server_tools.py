"""Integration tests for tools exposed by the MCP server.

Drives the in-process tool registry directly. Subprocess + stdio E2E is
deferred to a later slice as called out in PRD section 8.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np
import pytest

from prism_mcp.cache import Cache
from prism_mcp.config import ServerConfig
from prism_mcp.embeddings import Encoder
from prism_mcp.library import Library
from prism_mcp.refresh import RefreshLoopConfig
from prism_mcp.registry import RegistryClient
from prism_mcp.server import _tls_verify_value, build_server
from tests.conftest import make_latest_manifest, make_prism_tarball


def _stub_encoder() -> Encoder:
    """Hermetic encoder used by tests that touch ``search_examples``."""

    def encode(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).digest()[:16]
            raw = np.frombuffer(digest, dtype=np.uint8).astype(np.float32)
            norm = np.linalg.norm(raw)
            out[i] = raw / norm if norm > 0 else raw
        return out

    return encode


def _stub_reranker():
    """Hermetic reranker that scores by sha256 of (query, doc).

    Deterministic and content-sensitive: each (query, document) pair
    gets a stable score in [0, 1] derived from the first byte of
    sha256, so the rerank stage actually reorders candidates but the
    test outcome is reproducible without downloading the real ONNX
    cross-encoder.
    """

    def rerank(query: str, documents: list[str]) -> np.ndarray:
        scores = np.zeros(len(documents), dtype=np.float32)
        for i, doc in enumerate(documents):
            payload = (query + "\0" + doc).encode("utf-8")
            scores[i] = hashlib.sha256(payload).digest()[0] / 255.0
        return scores

    return rerank


PACKAGE = "@nutanix-ui/prism-reactjs"
VERSION = "2.54.0"
TARBALL_URL = f"https://reg.test/{PACKAGE}/-/prism-reactjs-{VERSION}.tgz"


def _library_factory(
    cache_root: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    encoder: Encoder | None = None,
    reranker: object | None = None,
) -> Callable[[], Library]:
    """Return a callable that builds a Library wired to ``handler``.

    Args:
        cache_root (Path): tmp cache root.
        handler (Callable): MockTransport handler for httpx.
        encoder (Encoder | None): when supplied, the resulting
            :class:`Library` is wired with this encoder so tests that
            exercise the slice-9 ``search_examples`` path stay
            hermetic. ``None`` matches the existing slice 1-8 tests
            which never touch ``examples_index``.
        reranker (object | None): when supplied, the resulting
            :class:`Library` is wired with this cross-encoder
            reranker so the slice-9 hybrid pipeline doesn't
            lazy-load the real fastembed ONNX model in tests.
    """
    config = ServerConfig(
        registry_base_url="https://reg.test/api/npm/canaveral-npm/",
        package_name=PACKAGE,
        cache_dir=cache_root,
        auth_header="Basic dGVzdA==",
    )

    def factory() -> Library:
        client = RegistryClient(
            base_url=config.registry_base_url,
            auth_header=config.auth_header,
            transport=httpx.MockTransport(handler),
        )
        return Library(
            config=config,
            registry=client,
            cache=Cache(cache_root),
            encoder=encoder,
            reranker=reranker,  # type: ignore[arg-type]
        )

    return factory


def test_tls_verify_default_uses_certifi() -> None:
    """No CA bundle and no insecure flag => httpx uses certifi (``True``)."""
    cfg = ServerConfig(
        registry_base_url="https://reg.test/",
        package_name=PACKAGE,
        cache_dir=Path("/tmp/x"),
        auth_header=None,
    )
    assert _tls_verify_value(cfg) is True


def test_tls_verify_uses_ca_bundle_when_set(tmp_path: Path) -> None:
    """An explicit CA bundle path is forwarded as the verify value."""
    pem = tmp_path / "ntnx-ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    cfg = ServerConfig(
        registry_base_url="https://reg.test/",
        package_name=PACKAGE,
        cache_dir=Path("/tmp/x"),
        auth_header=None,
        ca_bundle=pem,
    )
    assert _tls_verify_value(cfg) == str(pem)


def test_tls_verify_ca_bundle_overrides_insecure(tmp_path: Path) -> None:
    """Explicit trust wins over the insecure escape hatch."""
    pem = tmp_path / "ntnx-ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    cfg = ServerConfig(
        registry_base_url="https://reg.test/",
        package_name=PACKAGE,
        cache_dir=Path("/tmp/x"),
        auth_header=None,
        ca_bundle=pem,
        insecure_tls=True,
    )
    assert _tls_verify_value(cfg) == str(pem)


def test_tls_verify_returns_false_when_insecure_only() -> None:
    """``insecure_tls=True`` alone disables verification."""
    cfg = ServerConfig(
        registry_base_url="https://reg.test/",
        package_name=PACKAGE,
        cache_dir=Path("/tmp/x"),
        auth_header=None,
        insecure_tls=True,
    )
    assert _tls_verify_value(cfg) is False


def _read_text_chunks(content_blocks: object) -> str:
    """Concatenate text content from a FastMCP call_tool block list."""
    return "".join(
        getattr(block, "text", "")
        for block in content_blocks  # type: ignore[union-attr]
    )


@pytest.mark.asyncio
async def test_get_library_meta_returns_resolved_state(
    cache_root: Path,
) -> None:
    """The MCP tool returns the same dict the orchestrator computed."""
    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    blocks, structured = await server.call_tool(
        "get_library_meta", arguments={}
    )

    expected_keys = {
        "package_name",
        "version",
        "last_indexed_at",
        "source_url",
        "cache_path",
        "from_cache",
    }

    # FastMCP wraps scalar returns under {"result": ...} but passes dict
    # returns through verbatim. ``get_library_meta`` returns a dict, so
    # ``structured`` IS the payload.
    assert set(structured) == expected_keys
    assert structured["package_name"] == PACKAGE
    assert structured["version"] == VERSION
    assert structured["source_url"] == TARBALL_URL
    assert structured["from_cache"] is False

    text = _read_text_chunks(blocks)
    assert PACKAGE in text
    assert VERSION in text


@pytest.mark.asyncio
async def test_listed_tools_include_post_consolidation_surface() -> None:
    """The v1 tool surface (knowledge + Figma mapping).

    Earlier consolidation pruned low-signal tools (``list_entities``,
    ``map_token``, ``check_contrast``, ``get_a11y_rules``,
    ``related_components``, ``get_component_cluster``,
    ``reflect_on_spec``, ``get_snapshot_template``); the Temporal
    verification loop and its tools were removed when validation moved
    to Cursor. What remains is the canonical Figma->Prism flow plus
    ``echo`` (operator-only).
    """
    server = build_server()

    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    # Public LLM-facing slice-1..11 tools (the survivors).
    assert {
        "echo",
        "get_library_meta",
        "get_entity",
        "search_entities",
        "search_examples",
    } <= names

    # The pruned tools must not be re-introduced.
    for removed in (
        "list_entities",
        "map_token",
        "check_contrast",
        "get_a11y_rules",
        "related_components",
        "get_component_cluster",
        # Removed with the Temporal verification loop.
        "start_generate_component",
        "submit_candidate",
        "get_component_status",
        "get_final_artefact",
        "compare_to_figma",
        "get_pwspec_example",
        "update_companion_tests",
    ):
        assert removed not in names, (
            f"{removed!r} was pruned in the slice-12.x consolidation; "
            "do not re-register without revisiting the tool budget"
        )

    # Phase-7 page-level surface: map_figma_tree is the only new
    # public entrypoint. The fetcher and walker helpers stay
    # package-private — exposing them would tempt callers to skip
    # the noise filter and pass raw Figma JSON straight into the
    # LLM (defeats the whole point of the walker).
    assert "map_figma_tree" in names
    for forbidden in (
        "fetch_figma_tree",
        "_fetch_figma_tree",
        "parse_figma_url",
        "walk_tree",
    ):
        assert forbidden not in names, (
            f"{forbidden!r} must remain package-private; "
            "the public entrypoint is map_figma_tree"
        )


@pytest.mark.asyncio
async def test_get_entity_returns_design_token_value(
    cache_root: Path,
) -> None:
    """Slice 6 demo: ``get_entity({name:"color-primary",type:"token"})``."""
    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    _, payload = await server.call_tool(
        "get_entity",
        arguments={"name": "color-primary", "type": "token"},
    )

    assert payload["type"] == "token"
    assert payload["name"] == "color-primary"
    assert payload["category"] == "color"
    assert payload["value"] == "#1B6BCC"
    assert payload["source_file"] == "src/styles/v2/Colors.less"


@pytest.mark.asyncio
async def test_search_entities_finds_focus_trap_hook(
    cache_root: Path,
) -> None:
    """Slice 5 demo: prose query for the focus-trap hook resolves it."""
    tarball = make_prism_tarball(
        version=VERSION,
        components=("Button", "Modal"),
        hooks=("useFocusTrap", "useResizeObserver"),
    )
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    _, search_payload = await server.call_tool(
        "search_entities",
        arguments={
            "query": "hook for trapping focus",
            "top_k": 3,
            "type": "hook",
        },
    )

    assert search_payload["results"], "expected at least one match"
    top = search_payload["results"][0]
    assert top["name"] == "useFocusTrap"
    assert top["type"] == "hook"
    assert {"focus", "trap"} <= set(top["why_matched"])

    _, full = await server.call_tool(
        "get_entity",
        arguments={"name": "useFocusTrap", "type": "hook"},
    )

    assert full["name"] == "useFocusTrap"
    assert full["type"] == "hook"
    param_names = {m["name"] for m in full["signature"]}
    assert {"innerRef", "options"} <= param_names


@pytest.mark.asyncio
async def test_search_entities_ranks_components_by_query(
    cache_root: Path,
) -> None:
    """``search_entities`` returns BM25-ranked rows with ``why_matched``."""
    tarball = make_prism_tarball(
        version=VERSION, components=("Button", "Modal", "Alert")
    )
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    _, structured = await server.call_tool(
        "search_entities",
        arguments={"query": "primary action button", "top_k": 3},
    )

    assert structured["version"] == VERSION
    results = structured["results"]
    assert results, "expected at least one result"
    assert results[0]["name"] == "Button"
    assert "button" in results[0]["why_matched"]
    assert results[0]["score"] > 0


@pytest.mark.asyncio
async def test_get_entity_returns_full_record(
    cache_root: Path,
) -> None:
    """``get_entity`` returns the full Entity dump including signature."""
    tarball = make_prism_tarball(version=VERSION, components=("Button",))
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    _, structured = await server.call_tool(
        "get_entity",
        arguments={"name": "Button", "type": "component"},
    )

    assert structured["name"] == "Button"
    assert structured["type"] == "component"
    assert structured["version"] == VERSION
    assert structured["import_path"].endswith(
        "from '@nutanix-ui/prism-reactjs';"
    )

    prop_names = {m["name"] for m in structured["signature"]}
    assert {"onClick", "disabled", "className"} <= prop_names


@pytest.mark.asyncio
async def test_get_entity_unknown_name_returns_error(
    cache_root: Path,
) -> None:
    """Unknown entities surface as an MCP tool error, not silent None."""
    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    from mcp.server.fastmcp.exceptions import ToolError

    server = build_server(library_factory=_library_factory(cache_root, handler))

    with pytest.raises(ToolError, match="no entity found"):
        await server.call_tool(
            "get_entity",
            arguments={"name": "Nonexistent", "type": "component"},
        )


@pytest.mark.asyncio
async def test_server_lifespan_starts_and_stops_refresh_loop(
    cache_root: Path,
) -> None:
    """The FastMCP lifespan boots and tears down the refresh task.

    Slice 7 wires :class:`RefreshLoop` into ``FastMCP``'s lifespan so
    every running server polls Artifactory on cold start and once per
    day after. We drive the lifespan directly here because FastMCP's
    public API doesn't expose a "run lifespan once" helper — the
    same context manager ``build_server`` registered is reached via
    ``server._mcp_server.lifespan``.
    """
    import asyncio

    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(
        library_factory=_library_factory(cache_root, handler),
        refresh_config=RefreshLoopConfig(
            interval_seconds=0.01,
            jitter_seconds=0.0,
            run_on_start=True,
        ),
    )

    inner = server._mcp_server
    async with inner.lifespan(inner):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_get_library_meta_reports_offline_via_from_cache(
    cache_root: Path,
) -> None:
    """Slice 8 demo: registry down + warm cache => from_cache=True surfaced.

    First we let the server fetch normally (warming the cache), then
    we mutate the handler to raise ``ConnectError`` and call
    ``get_library_meta`` again. The MCP response must carry
    ``from_cache=True`` so an LLM client can detect degraded mode.
    """
    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    state = {"online": True}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not state["online"]:
            raise httpx.ConnectError("offline")
        if url == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    server = build_server(library_factory=_library_factory(cache_root, handler))

    _, online_meta = await server.call_tool("get_library_meta", arguments={})
    assert online_meta["from_cache"] is False

    state["online"] = False

    _, offline_meta = await server.call_tool("get_library_meta", arguments={})
    assert offline_meta["from_cache"] is True
    assert offline_meta["version"] == VERSION
    assert offline_meta["source_url"] == "(offline)"


# --------------------------------------------------------------------------
# Slice 9 — search_examples tool surface tests.
# --------------------------------------------------------------------------


def _examples_handler(
    tarball: bytes, document: dict
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a stock MockTransport handler for the slice 9 tests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    return handler


@pytest.mark.asyncio
async def test_search_examples_returns_results_shape(
    cache_root: Path,
) -> None:
    """``search_examples`` returns ``{version, results=[ExampleHit...]}``."""
    tarball = make_prism_tarball(
        version=VERSION,
        components=("Button", "Modal"),
    )
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )
    server = build_server(
        library_factory=_library_factory(
            cache_root,
            _examples_handler(tarball, document),
            encoder=_stub_encoder(),
            reranker=_stub_reranker(),
        )
    )

    _, payload = await server.call_tool(
        "search_examples",
        arguments={"query": "icon button", "top_k": 3},
    )

    assert payload["version"] == VERSION
    assert isinstance(payload["results"], list)
    assert 0 < len(payload["results"]) <= 3
    first = payload["results"][0]
    expected_keys = {
        "component_name",
        "example_id",
        "title",
        "code",
        "imports",
        "score",
    }
    assert expected_keys <= set(first)
    assert isinstance(first["score"], float)


@pytest.mark.asyncio
async def test_search_examples_filter_components_narrows_results(
    cache_root: Path,
) -> None:
    """``filter_components`` confines hits to the named components."""
    tarball = make_prism_tarball(
        version=VERSION,
        components=("Button", "Modal", "Alert"),
    )
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )
    server = build_server(
        library_factory=_library_factory(
            cache_root,
            _examples_handler(tarball, document),
            encoder=_stub_encoder(),
            reranker=_stub_reranker(),
        )
    )

    _, payload = await server.call_tool(
        "search_examples",
        arguments={
            "query": "anything",
            "top_k": 10,
            "filter_components": ["Modal"],
        },
    )

    assert {row["component_name"] for row in payload["results"]} == {"Modal"}


@pytest.mark.asyncio
async def test_search_examples_invalid_top_k_surfaces_error(
    cache_root: Path,
) -> None:
    """A ``top_k=0`` propagates as an MCP tool error (ValueError)."""
    from mcp.shared.exceptions import McpError

    tarball = make_prism_tarball(version=VERSION)
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )
    server = build_server(
        library_factory=_library_factory(
            cache_root,
            _examples_handler(tarball, document),
            encoder=_stub_encoder(),
            reranker=_stub_reranker(),
        )
    )

    with pytest.raises((McpError, ValueError, Exception)):
        await server.call_tool(
            "search_examples",
            arguments={"query": "x", "top_k": 0},
        )


@pytest.mark.asyncio
async def test_search_examples_reranker_param_bypasses_rerank(
    cache_root: Path,
) -> None:
    """``reranker=False`` skips the cross-encoder stage even when wired.

    Pins the SOTA tool contract: callers can opt out of the rerank
    refinement for latency-sensitive batch calls. We assert this
    by injecting a reranker that would raise if invoked.
    """
    tarball = make_prism_tarball(
        version=VERSION,
        components=("Button", "Modal"),
    )
    document = make_latest_manifest(
        package_name=PACKAGE,
        version=VERSION,
        tarball_url=TARBALL_URL,
        tarball_bytes=tarball,
    )

    def exploding_reranker(query: str, documents: list[str]) -> np.ndarray:
        raise AssertionError("reranker must not be called")

    server = build_server(
        library_factory=_library_factory(
            cache_root,
            _examples_handler(tarball, document),
            encoder=_stub_encoder(),
            reranker=exploding_reranker,
        )
    )

    _, payload = await server.call_tool(
        "search_examples",
        arguments={
            "query": "modal",
            "top_k": 3,
            "reranker": False,
        },
    )

    assert payload["version"] == VERSION
    assert isinstance(payload["results"], list)
