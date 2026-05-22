"""Background refresh driver for the published Prism library.

Slice 7 of the PRD asks for a daily poll against Artifactory using
``If-None-Match`` plus ``dist-tags.latest`` so a freshly-published
Prism version lands in the running server without a restart. The
acquisition logic already lives in :class:`prism_mcp.library.Library`;
this module just owns the periodic driver and the lifecycle hooks
that wire it into FastMCP.

The class is intentionally small and pure-asyncio so that:

* tests can drive it with a tiny interval (50ms) and ``asyncio.sleep``;
* production wires it as a FastMCP lifespan, started after the library
  is constructed and cancelled before the server shuts down;
* errors during a refresh never crash the server — they're logged and
  the next tick will retry.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from prism_mcp.library import Library, LibraryError, RefreshOutcome

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_JITTER_SECONDS = 5 * 60


@dataclass(frozen=True)
class RefreshLoopConfig:
    """Tunable knobs for :class:`RefreshLoop`.

    Args:
        interval_seconds (float): base sleep between refresh attempts.
            PRD default is daily (``86400``). Tests use a few hundred
            microseconds.
        jitter_seconds (float): random ``+/-`` jitter added to each
            sleep so a fleet of laptops doesn't synchronise their hits.
            Set to ``0`` in tests for determinism.
        run_on_start (bool): perform one refresh immediately when the
            loop starts, before the first sleep. The PRD wants every
            cold start to refresh, so this defaults to ``True``.
    """

    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    jitter_seconds: float = DEFAULT_JITTER_SECONDS
    run_on_start: bool = True


class RefreshLoop:
    """Asyncio-driven periodic refresh of the in-memory library state.

    Lifecycle:

    1. Construct with a :class:`Library` and (optionally) a
       :class:`RefreshLoopConfig`.
    2. Call :meth:`start` from an async context. Spawns an
       ``asyncio.Task`` running :meth:`run_forever`.
    3. Call :meth:`stop` from an async context to cancel and await
       graceful shutdown.

    Calling :meth:`start` twice is a no-op (the second call returns the
    existing task). This keeps the FastMCP lifespan idempotent for
    tests that re-enter the context.

    Args:
        library (Library): library instance whose ``refresh()`` to call.
        config (RefreshLoopConfig | None): timing knobs; defaults to the
            PRD's daily-poll cadence.
        clock (Callable[[float], Awaitable[None]] | None): override
            for ``asyncio.sleep`` so unit tests can drive deterministic
            timing. The signature mirrors ``asyncio.sleep(delay)``.
    """

    def __init__(
        self,
        library: Library,
        config: RefreshLoopConfig | None = None,
        clock: Callable[[float], object] | None = None,
    ) -> None:
        self._library = library
        self._config = config or RefreshLoopConfig()
        self._sleep = clock or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_outcome: RefreshOutcome | None = None

    @property
    def last_outcome(self) -> RefreshOutcome | None:
        """Return the most recent successful :class:`RefreshOutcome`."""
        return self._last_outcome

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the background task is alive."""
        return self._task is not None and not self._task.done()

    async def run_once(self) -> RefreshOutcome | None:
        """Run one refresh attempt and capture the outcome.

        Errors are logged and swallowed so the loop never dies on a
        transient registry hiccup. Successful runs update
        :attr:`last_outcome` so observers can poll it.

        Returns:
            RefreshOutcome | None: the outcome, or ``None`` if the
            refresh raised :class:`LibraryError` (cold start with no
            cache — recovery is impossible at this tick).
        """
        try:
            outcome = await asyncio.to_thread(self._library.refresh)
        except LibraryError as exc:
            logger.error(
                "refresh failed and no cache fallback is available: %s",
                exc,
            )
            return None
        except Exception:
            logger.exception("refresh raised unexpectedly; will retry")
            return None

        self._last_outcome = outcome
        if outcome.swapped:
            logger.info(
                "refresh swapped index version_before=%s version_after=%s",
                outcome.version_before,
                outcome.version_after,
            )
        elif outcome.not_modified:
            logger.info(
                "refresh no-op: registry returned 304 version=%s",
                outcome.version_after,
            )
        elif outcome.offline:
            logger.warning(
                "refresh fell back to cached version=%s; registry unreachable",
                outcome.version_after,
            )
        else:
            logger.info(
                "refresh produced no change version=%s",
                outcome.version_after,
            )
        return outcome

    async def run_forever(self) -> None:
        """Drive :meth:`run_once` until :meth:`stop` cancels us.

        The loop alternates between sleeping for the configured
        interval (with optional jitter) and calling :meth:`run_once`.
        The first iteration runs immediately when ``run_on_start`` is
        true so cold-start refresh happens on demo day.
        """
        if self._config.run_on_start:
            await self.run_once()

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._next_sleep(),
                )
            except TimeoutError:
                pass
            else:
                break
            await self.run_once()

    def _next_sleep(self) -> float:
        """Return the next sleep duration, applying jitter."""
        jitter = self._config.jitter_seconds
        base = self._config.interval_seconds
        if jitter <= 0:
            return base
        return max(0.0, base + random.uniform(-jitter, jitter))

    def start(self) -> asyncio.Task[None]:
        """Schedule :meth:`run_forever` on the current event loop.

        Returns:
            asyncio.Task: the running task. Idempotent: a second call
            returns the same task.

        Raises:
            RuntimeError: if no event loop is running (caller must
                be inside an ``async def``).
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self.run_forever(), name="prism-mcp-refresh"
        )
        return self._task

    async def stop(self) -> None:
        """Signal the loop to exit and await graceful shutdown."""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
