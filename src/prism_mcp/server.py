"""Entrypoint for the Prism ReactJS MCP server.

This module wires up the official ``mcp`` Python SDK's ``FastMCP`` over
the stdio transport and registers the v1 tool surface.

Slice 1 added the ``echo`` liveness tool.
Slice 2 adds ``get_library_meta`` and an Artifactory acquisition path.
Slices 3-6 add ``list_entities`` / ``get_entity`` / ``search_entities``
across components, hooks, managers, utils, and tokens.
Slice 7 wires a background refresh task into the FastMCP lifespan.
Slice 8 surfaces a clear "VPN required" error on cold-start-no-cache.

Stdout is reserved for the MCP JSON-RPC framing; all logging is routed
to stderr per the project's non-functional requirements.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from prism_mcp.cache import Cache
from prism_mcp.config import ConfigError, ServerConfig
from prism_mcp.entities import EntityType
from prism_mcp.library import Library, LibraryError
from prism_mcp.refresh import RefreshLoop, RefreshLoopConfig
from prism_mcp.registry import RegistryClient

logger = logging.getLogger(__name__)

SERVER_NAME = "prism-mcp"
ECHO_REPLY = "prism-mcp: alive"


def build_server(
    config: ServerConfig | None = None,
    library_factory: object | None = None,
    refresh_config: RefreshLoopConfig | None = None,
    enable_refresh_loop: bool = True,
) -> FastMCP:
    """Construct the MCP server with the v1 tool surface registered.

    Args:
        config (ServerConfig | None): pre-resolved config, or ``None``
            to defer resolution until ``get_library_meta`` is first
            called. Tests pass a fixed config in.
        library_factory (Callable[[], Library] | None): override the
            :class:`Library` constructor. Used by tests to inject a
            fake. If ``None``, a real :class:`Library` is built from
            ``config`` on first use.
        refresh_config (RefreshLoopConfig | None): tunables for the
            background refresh driver. Defaults to the PRD's daily
            cadence; tests override to a tight interval. Ignored when
            ``enable_refresh_loop`` is ``False``.
        enable_refresh_loop (bool): start the periodic refresh task
            under the FastMCP lifespan. Defaults to ``True`` for
            production; the in-process tool tests set this to
            ``False`` because they exercise ``call_tool`` directly
            without ``server.run()``.

    Returns:
        FastMCP: configured server instance with all tools wired up.
    """
    state = _ServerState(
        config=config,
        library_factory=library_factory,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        """Start/stop the daily refresh task across server lifetime.

        We eagerly construct the library here so the cold-start refresh
        runs once before the first tool call lands, matching the
        Slice 7 demo. On cold-start-no-cache the resulting
        :class:`LibraryError` propagates out and FastMCP shuts the
        server down — Slice 8's "connect to VPN" failure mode.
        """
        loop = state.start_refresh_loop(
            refresh_config=refresh_config,
            enabled=enable_refresh_loop,
        )
        try:
            yield
        finally:
            if loop is not None:
                await loop.stop()

    server = FastMCP(SERVER_NAME, lifespan=lifespan)

    @server.tool()
    def echo() -> str:
        """Return a fixed liveness string.

        Returns:
            str: the constant defined by :data:`ECHO_REPLY`.
        """
        logger.info("echo tool invoked")
        return ECHO_REPLY

    @server.tool()
    def get_library_meta() -> dict[str, Any]:
        """Return metadata about the currently indexed Prism library.

        Triggers a registry fetch on cold call; subsequent calls reuse
        the in-process state (and the on-disk cache survives restarts).
        On Artifactory failure with a populated cache, returns the
        cached state with ``from_cache=True``.

        Returns:
            dict: keys ``package_name``, ``version``, ``last_indexed_at``,
            ``source_url``, ``cache_path``, ``from_cache``.
        """
        logger.info("get_library_meta tool invoked")
        library = state.library()
        meta = library.acquire_latest()
        return meta.to_dict()

    @server.tool()
    def list_entities(
        type: EntityType | None = None,
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        """List indexed Prism entities, optionally filtered by ``type``.

        Args:
            type (EntityType | None): when set, restrict to
                ``"component"`` / ``"hook"`` / ``"manager"`` /
                ``"util"`` / ``"token"``. Slice 3 only populates
                components; later slices add the rest.
            include_deprecated (bool): include entities flagged
                ``deprecated``. Default ``False`` because LLMs rarely
                want those.

        Returns:
            dict: ``{"entities": [{"name": ..., "type": ...,
            "summary": ..., "deprecated": ...}, ...], "version": ...}``.
            The list is a *summary* projection — call ``get_entity`` for
            the full signature and examples.
        """
        logger.info(
            "list_entities tool invoked type=%s deprecated=%s",
            type,
            include_deprecated,
        )
        index = state.library().index()
        entries = index.list(type=type, include_deprecated=include_deprecated)
        return {
            "version": index.version,
            "entities": [
                {
                    "name": entity.name,
                    "type": entity.type,
                    "summary": entity.summary,
                    "category": entity.category,
                    "deprecated": entity.deprecated,
                }
                for entity in entries
            ],
        }

    @server.tool()
    def search_entities(
        query: str,
        top_k: int = 5,
        type: EntityType | None = None,
    ) -> dict[str, Any]:
        """Rank indexed Prism entities by relevance to ``query``.

        Powered by BM25 over a synthetic doc per entity:
        ``name + type + category + summary + example-titles`` (PRD
        section 5). camelCase identifiers are tokenized into their
        parts so ``useFocusTrap`` matches a query for ``focus trap``.

        Args:
            query (str): free-text prose query from the LLM.
            top_k (int): max rows to return (default 5).
            type (EntityType | None): optional type filter, same
                literals as :meth:`list_entities`.

        Returns:
            dict: ``{"version": ..., "results": [{...}]}`` where each
            row carries ``name``, ``type``, ``score``, ``summary``,
            ``import_path``, and ``why_matched`` (the query tokens
            that hit the entity's doc). Empty ``results`` means no
            entity scored above zero.
        """
        logger.info(
            "search_entities tool invoked query=%r top_k=%d type=%s",
            query,
            top_k,
            type,
        )
        index = state.library().index()
        rows = index.search(query=query, top_k=top_k, type=type)
        return {"version": index.version, "results": rows}

    @server.tool()
    def get_entity(name: str, type: EntityType) -> dict[str, Any]:
        """Return the full record for one entity.

        Args:
            name (str): exact case-sensitive identifier
                (e.g. ``"Button"``).
            type (EntityType): one of the five entity-type literals.

        Returns:
            dict: the full :class:`Entity` projection including
            ``signature`` (props) and ``examples``.

        Raises:
            LibraryError: if no entity matches; surfaced to the client
            as an MCP tool error.
        """
        logger.info("get_entity tool invoked name=%s type=%s", name, type)
        index = state.library().index()
        entity = index.get(name=name, type=type)
        if entity is None:
            raise LibraryError(
                f"no entity found for type={type!r} name={name!r}"
            )
        return entity.model_dump()

    return server


class _ServerState:
    """Lazy holder for runtime objects shared across tool invocations.

    Keeps tool decorators free of import-time side effects so that
    ``import prism_mcp.server`` never touches the network or
    filesystem. Construction errors surface on first tool call instead
    of preventing the server from booting at all — important because
    Cursor spawns the process before the user types anything.

    Args:
        config (ServerConfig | None): caller-provided config, or
            ``None`` to resolve from env on first use.
        library_factory (Callable[[], Library] | None): override the
            real :class:`Library` constructor for tests.
    """

    def __init__(
        self,
        config: ServerConfig | None,
        library_factory: object | None,
    ) -> None:
        self._config = config
        self._library_factory = library_factory
        self._library: Library | None = None
        self._refresh_loop: RefreshLoop | None = None

    @property
    def refresh_loop(self) -> RefreshLoop | None:
        """Return the running refresh loop, if any."""
        return self._refresh_loop

    def start_refresh_loop(
        self,
        refresh_config: RefreshLoopConfig | None,
        enabled: bool,
    ) -> RefreshLoop | None:
        """Eagerly build the library and start the refresh task.

        Called from the FastMCP lifespan startup. Letting library
        construction happen here (instead of on the first tool call)
        is what gives Slice 8 its clear "cold start with no cache"
        failure mode: the :class:`LibraryError` raised by the cold
        refresh propagates out of the lifespan and FastMCP refuses to
        serve.

        Args:
            refresh_config (RefreshLoopConfig | None): tunables passed
                through to :class:`RefreshLoop`.
            enabled (bool): when ``False``, skip starting the loop and
                return ``None``. The in-process tool tests use this
                because they call ``call_tool`` directly without
                running the server lifecycle.

        Returns:
            RefreshLoop | None: the started loop, or ``None`` when
            disabled.
        """
        if not enabled:
            return None
        library = self.library()
        loop = RefreshLoop(library=library, config=refresh_config)
        loop.start()
        self._refresh_loop = loop
        return loop

    def library(self) -> Library:
        """Return the shared :class:`Library`, building it lazily."""
        if self._library is not None:
            return self._library
        if self._library_factory is not None:
            built = self._library_factory()  # type: ignore[operator]
            if not isinstance(built, Library):
                raise LibraryError(
                    "library_factory returned a non-Library value"
                )
            self._library = built
            return built

        config = self._config or ServerConfig.from_env()
        if config.auth_header is None:
            logger.warning(
                "no Artifactory credentials configured; set JFROG_AUTH "
                "or JFROG_EMAIL+JFROG_API_KEY"
            )
        cache = Cache(config.cache_dir)
        registry = RegistryClient(
            base_url=config.registry_base_url,
            auth_header=config.auth_header,
            verify=_tls_verify_value(config),
        )
        self._library = Library(config=config, registry=registry, cache=cache)
        return self._library


def _tls_verify_value(config: ServerConfig) -> bool | str:
    """Translate ``ServerConfig`` TLS knobs into an httpx ``verify=`` arg.

    Precedence:

    1. If a CA bundle path is set, use it (and ignore the insecure
       flag — explicit trust beats opt-out every time).
    2. Else if ``insecure_tls`` is True, return ``False`` so httpx
       skips verification entirely. A warning lands on stderr from the
       :class:`RegistryClient` constructor.
    3. Else return ``True`` so certifi's default bundle is used.

    Args:
        config (ServerConfig): resolved server configuration.

    Returns:
        bool | str: value suitable for ``httpx.Client(verify=...)``.
    """
    if config.ca_bundle is not None:
        return str(config.ca_bundle)
    return not config.insecure_tls


def _configure_logging() -> None:  # pragma: no cover - transport layer
    """Route all logging to stderr.

    Stdout is reserved for the MCP protocol on the stdio transport.
    Writing anything else to stdout would corrupt the JSON-RPC stream
    and silently break clients (PRD section 9, Security).
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:  # pragma: no cover - transport layer
    """Console-script entrypoint: serve MCP over stdio."""
    _configure_logging()
    logger.info("starting %s on stdio transport", SERVER_NAME)
    try:
        server = build_server()
    except ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        sys.exit(2)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
