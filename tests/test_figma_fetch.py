"""Tests for the Figma REST fetcher (Phase 5).

Covers:

* URL parsing (every accepted form + invalid inputs).
* Error code mapping for each HTTP status the fetcher cares about.
* Cache hit / miss behaviour with ``tmp_path`` as the cache dir.
* ``bypass_cache=True`` always re-fetches.

The fetcher's :func:`_fetch_figma_tree` accepts a
``client_factory`` kwarg so we never make real HTTP calls in unit
tests. The integration test in ``test_figma_fetch_integration.py``
is gated on ``$FIGMA_TOKEN`` being set.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from prism_mcp.figma.fetch import (
    FetchedTree,
    FetchError,
    FetchErrorCode,
    ParsedFigmaUrl,
    _fetch_figma_tree,
    _fetch_figma_tree_full,
    _unwrap_response_full,
    parse_figma_url,
)

# --------------------------------------------------------------------------
# URL parsing.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected_key, expected_node, expected_branch",
    [
        (
            "https://www.figma.com/design/abc123/My-File?node-id=624-6826",
            "abc123",
            "624:6826",
            False,
        ),
        (
            "https://figma.com/design/abc123/My-File?node-id=624-6826",
            "abc123",
            "624:6826",
            False,
        ),
        (
            "https://www.figma.com/file/abc123/My-File?node-id=1-2",
            "abc123",
            "1:2",
            False,
        ),
        (
            "https://www.figma.com/design/abc123/branch/br456/My-File?node-id=10-20",
            "br456",
            "10:20",
            True,
        ),
        (
            "https://www.figma.com/proto/abc123/Click-thru?node-id=5-6",
            "abc123",
            "5:6",
            False,
        ),
        (
            "https://www.figma.com/design/abc123/My-File?other=1&node-id=624-6826&extra=2",
            "abc123",
            "624:6826",
            False,
        ),
        (
            "  https://www.figma.com/design/abc123/My-File?node-id=624-6826  ",
            "abc123",
            "624:6826",
            False,
        ),
    ],
)
def test_parse_figma_url_accepts_canonical_forms(
    url: str,
    expected_key: str,
    expected_node: str,
    expected_branch: bool,
) -> None:
    parsed = parse_figma_url(url)
    assert parsed.file_key == expected_key
    assert parsed.node_id == expected_node
    assert parsed.is_branch is expected_branch
    assert parsed.original_url == url.strip()


def test_parse_figma_url_accepts_already_colon_form_node_id() -> None:
    """Figma URLs occasionally arrive with ``:`` already (e.g.
    pasted from a script). We accept that too."""
    parsed = parse_figma_url(
        "https://www.figma.com/design/k/x?node-id=624:6826"
    )
    assert parsed.node_id == "624:6826"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/design/k/x?node-id=1-2",
        "not a url at all",
        "https://www.figma.com/design/abc123/My-File",
        "https://www.figma.com/design/abc123/My-File?node-id=",
        "https://www.figma.com/anything-else/abc123",
    ],
)
def test_parse_figma_url_rejects_bad_urls(url: str) -> None:
    with pytest.raises(FetchError) as ei:
        parse_figma_url(url)
    assert ei.value.code == FetchErrorCode.invalid_url


def test_parse_figma_url_rejects_non_str() -> None:
    with pytest.raises(FetchError) as ei:
        parse_figma_url(None)  # type: ignore[arg-type]
    assert ei.value.code == FetchErrorCode.invalid_url


# --------------------------------------------------------------------------
# HTTP behaviour with a stub client_factory.
# --------------------------------------------------------------------------


class _StubResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(
        self, *, status_code: int, body: Any = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _StubClient:
    """Minimal stub for ``httpx.AsyncClient`` async context manager."""

    def __init__(self, responses: list[Any]) -> None:
        # ``responses`` may contain _StubResponse instances OR
        # Exception instances — the latter are raised at the
        # matching attempt index.
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _StubResponse:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise RuntimeError("no more stubbed responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _make_factory(stub: _StubClient) -> Any:
    @asynccontextmanager
    async def _factory():
        async with stub as client:
            yield client

    return lambda: _factory()


@pytest.fixture()
def _no_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)


def _ok_response(node_id: str = "1:1") -> _StubResponse:
    return _StubResponse(
        status_code=200,
        body={
            "name": "Test",
            "nodes": {
                node_id: {
                    "document": {
                        "id": node_id,
                        "name": "root",
                        "type": "FRAME",
                        "absoluteBoundingBox": {
                            "x": 0,
                            "y": 0,
                            "width": 1,
                            "height": 1,
                        },
                    }
                }
            },
        },
    )


def test_fetch_missing_token_raises(
    _no_token_env: None, tmp_path: Path
) -> None:
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token=None,
                cache_dir=tmp_path,
            )
        )
    assert ei.value.code == FetchErrorCode.missing_token


def test_fetch_invalid_token_on_403(tmp_path: Path) -> None:
    stub = _StubClient([_StubResponse(status_code=403, text="Forbidden")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.invalid_token


def test_fetch_file_not_found_on_404(tmp_path: Path) -> None:
    stub = _StubClient([_StubResponse(status_code=404, text="Not Found")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.file_not_found


def test_fetch_rate_limited_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 retries past the first attempt = 4 total 429s → rate_limited."""
    monkeypatch.setattr(
        "prism_mcp.figma.fetch._RETRY_BACKOFF_SECONDS",
        (0.0, 0.0, 0.0),
    )
    stub = _StubClient([_StubResponse(status_code=429, text="Slow down")] * 4)
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.rate_limited


def test_fetch_recovers_after_transient_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 followed by a 200 must succeed via retry."""
    monkeypatch.setattr(
        "prism_mcp.figma.fetch._RETRY_BACKOFF_SECONDS",
        (0.0, 0.0, 0.0),
    )
    stub = _StubClient(
        [
            _StubResponse(status_code=500, text="Internal"),
            _ok_response("1:1"),
        ]
    )
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
        )
    )
    assert document["id"] == "1:1"
    assert document["type"] == "FRAME"


def test_fetch_timeout_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "prism_mcp.figma.fetch._RETRY_BACKOFF_SECONDS",
        (0.0, 0.0, 0.0),
    )
    stub = _StubClient([httpx.ConnectTimeout("slow")] * 4)
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.network_timeout


def test_fetch_transport_error_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "prism_mcp.figma.fetch._RETRY_BACKOFF_SECONDS",
        (0.0, 0.0, 0.0),
    )
    stub = _StubClient([httpx.ConnectError("dns fail")] * 4)
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.transport_error


def test_fetch_node_not_found_when_response_missing_node(
    tmp_path: Path,
) -> None:
    """The HTTP succeeded but the node id isn't in the body —
    Figma's "file exists, node doesn't" 200 case."""
    stub = _StubClient(
        [
            _StubResponse(
                status_code=200,
                body={"name": "Test", "nodes": {}},
            )
        ]
    )
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.node_not_found


def test_fetch_tree_too_large_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop the cap to a tiny value, then any reasonable payload
    trips it."""
    monkeypatch.setattr(
        "prism_mcp.figma.fetch._TREE_SIZE_CAP_BYTES",
        10,
    )
    stub = _StubClient([_ok_response("1:1")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    with pytest.raises(FetchError) as ei:
        asyncio.run(
            _fetch_figma_tree(
                parsed=parsed,
                figma_token="t",
                cache_dir=tmp_path,
                client_factory=_make_factory(stub),
            )
        )
    assert ei.value.code == FetchErrorCode.tree_too_large


# --------------------------------------------------------------------------
# Cache hit / miss.
# --------------------------------------------------------------------------


def test_fetch_writes_cache_on_success(tmp_path: Path) -> None:
    stub = _StubClient([_ok_response("1:1")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            depth=6,
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
        )
    )
    cache_file = tmp_path / "k--1_1--6.json"
    assert cache_file.is_file()
    body = json.loads(cache_file.read_text())
    assert "nodes" in body


def test_fetch_reads_cache_on_hit(tmp_path: Path) -> None:
    """A fresh cache file is served WITHOUT touching the stub."""
    cache_file = tmp_path / "k--1_1--6.json"
    cache_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "1:1": {
                        "document": {
                            "id": "1:1",
                            "name": "cached",
                            "type": "FRAME",
                        }
                    }
                }
            }
        )
    )
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")

    # An empty stub would raise if accessed — that proves cache
    # short-circuited the call.
    stub = _StubClient([])
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            depth=6,
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
        )
    )
    assert document["name"] == "cached"
    assert stub.calls == []


def test_fetch_bypass_cache_forces_refetch(tmp_path: Path) -> None:
    """``bypass_cache=True`` ignores even a valid cache entry."""
    cache_file = tmp_path / "k--1_1--6.json"
    cache_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "1:1": {
                        "document": {
                            "id": "1:1",
                            "name": "STALE",
                            "type": "FRAME",
                        }
                    }
                }
            }
        )
    )
    fresh = _StubResponse(
        status_code=200,
        body={
            "nodes": {
                "1:1": {
                    "document": {
                        "id": "1:1",
                        "name": "FRESH",
                        "type": "FRAME",
                    }
                }
            }
        },
    )
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    stub = _StubClient([fresh])
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            depth=6,
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
            bypass_cache=True,
        )
    )
    assert document["name"] == "FRESH"


# --------------------------------------------------------------------------
# P1 fetch fix — preserve the components / componentSets / styles maps.
# --------------------------------------------------------------------------


def _ok_response_with_maps(node_id: str = "1:1") -> _StubResponse:
    """A 200 carrying the document AND its sibling resolution maps."""
    return _StubResponse(
        status_code=200,
        body={
            "name": "Test",
            "nodes": {
                node_id: {
                    "document": {
                        "id": node_id,
                        "name": "root",
                        "type": "FRAME",
                    },
                    "components": {
                        "10:1": {
                            "key": "globalkeyA",
                            "name": "Action/ Button",
                            "description": "http://prism-styleguide/#/x?id=button",
                            "remote": True,
                        }
                    },
                    "componentSets": {
                        "10:0": {"key": "setkeyA", "name": "Action"}
                    },
                    "styles": {
                        "S:1": {"key": "stylekeyA", "name": "Title/H1"}
                    },
                }
            },
        },
    )


def test_unwrap_response_full_preserves_maps() -> None:
    """``_unwrap_response_full`` returns the document plus all 3 maps."""
    payload = {
        "nodes": {
            "1:1": {
                "document": {"id": "1:1", "type": "FRAME"},
                "components": {"c": {"key": "k"}},
                "componentSets": {"s": {"key": "sk"}},
                "styles": {"st": {"key": "stk"}},
            }
        }
    }
    fetched = _unwrap_response_full(payload, "1:1")
    assert isinstance(fetched, FetchedTree)
    assert fetched.document["id"] == "1:1"
    assert fetched.components == {"c": {"key": "k"}}
    assert fetched.component_sets == {"s": {"key": "sk"}}
    assert fetched.styles == {"st": {"key": "stk"}}


def test_unwrap_response_full_defaults_missing_maps_to_empty() -> None:
    """A document-only node (no maps) yields empty dicts, never None."""
    payload = {"nodes": {"1:1": {"document": {"id": "1:1", "type": "FRAME"}}}}
    fetched = _unwrap_response_full(payload, "1:1")
    assert fetched.components == {}
    assert fetched.component_sets == {}
    assert fetched.styles == {}


def test_fetch_full_returns_maps(tmp_path: Path) -> None:
    """``_fetch_figma_tree_full`` threads the maps through end-to-end."""
    stub = _StubClient([_ok_response_with_maps("1:1")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    fetched = asyncio.run(
        _fetch_figma_tree_full(
            parsed=parsed,
            figma_token="t",
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
        )
    )
    assert isinstance(fetched, FetchedTree)
    assert fetched.document["id"] == "1:1"
    assert fetched.components["10:1"]["key"] == "globalkeyA"
    assert fetched.component_sets["10:0"]["key"] == "setkeyA"
    assert fetched.styles["S:1"]["key"] == "stylekeyA"


def test_fetch_legacy_wrapper_still_returns_document_only(
    tmp_path: Path,
) -> None:
    """The backward-compatible ``_fetch_figma_tree`` keeps returning the
    bare document dict so existing callers/tests are unaffected."""
    stub = _StubClient([_ok_response_with_maps("1:1")])
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
        )
    )
    assert isinstance(document, dict)
    assert document["id"] == "1:1"
    # The wrapper returns ONLY the document — no maps leak through.
    assert "components" not in document


def test_fetch_expired_cache_falls_back_to_network(tmp_path: Path) -> None:
    """A cache file older than the TTL is treated as a miss."""
    cache_file = tmp_path / "k--1_1--6.json"
    cache_file.write_text(
        json.dumps(
            {
                "nodes": {
                    "1:1": {
                        "document": {
                            "name": "STALE",
                            "type": "FRAME",
                            "id": "1:1",
                        }
                    }
                }
            }
        )
    )
    import os

    very_old = 1
    os.utime(cache_file, (very_old, very_old))

    fresh = _StubResponse(
        status_code=200,
        body={
            "nodes": {
                "1:1": {
                    "document": {"id": "1:1", "name": "FRESH", "type": "FRAME"}
                }
            }
        },
    )
    parsed = ParsedFigmaUrl(file_key="k", node_id="1:1", original_url="x")
    stub = _StubClient([fresh])
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token="t",
            depth=6,
            cache_dir=tmp_path,
            client_factory=_make_factory(stub),
            cache_ttl_seconds=60,
        )
    )
    assert document["name"] == "FRESH"
