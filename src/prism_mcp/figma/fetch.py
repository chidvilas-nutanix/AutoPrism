"""Figma REST API fetcher — private to :mod:`prism_mcp.figma`.

This module is deliberately not re-exported from
``prism_mcp.figma.__init__`` and is never registered as a separate
MCP tool: the only legitimate caller is the ``map_figma_tree`` tool
wrapper in :mod:`prism_mcp.server`. Keeping it private to the
package avoids tempting downstream code to fetch a raw Figma tree
and bypass the walker.

See design doc §6 for the full contract — URL forms, error
taxonomy, retry policy, cache layout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
"""HTTP timeout per request. Generous because Figma's REST API
can be slow on very large files; the 30s window covers all but
the worst tail."""

_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
"""Exponential backoff schedule for retries on 429 / 5xx /
transient network errors. See design doc §6.4."""

_TREE_SIZE_CAP_BYTES = 10 * 1024 * 1024
"""Maximum size of the raw Figma tree JSON (post-parse).

10MB is well above any reasonable single-page Figma node — the
Active Cluster page in §8.1 is ~50KB. Beyond 10MB the user
almost certainly fetched a whole file by accident and the walker
would OOM downstream."""

_DEFAULT_CACHE_TTL_SECONDS = 3600
"""1-hour TTL for the on-disk cache. Long enough that an
iterative debug session ("walk, fix, walk again") avoids the
network; short enough that a designer-edit-and-fetch round-trip
still picks up the fresh tree."""

_DEFAULT_DEPTH = 12
"""Default ``depth`` query parameter sent to Figma's REST API.

Figma's docs say omitting ``depth`` returns the whole subtree;
specifying ``depth=N`` returns up to ``N`` levels below the
requested node (root counts as level 0).

We pin to ``12`` because real Nutanix designs routinely nest
table cells, icon stacks, and instance-of-instance wrappers
8-10 levels under the page-root FRAME. The previous default of
``6`` truncated FRAMEs like ``Table/Table Title`` to leaves with
``children: []``, which made shape-based pattern detectors
(``match_column_of_cells``, ``match_kpi_tile``) miss every match
because their interior signal had been stripped server-side.

12 levels is enough for every Nutanix page we have inspected and
still safely under the 10MB :data:`_TREE_SIZE_CAP_BYTES` for the
``Figma-basics`` and CPQ-style pages we walk. Callers needing a
deeper or shallower fetch can pass ``figma_depth=N`` to
``map_figma_tree`` per call without changing this default."""


# --------------------------------------------------------------------------
# Public dataclass for a parsed URL.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedFigmaUrl:
    """A parsed Figma node URL.

    Args:
        file_key (str): the file key (or the branch key when the
            URL references a branch — Figma's REST API treats
            branch keys as full files in this endpoint).
        node_id (str): canonical colon form (``"626:987"``),
            never the URL's hyphen form.
        is_branch (bool): True iff this URL referenced a branch,
            so the caller can warn about extra latency.
        original_url (str): the input URL, echoed back for
            logging / error messages.
    """

    file_key: str
    node_id: str
    is_branch: bool = False
    original_url: str = ""


@dataclass(frozen=True)
class FetchedTree:
    """A fetched Figma node subtree plus its sibling resolution maps.

    The Figma REST ``/v1/files/:key/nodes`` response nests, under
    ``nodes[<node_id>]``, the ``document`` SceneNode subtree alongside
    three resolution maps that are **siblings** of ``document`` — not
    children of it:

    * ``components`` — ``componentId -> {key, name, description, remote,
      componentSetId?, documentationLinks}``. The join that turns an
      ``INSTANCE``'s node-local ``componentId`` into the stable, global
      ``componentKey``. This is the single strongest identity signal and
      was historically discarded by :func:`_unwrap_response` (see
      ``improvements/01-current-state-analysis.md`` §1.1).
    * ``componentSets`` — ``componentSetId -> {key, name, description}``.
      The logical variant-family name + description live here when an
      instance belongs to a component-set.
    * ``styles`` — ``styleId -> {key, name, styleType}``. Text / fill /
      effect style references (P5 token resolution).

    Args:
        document (dict[str, Any]): the unwrapped
            ``nodes[node_id].document`` subtree — the exact value the
            legacy :func:`_fetch_figma_tree` returns.
        components (dict[str, Any]): the ``components`` map, or ``{}``
            when the response omitted it.
        component_sets (dict[str, Any]): the ``componentSets`` map, or
            ``{}`` when absent.
        styles (dict[str, Any]): the ``styles`` map, or ``{}`` when
            absent.
    """

    document: dict[str, Any]
    components: dict[str, Any]
    component_sets: dict[str, Any]
    styles: dict[str, Any]


# --------------------------------------------------------------------------
# Error taxonomy — design doc §6.4.
# --------------------------------------------------------------------------


class FetchErrorCode:
    """String-valued codes for :class:`FetchError`. Kept as a
    plain class of constants (not a StrEnum) so the JSON-RPC layer
    can compare them as raw strings without an import dance."""

    missing_token = "missing_token"
    invalid_token = "invalid_token"
    file_not_found = "file_not_found"
    node_not_found = "node_not_found"
    rate_limited = "rate_limited"
    network_timeout = "network_timeout"
    tree_too_large = "tree_too_large"
    transport_error = "transport_error"
    invalid_url = "invalid_url"


_HINT_BY_CODE = {
    FetchErrorCode.missing_token: (
        "Set FIGMA_TOKEN in your environment (or .env file) and retry. "
        "Generate a PAT at https://www.figma.com/developers/api#access-tokens."
    ),
    FetchErrorCode.invalid_token: (
        "FIGMA_TOKEN was rejected by Figma. Confirm the token is current and "
        "has access to this file."
    ),
    FetchErrorCode.file_not_found: (
        "Figma returned 404 for this file key. Check the URL and your access."
    ),
    FetchErrorCode.node_not_found: (
        "The file exists but the requested node id is not present. "
        "Confirm the node-id in the URL (hyphen-form is OK; we normalise)."
    ),
    FetchErrorCode.rate_limited: (
        "Figma is rate-limiting your token (HTTP 429). The fetcher already "
        "retried 3 times with exponential backoff. Try again in a minute."
    ),
    FetchErrorCode.network_timeout: (
        "Figma did not respond within 30s. Retry the call; if persistent, "
        "check your network."
    ),
    FetchErrorCode.tree_too_large: (
        "The Figma response exceeds the 10MB safety cap. Try a smaller "
        "node-id (a specific frame, not the whole page)."
    ),
    FetchErrorCode.transport_error: (
        "Network error talking to Figma. Retry the call; if persistent, "
        "check your network."
    ),
    FetchErrorCode.invalid_url: (
        "The Figma URL could not be parsed. Expected forms: "
        "figma.com/design/<key>/...?node-id=A-B or "
        "figma.com/file/<key>/...?node-id=A-B or "
        "figma.com/design/<key>/branch/<branchKey>/...?node-id=A-B."
    ),
}


class FetchError(Exception):
    """Raised by the Figma REST fetcher and its URL parser.

    Args:
        code (str): one of :class:`FetchErrorCode`'s constants.
        message (str): one-line user-facing description.
        hint (str): optional fix suggestion — defaulted from
            :data:`_HINT_BY_CODE` when not supplied.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else _HINT_BY_CODE.get(code, "")
        super().__init__(f"[{code}] {message}")


# --------------------------------------------------------------------------
# URL parsing.
# --------------------------------------------------------------------------


_DESIGN_URL_RE = re.compile(
    r"""
    ^https?://
    (?:www\.)?figma\.com/
    (?P<kind>design|file|proto)/
    (?P<key>[A-Za-z0-9]+)
    (?:/branch/(?P<branch>[A-Za-z0-9]+))?
    (?:/(?P<rest>[^?]*))?
    (?:\?(?P<query>.*))?
    $
    """,
    re.VERBOSE,
)


def parse_figma_url(url: str) -> ParsedFigmaUrl:
    """Parse a Figma node URL into ``(file_key, node_id)``.

    Accepts:

    * ``https://www.figma.com/design/<key>/<name>?node-id=A-B``
    * ``https://www.figma.com/file/<key>/<name>?node-id=A-B``
    * ``https://www.figma.com/design/<key>/branch/<branchKey>/<name>?node-id=A-B``
    * ``https://www.figma.com/proto/<key>/...`` — proto links are
      tolerated; the key is used as a file key.

    The ``node-id`` URL parameter uses ``-`` between segments; the
    REST API expects ``:``. We normalise to colon form.

    Args:
        url (str): the full URL string. Whitespace is stripped.

    Returns:
        ParsedFigmaUrl: structured handle.

    Raises:
        FetchError: with ``code="invalid_url"`` for unparseable
            inputs.
    """
    if not isinstance(url, str):
        raise FetchError(
            code=FetchErrorCode.invalid_url,
            message=f"node_url must be a string, got {type(url).__name__}",
        )
    url = url.strip()
    match = _DESIGN_URL_RE.match(url)
    if not match:
        raise FetchError(
            code=FetchErrorCode.invalid_url,
            message=f"could not parse Figma URL: {url!r}",
        )
    file_key = match.group("key")
    branch_key = match.group("branch")

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=False)
    node_ids = query.get("node-id") or query.get("nodeId") or []
    if not node_ids or not node_ids[0]:
        raise FetchError(
            code=FetchErrorCode.invalid_url,
            message=f"URL missing required ?node-id parameter: {url!r}",
        )
    raw_node_id = node_ids[0].strip()
    # Figma URL form: "624-6826"  →  REST form: "624:6826".
    # The first hyphen separates the two id parts; further hyphens
    # are extremely unusual but we still convert them all to colons
    # to be safe (a tri-part id would be malformed anyway).
    node_id = raw_node_id.replace("-", ":")

    return ParsedFigmaUrl(
        file_key=branch_key or file_key,
        node_id=node_id,
        is_branch=branch_key is not None,
        original_url=url,
    )


# --------------------------------------------------------------------------
# Cache helpers.
# --------------------------------------------------------------------------


def _default_cache_dir() -> Path:
    """Return ``$PRISM_MCP_FIGMA_CACHE_DIR`` or
    ``~/.cache/prism-mcp/figma/`` per design doc §6.3."""
    override = os.environ.get("PRISM_MCP_FIGMA_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "prism-mcp" / "figma"


def _cache_path(
    *,
    cache_dir: Path,
    file_key: str,
    node_id: str,
    depth: int,
) -> Path:
    """Compose the cache file path for one request.

    Layout per design doc §6.3:
    ``<cache_dir>/<file_key>--<node_id>--<depth>.json``.

    Node ids contain colons; we replace them with ``_`` so the
    filename is safe on every filesystem we care about. The
    original colon form is recoverable from the JSON body.
    """
    safe_id = node_id.replace(":", "_")
    return cache_dir / f"{file_key}--{safe_id}--{depth}.json"


def _read_cache(
    path: Path,
    *,
    ttl_seconds: float,
) -> dict[str, Any] | None:
    """Return cached JSON if fresh, else ``None``."""
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    age = time.time() - mtime
    if age > ttl_seconds:
        return None
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "figma cache read failed for %s; will refetch",
            path,
            exc_info=True,
        )
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Best-effort cache write. Failures are logged but never raise."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hash-prefixed temp file → atomic rename for crash-safety.
        digest = hashlib.sha256(path.name.encode()).hexdigest()[:8]
        tmp = path.with_suffix(f".{digest}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)
    except OSError:
        logger.warning(
            "figma cache write failed for %s; continuing",
            path,
            exc_info=True,
        )


# --------------------------------------------------------------------------
# HTTP layer.
# --------------------------------------------------------------------------


_BASE_URL = "https://api.figma.com"
"""Production Figma REST base. Tests inject a different base via
the optional ``base_url`` arg to :func:`_fetch_figma_tree`."""


async def _fetch_figma_tree_full(
    *,
    parsed: ParsedFigmaUrl,
    figma_token: str | None,
    depth: int = _DEFAULT_DEPTH,
    bypass_cache: bool = False,
    cache_dir: Path | None = None,
    cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
    client_factory: Any = None,
    base_url: str = _BASE_URL,
) -> FetchedTree:
    """Fetch the Figma node tree for ``parsed.node_id``.

    This is the full-fidelity fetch: it returns a :class:`FetchedTree`
    carrying the ``document`` subtree **plus** the sibling ``components``
    / ``componentSets`` / ``styles`` resolution maps. The thin
    :func:`_fetch_figma_tree` wrapper preserves the legacy
    document-only return for existing callers.

    Args:
        parsed (ParsedFigmaUrl): output of :func:`parse_figma_url`.
        figma_token (str | None): the PAT. Defaults to
            ``$FIGMA_TOKEN`` when not supplied.
        depth (int): the ``depth`` query parameter — controls how
            deep Figma walks the tree before returning. Default
            6 per :data:`_DEFAULT_DEPTH`.
        bypass_cache (bool): when ``True``, ignore any cached
            payload and force a network fetch. The fresh
            response is still written to cache on success.
        cache_dir (Path | None): override cache root. ``None``
            uses ``$PRISM_MCP_FIGMA_CACHE_DIR`` or the default
            ``~/.cache/prism-mcp/figma``.
        cache_ttl_seconds (float): cache freshness window.
        client_factory (Callable | None): factory returning an
            ``httpx.AsyncClient`` context manager. Tests inject a
            stub to avoid network. Production uses
            ``httpx.AsyncClient``.
        base_url (str): override the REST base URL. Tests inject
            a local server; production uses
            ``https://api.figma.com``.

    Returns:
        FetchedTree: the unwrapped ``document`` subtree plus the
        ``components`` / ``componentSets`` / ``styles`` maps that are
        siblings of ``document`` in ``response["nodes"][node_id]``.

    Raises:
        FetchError: see :class:`FetchErrorCode` for the taxonomy.
    """
    token = (figma_token or os.environ.get("FIGMA_TOKEN", "")).strip()
    if not token:
        raise FetchError(
            code=FetchErrorCode.missing_token,
            message="No Figma token available (neither figma_token arg nor "
            "FIGMA_TOKEN env var is set).",
        )

    resolved_cache_dir = cache_dir or _default_cache_dir()
    cache_path = _cache_path(
        cache_dir=resolved_cache_dir,
        file_key=parsed.file_key,
        node_id=parsed.node_id,
        depth=depth,
    )

    if not bypass_cache:
        cached = _read_cache(cache_path, ttl_seconds=cache_ttl_seconds)
        if cached is not None:
            logger.info(
                "figma cache hit file=%s node=%s depth=%d age=fresh",
                parsed.file_key,
                parsed.node_id,
                depth,
            )
            return _unwrap_response_full(cached, parsed.node_id)

    payload = await _fetch_with_retries(
        file_key=parsed.file_key,
        node_id=parsed.node_id,
        depth=depth,
        token=token,
        client_factory=client_factory,
        base_url=base_url,
    )

    raw_bytes = len(json.dumps(payload))
    if raw_bytes > _TREE_SIZE_CAP_BYTES:
        raise FetchError(
            code=FetchErrorCode.tree_too_large,
            message=(
                f"Figma response is {raw_bytes / 1_000_000:.1f}MB which exceeds "
                f"the {_TREE_SIZE_CAP_BYTES / 1_000_000:.0f}MB safety cap."
            ),
        )

    _write_cache(cache_path, payload)

    return _unwrap_response_full(payload, parsed.node_id)


async def _fetch_figma_tree(
    *,
    parsed: ParsedFigmaUrl,
    figma_token: str | None,
    depth: int = _DEFAULT_DEPTH,
    bypass_cache: bool = False,
    cache_dir: Path | None = None,
    cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
    client_factory: Any = None,
    base_url: str = _BASE_URL,
) -> dict[str, Any]:
    """Backward-compatible fetch returning ONLY the document subtree.

    Preserved verbatim for the callers and tests that assert a plain
    ``document`` dict (``server.py`` historically, the fetch unit +
    integration tests, ``scripts/fetch_x_ray_fixtures.py``). New code
    that needs the ``components`` / ``componentSets`` / ``styles``
    resolution maps should call :func:`_fetch_figma_tree_full`, which
    returns a :class:`FetchedTree`.

    Args:
        parsed (ParsedFigmaUrl): output of :func:`parse_figma_url`.
        figma_token (str | None): the PAT (defaults to ``$FIGMA_TOKEN``).
        depth (int): the ``depth`` query parameter.
        bypass_cache (bool): force a network fetch when ``True``.
        cache_dir (Path | None): override the cache root.
        cache_ttl_seconds (float): cache freshness window.
        client_factory (Callable | None): test injection point.
        base_url (str): REST base override for tests.

    Returns:
        dict[str, Any]: the unwrapped document subtree, i.e.
        ``response["nodes"][node_id]["document"]``.

    Raises:
        FetchError: see :class:`FetchErrorCode` for the taxonomy.
    """
    fetched = await _fetch_figma_tree_full(
        parsed=parsed,
        figma_token=figma_token,
        depth=depth,
        bypass_cache=bypass_cache,
        cache_dir=cache_dir,
        cache_ttl_seconds=cache_ttl_seconds,
        client_factory=client_factory,
        base_url=base_url,
    )
    return fetched.document


def _unwrap_response_full(
    payload: dict[str, Any], node_id: str
) -> FetchedTree:
    """Pull the ``document`` subtree AND its sibling resolution maps.

    The Figma REST API returns ``{"nodes": {<id>: {"document": {...},
    "components": {...}, "componentSets": {...}, "styles": {...}}}}``.
    We unwrap here so callers get a :class:`FetchedTree` with the
    document (what the walker traverses) plus the ``components`` /
    ``componentSets`` / ``styles`` maps that turn an instance's
    node-local ``componentId`` into a global ``componentKey``.

    Missing maps default to ``{}`` so downstream code never has to
    None-check them.

    Raises:
        FetchError: ``node_not_found`` if the id is absent.
    """
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        raise FetchError(
            code=FetchErrorCode.node_not_found,
            message=f"Figma response missing 'nodes' object: {payload!r}",
        )
    node = nodes.get(node_id)
    if not isinstance(node, dict) or "document" not in node:
        raise FetchError(
            code=FetchErrorCode.node_not_found,
            message=(
                f"Figma response does not contain node_id={node_id!r} — "
                f"the file may not include it, or the id may be stale."
            ),
        )
    document = node["document"]
    if not isinstance(document, dict):
        raise FetchError(
            code=FetchErrorCode.node_not_found,
            message=(f"Figma node_id={node_id!r} has no 'document' subtree."),
        )

    def _as_map(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    return FetchedTree(
        document=document,
        components=_as_map(node.get("components")),
        component_sets=_as_map(node.get("componentSets")),
        styles=_as_map(node.get("styles")),
    )


def _unwrap_response(payload: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Pull ``payload["nodes"][node_id]["document"]`` defensively.

    Thin wrapper over :func:`_unwrap_response_full` preserved for
    callers (and tests) that only need the document subtree. New code
    that wants the ``components`` / ``componentSets`` / ``styles`` maps
    should call :func:`_unwrap_response_full` directly.

    Raises:
        FetchError: ``node_not_found`` if the id is absent.
    """
    return _unwrap_response_full(payload, node_id).document


async def _fetch_with_retries(
    *,
    file_key: str,
    node_id: str,
    depth: int,
    token: str,
    client_factory: Any,
    base_url: str,
) -> dict[str, Any]:
    """Perform the GET with exponential-backoff retries.

    Returns the JSON-decoded body on the first 2xx response.
    Raises :class:`FetchError` after the retry budget is
    exhausted.
    """
    headers = {"X-Figma-Token": token, "Accept": "application/json"}
    params = {"ids": node_id, "depth": str(depth)}
    url = f"{base_url}/v1/files/{file_key}/nodes"

    factory = client_factory or _default_client_factory()

    last_status: int | None = None
    last_text: str = ""
    for attempt, backoff in enumerate([0.0, *_RETRY_BACKOFF_SECONDS]):
        if backoff > 0:
            await asyncio.sleep(backoff)
        try:
            async with factory() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=_DEFAULT_TIMEOUT,
                )
        except httpx.TimeoutException as exc:
            logger.warning(
                "figma fetch timeout attempt=%d file=%s node=%s err=%s",
                attempt,
                file_key,
                node_id,
                exc,
            )
            if attempt == len(_RETRY_BACKOFF_SECONDS):
                raise FetchError(
                    code=FetchErrorCode.network_timeout,
                    message=f"Figma timed out after {attempt + 1} attempts.",
                ) from exc
            continue
        except httpx.TransportError as exc:
            logger.warning(
                "figma transport error attempt=%d file=%s node=%s err=%s",
                attempt,
                file_key,
                node_id,
                exc,
            )
            if attempt == len(_RETRY_BACKOFF_SECONDS):
                raise FetchError(
                    code=FetchErrorCode.transport_error,
                    message=f"Network error after {attempt + 1} attempts: {exc}",
                ) from exc
            continue

        last_status = response.status_code
        last_text = (response.text or "")[:200]
        if 200 <= response.status_code < 300:
            try:
                return response.json()
            except ValueError as exc:
                raise FetchError(
                    code=FetchErrorCode.transport_error,
                    message=f"Figma returned non-JSON body: {response.text[:200]!r}",
                ) from exc

        if response.status_code in (401, 403):
            raise FetchError(
                code=FetchErrorCode.invalid_token,
                message=(
                    f"Figma rejected the token (HTTP {response.status_code}): "
                    f"{last_text!r}"
                ),
            )
        if response.status_code == 404:
            raise FetchError(
                code=FetchErrorCode.file_not_found,
                message=(
                    f"Figma returned 404 for file_key={file_key!r}: {last_text!r}"
                ),
            )
        if response.status_code in (429, 500, 502, 503, 504):
            logger.warning(
                "figma transient status=%d attempt=%d file=%s node=%s",
                response.status_code,
                attempt,
                file_key,
                node_id,
            )
            if attempt == len(_RETRY_BACKOFF_SECONDS):
                code = (
                    FetchErrorCode.rate_limited
                    if response.status_code == 429
                    else FetchErrorCode.transport_error
                )
                raise FetchError(
                    code=code,
                    message=(
                        f"Figma returned {response.status_code} after "
                        f"{attempt + 1} attempts: {last_text!r}"
                    ),
                )
            continue

        # Any other 4xx is a hard fail.
        raise FetchError(
            code=FetchErrorCode.transport_error,
            message=(
                f"Figma returned unexpected HTTP {response.status_code}: "
                f"{last_text!r}"
            ),
        )

    # Defensive: the loop should always raise or return above.
    raise FetchError(
        code=FetchErrorCode.transport_error,
        message=(
            f"Exhausted retries without a 2xx; last_status={last_status} "
            f"last_text={last_text!r}"
        ),
    )


def _default_client_factory() -> Any:
    """Default :class:`httpx.AsyncClient` factory.

    Returns a callable producing an ``httpx.AsyncClient`` context
    manager. Kept as a function (not a constant) so tests can
    monkeypatch this module's attribute to inject their own
    factory globally if they prefer that over the
    ``client_factory=`` kwarg."""
    return lambda: httpx.AsyncClient()
